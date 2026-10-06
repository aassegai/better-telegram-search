# Better Telegram Search

English · [Русский](README.md)

A desktop app for searching your Telegram Desktop conversation archives. Find a
message by keywords or meaning, a photo by its description, or text inside an
image, then read the surrounding messages. Filter by conversation, author, and date.

The app runs locally on CPU. Your conversations stay on your computer; semantic
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

### Your first search

1. Export your conversations from Telegram Desktop as **JSON**. Include photos
   to search images and recognize text inside them.
2. Click **Import archive**, enter the path to `result.json`, then click
   **Check export** and **Apply changes**.
3. Enter a query and click **Search**. **Keywords** search works immediately after
   import. Click **Open context** to see the surrounding messages.
4. For semantic search, open **Settings and diagnostics** and click
   **Prepare model and index**. Once ready, select **Meaning** or
   **Keywords and meaning**, then click **Search**.
5. For photos, click **Prepare OCR** or **Prepare photo search** in settings.
   Under **Search in**, select **Text**, **Images**, and/or **OCR**. You can combine
   them, such as OCR + text or OCR + images. Enter a query and click **Search**.

Preparing E5/CLIP downloads the models; subsequent searches work offline.
**Search results** settings control the number of results and the number of
messages per result fragment. Keep your export folder: the app uses its original
photos.
