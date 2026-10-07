# Better Telegram Search

English · [Русский](README.md)

A desktop app for searching your Telegram Desktop conversation archives. Find a
message by keywords or meaning, a photo by its description, or text inside an
image, then read the surrounding messages. Filter by conversation, author, and date.

The app runs locally on CPU or GPU. Your conversations stay on your computer; semantic
search and photo search use ONNX Runtime.

## Quickstart

### Download a ready-to-run build

Download the archive for your OS and architecture from
[GitHub Releases](https://github.com/aassegai/better-telegram-search/releases)
and extract the entire archive. You do not need Python or Node.js to run it.

| Platform | Launch |
| --- | --- |
| Windows x64 | `BetterTelegramSearch/telegram-search.exe` |
| Linux x64 / ARM64 | `BetterTelegramSearch/telegram-search` |
| macOS Intel / Apple Silicon | `Better Telegram Search.app` |

Your browser opens automatically at `http://127.0.0.1:8765`.
[OS requirements and updates](docs/portable-builds.en.md).
Use the **RU / EN** slider at the top to select English. Your choice is saved in
this browser. Messages and search queries keep their original language.

For NVIDIA on Windows/Linux x64, choose an archive ending in `-gpu`. Torch and a
separate CUDA installation are unnecessary; a compatible NVIDIA driver is required.
Set **Indexing device → GPU** and **Search device → CPU** in settings.
Click **Apply device**, then resume indexing.
Both devices use one index, preserving completed chunks and progress.
GPU sessions are unloaded when indexing is paused or finished.
macOS supports CoreML when the model passes its CPU compatibility check.

From 0.3.0 onwards, use **Settings → Application updates** to check, download,
and **Update and restart**. Upgrade from 0.2.0 manually the first time: pause text
and media indexing, fully close the old app, and launch the current release with the same workspace.
Select GPU indexing and resume; completed CPU chunks do not need rebuilding.

### Run from source

Install Git, [uv](https://docs.astral.sh/uv/getting-started/installation/), and
Node.js 22+. uv sets up Python 3.12 and a separate `.venv` environment.

```sh
git clone https://github.com/aassegai/better-telegram-search.git
cd better-telegram-search
uv sync --locked --python 3.12 --extra semantic --extra ocr
npm ci --prefix frontend
npm run build --prefix frontend
uv run --no-sync telegram-search run
```

For subsequent launches, run the last command. Source installations store their
data in `workspace/`; packaged builds use your OS's application data directory.

For NVIDIA, use a separate environment instead of the CPU extra:

```sh
UV_PROJECT_ENVIRONMENT=workspace/gpu-env uv sync --locked --python 3.12 --extra gpu --extra ocr
UV_PROJECT_ENVIRONMENT=workspace/gpu-env uv run --no-sync telegram-search run
```

In PowerShell, set `$env:UV_PROJECT_ENVIRONMENT = 'workspace/gpu-env'` first, then
run the commands without the environment prefix. [Device details](docs/gpu-and-updates.en.md).

### Your first search

1. Export your conversations from Telegram Desktop as **JSON**. Include photos
   to search images and recognize text inside them.
2. Click **Import archive**, enter the path to `result.json`, then click
   **Check export** and **Apply changes**. The export folder is the parent folder
   containing `result.json`; usually you can leave it blank.
3. Enter a query and click **Search**. **Keywords** search works immediately after
   import. Click **Open context** to see the surrounding messages.
4. Open **Settings** and prepare **E5** for semantic search, **CLIP** for photos,
   or **OCR** for text in images. Then open **⋯** beside a chat and click
   **Resume indexing** for the required index.
5. The chat menu contains text and image batch sizes, progress, and an estimate
   for the whole remaining queue. Larger GPU batches use more memory; the app
   retries smaller batches if memory runs out.
6. **Keywords and meaning** is the default mode. Keyword search remains available
   while the semantic index is being built. Combine **Text**, **Images**, and
   **OCR** under **Search in**, or select **Exact phrase** when needed.

Preparing E5/CLIP downloads the models; subsequent searches work offline.
**Search results** on the main search screen control the number of results and the number of
messages per result fragment. Keep your export folder: the app uses its original
photos.
