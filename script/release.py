#!/usr/bin/env python3
"""Discover upstream candidates and package validated Koda runtimes."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
UPSTREAM = "Kotlin/kotlin-lsp"
PATCH_REVISION = "android-8"
PLATFORM = "macos-aarch64"
VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
DIGEST = re.compile(r"[0-9a-f]{64}")


def validate_version(version):
    if not isinstance(version, str) or not VERSION.fullmatch(version):
        raise ValueError(f"Unsupported upstream version: {version!r}")
    return version


def digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def recipe_digest(root=ROOT):
    paths = [path for directory in ("patches", "script", "fixtures")
             for path in (root / directory).rglob("*")
             if path.is_file() and "__pycache__" not in path.parts
             and not any(part in (".gradle", "build") for part in path.relative_to(root).parts)
             and path.name != "local.properties"]
    paths.extend(root / name for name in ("NOTICE", "LICENSE-APACHE", "LICENSE-GPL", ".github/workflows/release.yml"))
    hasher = hashlib.sha256()
    hasher.update(PATCH_REVISION.encode() + b"\0" + PLATFORM.encode() + b"\0")
    for path in sorted(paths):
        if path.is_symlink():
            raise ValueError(f"Recipe contains a symlink: {path}")
        hasher.update(path.relative_to(root).as_posix().encode() + b"\0")
        hasher.update(bytes.fromhex(digest(path)))
    return hasher.hexdigest()


def release_tag(version, recipe):
    validate_version(version)
    if not DIGEST.fullmatch(recipe):
        raise ValueError("Invalid recipe SHA-256")
    return f"koda-{version}-{PATCH_REVISION}-{recipe[:12]}"


def request_bytes(url, maximum=16 * 1024**2, token=None):
    headers = {"User-Agent": "koda-kotlin-lsp-release", "Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as response:
        if not response.url.startswith("https://"):
            raise RuntimeError("Insecure release download redirect")
        content = response.read(maximum + 1)
        if len(content) > maximum:
            raise RuntimeError(f"Response exceeds {maximum} bytes")
        return content


def github(path, missing_ok=False):
    try:
        return json.loads(request_bytes("https://api.github.com/" + path,
                                      token=os.environ.get("GH_TOKEN")))
    except urllib.error.HTTPError as error:
        if missing_ok and error.code == 404:
            return None
        raise


def newest_release(releases):
    candidates = []
    for release in releases:
        if release.get("draft") or release.get("prerelease"):
            continue
        tag = release.get("tag_name", "")
        if tag.startswith("kotlin-lsp/v") and VERSION.fullmatch(tag.removeprefix("kotlin-lsp/v")):
            candidates.append(release)
    if not candidates:
        raise RuntimeError("No supported published upstream release was found")
    return max(candidates, key=lambda release: tuple(map(int, release["tag_name"].split("/v")[1].split("."))))


def discover(repository, version=None, output=Path("plan.json"), github_output=None):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Invalid release repository")
    if version:
        version = validate_version(version)
        tag = "kotlin-lsp/v" + version
        upstream = github(f"repos/{UPSTREAM}/releases/tags/{urllib.parse.quote(tag, safe='')}")
        if upstream.get("draft") or upstream.get("prerelease") or upstream.get("tag_name") != tag:
            raise RuntimeError("Only published upstream releases may be built")
    else:
        upstream = newest_release(github(f"repos/{UPSTREAM}/releases?per_page=100"))
        version = validate_version(upstream["tag_name"].removeprefix("kotlin-lsp/v"))
    recipe = recipe_digest()
    tag = release_tag(version, recipe)
    existing = github(f"repos/{repository}/releases/tags/{tag}", missing_ok=True)
    needed = existing is None or existing.get("draft", False)
    plan = {"schema": 1, "version": version, "upstream_tag": upstream["tag_name"],
            "release_tag": tag, "recipe_sha256": recipe, "platform": PLATFORM,
            "patch_revision": PATCH_REVISION, "needed": needed,
            "upstream_release_url": upstream["html_url"],
            "build_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()}
    if needed:
        commit = github(f"repos/{UPSTREAM}/commits/{urllib.parse.quote(upstream['tag_name'], safe='')}")["sha"]
        if not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise RuntimeError("Invalid upstream source commit")
        source_url = f"https://codeload.github.com/{UPSTREAM}/tar.gz/{commit}"
        archive_name = f"kotlin-server-{version}-aarch64.sit"
        archive_url = f"https://download.jetbrains.com/language-server/kotlin-server/{version}/{archive_name}"
        checksum = request_bytes(archive_url + ".sha256", maximum=4096).decode().strip().split()[0]
        if not DIGEST.fullmatch(checksum):
            raise RuntimeError("Invalid upstream archive checksum")
        plan.update(source_commit=commit, source_url=source_url,
                    source_sha256=hashlib.sha256(request_bytes(source_url, maximum=64 * 1024**2)).hexdigest(),
                    archive_url=archive_url, archive_sha256=checksum)
    output.write_text(json.dumps(plan, indent=2) + "\n")
    if github_output:
        with github_output.open("a") as destination:
            destination.write(f"needed={str(needed).lower()}\ntag={tag}\nversion={version}\n")
    print(f"{tag}: {'build required' if needed else 'already published'}")
    return plan


def read_plan(path):
    plan = json.loads(path.read_text())
    version = validate_version(plan.get("version"))
    if plan.get("schema") != 1 or plan.get("platform") != PLATFORM:
        raise ValueError("Unsupported release plan")
    if plan.get("patch_revision") != PATCH_REVISION or plan.get("recipe_sha256") != recipe_digest():
        raise ValueError("Release plan does not match this patch recipe")
    if plan.get("release_tag") != release_tag(version, plan["recipe_sha256"]):
        raise ValueError("Unexpected release tag")
    if not re.fullmatch(r"[0-9a-f]{40}", plan.get("source_commit", "")):
        raise ValueError("Invalid source commit")
    for name in ("source_sha256", "archive_sha256"):
        if not DIGEST.fullmatch(plan.get(name, "")):
            raise ValueError(f"Invalid {name}")
    expected_source = f"https://codeload.github.com/{UPSTREAM}/tar.gz/{plan['source_commit']}"
    expected_archive = f"https://download.jetbrains.com/language-server/kotlin-server/{version}/kotlin-server-{version}-aarch64.sit"
    if plan.get("source_url") != expected_source or plan.get("archive_url") != expected_archive:
        raise ValueError("Unexpected upstream download URL")
    return plan


def package(plan_path, server, reports, destination):
    plan = read_plan(plan_path)
    for variant in ("demoDebug", "fullDebug"):
        result_path = reports / variant / "results.json"
        result = json.loads(result_path.read_text())
        checks = result.get("checks", [])
        if (result.get("failure") or result.get("passed") is not True or result.get("expected_release") != plan["version"]
                or result.get("requested_variant") != variant or not checks
                or any(not check.get("passed") for check in checks if check.get("required", True))):
            raise RuntimeError(f"The {variant} protocol probe did not pass")
    product = json.loads((server / "product-info.json").read_text())
    if product.get("buildNumber") != plan["version"]:
        raise RuntimeError("Runtime version does not match the release plan")
    importer = json.loads((server / "zed-native-importer.json").read_text())
    if importer.get("revision") != plan["version"] + "+" + PATCH_REVISION:
        raise RuntimeError("Runtime patch revision does not match the release plan")
    destination.mkdir(parents=True, exist_ok=True)
    name = plan["release_tag"] + "-" + PLATFORM + ".tar.gz"
    archive = destination / name
    with tarfile.open(archive, "w:gz") as output:
        output.add(server, arcname="kotlin-server")
    manifest = {"schema": 1, "release_tag": plan["release_tag"],
                "upstream_version": plan["version"], "upstream_source_commit": plan["source_commit"],
                "upstream_archive_sha256": plan["archive_sha256"],
                "patch_revision": PATCH_REVISION, "recipe_sha256": plan["recipe_sha256"],
                "build_commit": plan["build_commit"],
                "artifacts": [{"platform": PLATFORM, "name": name, "sha256": digest(archive),
                               "size": archive.stat().st_size, "entrypoint": "kotlin-server/bin/intellij-server"}]}
    manifest_path = destination / "runtime.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    (destination / "SHA256SUMS").write_text("".join(f"{digest(path)}  {path.name}\n" for path in (archive, manifest_path)))
    (destination / "release-notes.md").write_text(
        f"Patched Kotlin LSP {plan['version']} ({PATCH_REVISION}) for Apple Silicon macOS.\n\n"
        f"Upstream: {plan['upstream_release_url']}\n\n"
        "Validated with real Android imports, completion and navigation for demoDebug and fullDebug.\n"
        "The archive includes the bundled Java runtime, patch sources, licenses and provenance.\n"
        "The upstream prerelease expiry remains in effect. Koda integration must select a compatible patch revision.\n")
    print(f"Packaged {archive}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    detect = commands.add_parser("discover")
    detect.add_argument("--repository", required=True)
    detect.add_argument("--version")
    detect.add_argument("--output", type=Path, default=Path("plan.json"))
    detect.add_argument("--github-output", type=Path)
    bundle = commands.add_parser("package")
    bundle.add_argument("--plan", type=Path, default=Path("plan.json"))
    bundle.add_argument("--server", type=Path, required=True)
    bundle.add_argument("--reports", type=Path, required=True)
    bundle.add_argument("--destination", type=Path, default=Path("dist"))
    arguments = parser.parse_args()
    if arguments.command == "discover":
        discover(arguments.repository, arguments.version, arguments.output, arguments.github_output)
    else:
        package(arguments.plan, arguments.server, arguments.reports, arguments.destination)


if __name__ == "__main__":
    main()
