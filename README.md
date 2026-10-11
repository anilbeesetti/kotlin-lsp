# Koda Kotlin LSP

Build and release JetBrains' Kotlin language server with Koda's Android importer,
Compose completion and navigation patches. This repository modifies selected
modules in the official distribution; it does not rebuild the partially closed
server from scratch. Upstream's build expiration remains in effect.

The release workflow checks `Kotlin/kotlin-lsp` every six hours. It also runs on
changes to `main`, and can be dispatched manually with an upstream version.
Published releases are identified by the upstream version and a digest of this
repository's build recipe. An unchanged candidate is skipped. A failed build or
unfinished draft is retried on the next run.

For each candidate the workflow:

1. Resolves the upstream source tag to an immutable commit, snapshots its source
   checksum, and obtains JetBrains' distribution checksum over HTTPS.
2. Downloads the Apple Silicon macOS distribution, checks its checksum, applies
   the patches with zero fuzz, and compiles the replacement modules using JDK 21.
3. Runs the importer contract tests and real LSP probes against an isolated
   Android fixture, including both `demoDebug` and `fullDebug`, Compose completion,
   definitions, references, rename and diagnostics.
4. Packages the modified server with its bundled Java, patch sources, licenses
   and provenance. It uploads all assets to a draft before publishing the release.

A patch, compilation, expired-server startup, import or required probe failure
blocks publication. Build/probe evidence is retained as a workflow artifact.
Only the publication job has permission to write repository contents. No personal
access token is required; the workflow uses GitHub's repository token.

Release assets are a `.tar.gz` containing `kotlin-server/`, `runtime.json` with
version/provenance/platform/checksum information, and `SHA256SUMS`. The app should
select only compatible patch revisions and stage updates alongside the active
installation. Download and auto-update support in Koda is a separate change;
existing Koda nightlies still embed their original runtime pin.

## Patch maintenance

The initial patches and compiler dependencies were copied from Koda commit
`7e49ce60ae5da7173a18dff46e7fbf6ce4eafbdd` and target upstream `263.4702.0`.
The `263.6379.0` source already changes some patched APIs: the initial patchset
must be ported and the full macOS workflow must pass before the first usable
release can be published. Automation attempts new upstream versions; it cannot
automatically repair incompatible source or binary APIs.

Update the patch sources and compiler dependency pins together when necessary.
Changing the recipe produces a new release identity for the same upstream build,
so a corrected patchset can be released without overwriting an existing archive.

## Local checks

Requires Python 3.12 or newer:

```sh
python3 -m unittest discover -s tests -v
python3 script/test-kotlin-lsp --self-test
python3 script/release.py discover --repository anilbeesetti/kotlin-lsp
```

On Apple Silicon macOS with a full JDK 21 and Apple's Command Line Tools:

```sh
export JAVA21_HOME="$JAVA_HOME"
python3 script/build-runtime --plan plan.json
```

The workflow performs the Android SDK setup and release validation. Merely
building the archives or passing the offline tests does not validate a release.
The public source and binary distributions retain their upstream license files;
see `NOTICE`, `LICENSE-APACHE` and `LICENSE-GPL` for the copied Koda sources.
