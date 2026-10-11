import copy
import hashlib
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "script"))
import release


def upstream(version="263.6379.0", **fields):
    return {"tag_name": "kotlin-lsp/v" + version, "draft": False, "prerelease": False,
            "html_url": "https://github.com/Kotlin/kotlin-lsp/releases/tag/kotlin-lsp%2Fv" + version,
            **fields}


class DiscoveryTests(unittest.TestCase):
    def test_newest_release_uses_numeric_versions_and_skips_unpublished(self):
        selected = release.newest_release([upstream("263.9.0"), upstream("263.10.0"),
                                          upstream("264.1.0", draft=True),
                                          upstream("264.2.0", prerelease=True),
                                          {"tag_name": "unrelated"}])
        self.assertEqual(selected["tag_name"], "kotlin-lsp/v263.10.0")

    def test_missing_supported_release_fails(self):
        with self.assertRaisesRegex(RuntimeError, "No supported"):
            release.newest_release([upstream(draft=True)])

    def test_untrusted_version_cannot_become_a_tag_or_shell_argument(self):
        for version in ("../../file", "263.1.0\nneeded=true", "$(command)", "v263.1.0", "263.1.0-alpha", None):
            with self.subTest(version=version), self.assertRaises(ValueError):
                release.validate_version(version)

    def test_only_not_found_is_treated_as_missing_release(self):
        for status in (401, 403, 429, 500):
            with self.subTest(status=status), patch.object(release, "request_bytes", side_effect=urllib.error.HTTPError("url", status, "error", {}, None)):
                with self.assertRaises(urllib.error.HTTPError):
                    release.github("repos/example/repo", missing_ok=True)
        with patch.object(release, "request_bytes", side_effect=urllib.error.HTTPError("url", 404, "missing", {}, None)):
            self.assertIsNone(release.github("repos/example/repo", missing_ok=True))

    def discover_fixture(self, existing=None):
        calls = []

        def api(path, **options):
            calls.append(path)
            if path.endswith("releases?per_page=100"):
                return [upstream()]
            if "/commits/" in path:
                return {"sha": "b" * 40}
            return existing

        def download(url, **options):
            if url.endswith(".sha256"):
                return ("c" * 64 + " *archive.sit").encode()
            return b"immutable source archive"

        with tempfile.TemporaryDirectory() as temporary, patch.object(release, "github", side_effect=api), patch.object(release, "request_bytes", side_effect=download), patch.object(release.subprocess, "check_output", return_value="a" * 40 + "\n"):
            output = Path(temporary) / "plan.json"
            github_output = Path(temporary) / "outputs"
            plan = release.discover("anilbeesetti/kotlin-lsp", output=output, github_output=github_output)
            self.assertIn(f"needed={str(plan['needed']).lower()}", github_output.read_text())
            self.assertEqual(json.loads(output.read_text()), plan)
            return plan, calls

    def test_published_candidate_is_skipped_without_downloads(self):
        with patch.object(release, "request_bytes", side_effect=AssertionError("unexpected download")):
            plan, calls = self.discover_fixture({"draft": False})
        self.assertFalse(plan["needed"])
        self.assertNotIn("source_commit", plan)
        self.assertFalse(any("/commits/" in call for call in calls))

    def test_failed_or_draft_candidate_is_retried_with_immutable_sources(self):
        for existing in (None, {"draft": True}):
            with self.subTest(existing=existing):
                plan, calls = self.discover_fixture(existing)
                self.assertTrue(plan["needed"])
                self.assertTrue(plan["source_url"].endswith("b" * 40))
                self.assertEqual(plan["source_sha256"], hashlib.sha256(b"immutable source archive").hexdigest())

    def test_repaired_patchset_has_a_new_release_identity(self):
        self.assertNotEqual(release.release_tag("263.6379.0", "a" * 64), release.release_tag("263.6379.0", "b" * 64))


class PackagingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.plan = {"schema": 1, "version": "263.6379.0", "platform": release.PLATFORM,
                     "patch_revision": release.PATCH_REVISION, "recipe_sha256": release.recipe_digest(),
                     "source_commit": "b" * 40, "source_sha256": "c" * 64, "archive_sha256": "d" * 64,
                     "build_commit": "a" * 40, "upstream_release_url": upstream()["html_url"]}
        self.plan.update(release_tag=release.release_tag(self.plan["version"], self.plan["recipe_sha256"]),
                         source_url=f"https://codeload.github.com/Kotlin/kotlin-lsp/tar.gz/{'b' * 40}",
                         archive_url="https://download.jetbrains.com/language-server/kotlin-server/263.6379.0/kotlin-server-263.6379.0-aarch64.sit")
        self.plan_path = self.root / "plan.json"
        self.write_plan()
        self.server = self.root / "server"
        self.server.mkdir()
        (self.server / "product-info.json").write_text(json.dumps({"buildNumber": self.plan["version"]}))
        (self.server / "zed-native-importer.json").write_text(json.dumps({"revision": self.plan["version"] + "+" + release.PATCH_REVISION}))
        entrypoint = self.server / "bin/intellij-server"
        entrypoint.parent.mkdir()
        entrypoint.write_text("#!/bin/sh\nexit 0\n")
        entrypoint.chmod(0o755)
        self.reports = self.root / "reports"
        for variant in ("demoDebug", "fullDebug"):
            path = self.reports / variant
            path.mkdir(parents=True)
            (path / "results.json").write_text(json.dumps({"passed": True,
                "expected_release": self.plan["version"], "requested_variant": variant,
                "checks": [{"name": "initialize", "passed": True, "required": True}]}))
        self.destination = self.root / "dist"

    def write_plan(self):
        self.plan_path.write_text(json.dumps(self.plan))

    def bundle(self):
        release.package(self.plan_path, self.server, self.reports, self.destination)

    def test_missing_probe_prevents_packaging(self):
        (self.reports / "fullDebug/results.json").unlink()
        with self.assertRaises(FileNotFoundError):
            self.bundle()
        self.assertFalse(self.destination.exists())

    def test_expired_or_failed_probe_cannot_publish(self):
        path = self.reports / "demoDebug/results.json"
        result = json.loads(path.read_text())
        result.update(passed=False, failure={"message": "This build has expired"})
        path.write_text(json.dumps(result))
        with self.assertRaisesRegex(RuntimeError, "did not pass"):
            self.bundle()
        self.assertFalse(self.destination.exists())

    def test_incomplete_or_wrong_variant_probe_is_rejected(self):
        path = self.reports / "fullDebug/results.json"
        result = json.loads(path.read_text())
        for fields in ({"checks": []}, {"requested_variant": "demoDebug"}, {"expected_release": "263.4702.0"}):
            with self.subTest(fields=fields):
                path.write_text(json.dumps({**result, **fields}))
                with self.assertRaisesRegex(RuntimeError, "did not pass"):
                    self.bundle()

    def test_recipe_or_source_url_substitution_is_rejected(self):
        original = copy.deepcopy(self.plan)
        for fields in ({"recipe_sha256": "e" * 64}, {"source_url": "https://example.test/source"},
                       {"archive_url": "https://example.test/archive"}):
            with self.subTest(fields=fields):
                self.plan = {**original, **fields}
                self.write_plan()
                with self.assertRaises(ValueError):
                    release.read_plan(self.plan_path)

    def test_archive_preserves_executable_and_checksums(self):
        self.bundle()
        manifest = json.loads((self.destination / "runtime.json").read_text())
        asset = manifest["artifacts"][0]
        archive = self.destination / asset["name"]
        self.assertEqual(asset["sha256"], release.digest(archive))
        with tarfile.open(archive) as contents:
            self.assertEqual(contents.getmember("kotlin-server/bin/intellij-server").mode & 0o111, 0o111)
        for line in (self.destination / "SHA256SUMS").read_text().splitlines():
            checksum, name = line.split()
            self.assertEqual(checksum, release.digest(self.destination / name))

    def test_optional_performance_observation_does_not_block_release(self):
        path = self.reports / "demoDebug/results.json"
        result = json.loads(path.read_text())
        result["checks"].append({"name": "protocol_budget", "passed": False, "required": False})
        path.write_text(json.dumps(result))
        self.bundle()


if __name__ == "__main__":
    unittest.main()
