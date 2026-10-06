# Standalone CPU builds

Each build includes Python, the interface, ONNX Runtime, LanceDB, tokenizers, and
Tesseract with Russian and English dictionaries. You do not need Python, uv,
Node.js, or a system Tesseract installation to run it. Download E5 and CLIP from
settings; their model revisions are pinned. OCR preparation works offline with
the bundled dictionaries. Conversations and search indexes are not included.

| Platform | Architecture | Minimum OS | Format |
| --- | --- | --- | --- |
| Windows | x86_64 | Windows 10 1903+ / Server 2022 | ZIP |
| Linux / WSL2 | x86_64 | Ubuntu 22.04+ / glibc 2.35+ in CI | tar.gz |
| Linux | ARM64 | Ubuntu 22.04+ / glibc 2.35+ in CI | tar.gz |
| macOS | Apple Silicon | macOS 15 | ZIP containing .app |
| macOS | Intel | macOS 15 | ZIP containing .app |

Each artifact's JSON report confirms its ten successful native checks. Windows
ARM64 has no separate native build yet. Local Linux builds may require a newer
glibc; CI builds use Ubuntu 22.04. Windows signing and macOS notarization are not
configured; macOS builds use PyInstaller's ad-hoc signature.

Extract the **entire folder**, keeping all nested files. On Windows, launch
`BetterTelegramSearch/telegram-search.exe`; on Linux, launch
`BetterTelegramSearch/telegram-search`; on macOS, open `Better Telegram Search.app`.
Your browser opens once the local server is ready. Close the app process or its
terminal to stop the server. If port 8765 is busy, run
`telegram-search run --port 8767`. Select your interface language with the
**RU / EN** slider at the top of the page.

Application data is stored separately:

- Windows: `%LOCALAPPDATA%\BetterTelegramSearch\workspace`.
- macOS: `~/Library/Application Support/BetterTelegramSearch/workspace`.
- Linux: `${XDG_DATA_HOME:-~/.local/share}/BetterTelegramSearch/workspace`.

To open an existing workspace: `telegram-search --workspace /path/to/workspace run`.
Stop the old version before updating; extract the new version into a new folder.
Archives and JSON reports have SHA-256 checksums. `telegram-search --self-test`
checks the build offline using synthetic data in a temporary directory.

## Reproduce a build

Install uv, Git, and Node.js 22 on the target OS. The separate build environment
excludes PyTorch, transformers, and sentence-transformers:

```sh
UV_PROJECT_ENVIRONMENT=workspace/build-env uv sync --locked --no-dev --group bundle --extra semantic --extra ocr --python 3.12 --python-preference only-managed
cd frontend
npm ci
cd ..
workspace/build-env/bin/python scripts/build_app.py --expected-arch x86_64
```

In PowerShell, first set `$env:UV_PROJECT_ENVIRONMENT = 'workspace/build-env'`,
then run the same `uv sync` command without the environment variable prefix.
Launch the builder with `workspace/build-env/Scripts/python.exe`.
For ARM64, pass `--expected-arch arm64`.

Artifacts go into the ignored `artifacts/` folder. The builder extracts each
archive outside the repository and checks SQLite FTS5, LanceDB, CPU ONNX,
tokenizers, safetensors, OCR rus/eng, HTML/API, and session token protection for
mutations. Extraction and workspace paths include Cyrillic, Chinese characters,
and spaces. Windows checks the process UTF-8 code page for native OCR paths.
macOS Intel pins compatible ONNX Runtime 1.23.2 and LanceDB 0.25.3 wheels; other
platform versions come from the shared `uv.lock`. Dependency versions and
licenses are included in `_internal/licenses` (inside the .app resources on
macOS). Native dependencies require a separate build on each OS and architecture.
Both language versions of this guide are included in every build.

The `Portable builds` workflow builds five packages and uploads them to a
**draft release**. The `build-status` branch contains `latest.json` with platform
statuses, SHA-256 checksums, and download links. Drafts are visible to repository
writers. Repository content is checked before building; binaries, plans, models,
and Telegram archives stay out of Git.

Publishing is a separate step authorized by a checked-in
`.github/releases/v<version>.json` request on `main` or a manual run of
`Publish verified release`. It requires five successful native builds and
verifies uploaded JSON reports and archive checksums before publishing the draft.
Application code must match the verified builds; existing tags are never replaced.
