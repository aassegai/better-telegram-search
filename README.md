# Better Telegram Search

English · [Русский](README.ru.md)

Search your Telegram archive by words, meaning, image descriptions, or text inside
pictures. Open a result with its surrounding conversation to find the message you
remember, even when you cannot recall its exact wording.

The app runs locally and opens in your browser. Models process messages on your
computer through ONNX Runtime; conversation contents are not sent to an external
AI service.

## What you can do

- Combine text, image and OCR search with chat, sender, date and forwarded-source filters.
- Browse messages using filters and an empty query, without preparing a model.
- Inspect matching fragments, open conversation context and enlarge images in the page.
- Keep separate search tabs, restore the last ten searches and star favorites.
- Exclude selected senders from a chat's indexes and preview text noise-filter rules.
- Receive new messages and photos from selected chats through an optional Telegram connection.

## Quickstart

1. Download your OS and architecture from [GitHub Releases](https://github.com/aassegai/better-telegram-search/releases/latest).
   Choose a standard CPU package, or a full `-gpu` package for NVIDIA on Windows/Linux x64.
2. Extract the **entire archive**, then launch the application:

   | Platform | Launch |
   | --- | --- |
   | Windows x64 | `BetterTelegramSearch/telegram-search.exe` |
   | Linux x64 / ARM64 | `BetterTelegramSearch/telegram-search` |
   | macOS Intel / Apple Silicon | `Better Telegram Search.app` |

   Python and Node.js are included or unnecessary for these packages. Your browser
   opens at `http://127.0.0.1:8765`. Use **RU / EN** and the theme button at the top.
3. In Telegram Desktop, export one chat as **JSON**, including photos if needed.
   Choose **Import messages**, enter the path to `result.json`, check the export and
   apply the changes. The optional export folder is the **parent folder containing
   `result.json`**. Keep this folder: imported photos remain there.
4. Enter a query and click **Search**. Keyword search is available after import.
   To browse without a query, set filters and click **Show messages**.
5. For meaning, image or OCR search, open **Settings → Models**, choose and prepare
   the relevant model. Then open the chat's **⋯** menu to view indexing progress or
   resume a paused stage. Initial model preparation downloads files; subsequent
   searches work offline.

You can also start from **Sources · Telegram**, sign in and bind a selected Telegram
chat to a local archive. Follow the overlap check before confirming the binding.
See [Telegram connection](docs/telegram-sync.en.md) for API credentials, session
storage and synchronization limits.

## Searching and indexing

**Keywords and meaning** combines keyword and semantic search. Select **Keywords**
for significant query words or **Exact phrase** for a literal phrase. Ordinary
keyword search skips common Russian/English stop words and preserves negations.
OCR keyword search also supports substrings and small typos. Choose any combination
of **Text**, **Images** and **OCR** under **Search in**.

Result settings control results per page and messages per displayed fragment;
**Show more** pages through the saved result. **Open context** shows neighboring
messages. Search history and yellow-star favorites restore parameters and saved
results; independent tabs keep their own filters and results. **Stop** cancels the
current search; the next search starts from the beginning.

For new text indexes, BERTA is recommended for Russian and UltraBERT for English;
SigLIP 2 handles image search. Optional Giga/Jina reranking refines text/OCR results.
Changing UI language does not switch a model. Changing the text or image model
requires explicit reindexing; source messages and recognized OCR are preserved.
Existing legacy indexes remain usable. See [models and migration](docs/model-migration.en.md).

Choose indexing/query devices, memory limits, batches and chat overrides under
**Settings → Performance**. Index on GPU and search on CPU using the same index;
changing devices preserves completed work. NVIDIA needs a compatible driver and a
GPU package, without a separate Torch installation. See [GPU and updates](docs/gpu-and-updates.en.md).

## Run from source

Install Git, [uv](https://docs.astral.sh/uv/getting-started/installation/) and Node.js 22+.
uv creates a separate `.venv` and installs Python 3.12 when needed.

```sh
git clone https://github.com/aassegai/better-telegram-search.git
cd better-telegram-search
uv sync --locked --python 3.12 --extra semantic --extra ocr --extra telegram
npm ci --prefix frontend
npm run build --prefix frontend
uv run --no-sync telegram-search run
```

Use the last command for subsequent launches. Source runs store data in `workspace/`;
standalone packages use your OS application-data directory. Source GPU environments
and package requirements are covered in the guides below.

## Guides

- [Standalone packages, data locations and updates](docs/portable-builds.en.md)
- [Telegram connection](docs/telegram-sync.en.md)
- [Models and reindexing](docs/model-migration.en.md) · [UltraBERT](docs/ultrabert.md) · [Jina](docs/jina-reranking.md)
- [Text chunks](docs/chunking.en.md) · [Noise analysis](docs/noise-analysis.md) · [Search filters](docs/query-filters.md)
- [Indexing estimates](docs/indexing-estimates.en.md) · [SQLite diagnostics](docs/sqlite-indexing-recovery.md)
- [Architecture](docs/architecture.md) · [Release notes](https://github.com/aassegai/better-telegram-search/releases)
