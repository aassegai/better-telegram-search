# Import progress

[Русская версия](import-progress.ru.md)

**Import messages** accepts the JSON export of one Telegram Desktop dialog.
The optional export directory is the directory containing `result.json`, or an
ancestor containing the referenced media. Checking an export creates a durable
report before it starts reading the JSON. Closing the dialog keeps the task;
use **Open report** in the sidebar to return to it. **Open import** opens the
subsequent message-writing task and its report.

The same progress component shows preparation, verification and writing:

- Metadata reading counts messages and hashes JSON in one existing pass.
  This phase reports bytes; it does not make an extra pass just to get a total.
- Checking messages compares canonical revisions and checks referenced files.
  Message counts and the current attachment's byte progress are separate.
- **Check complete** means the report can be applied; it has not imported messages.
- Applying first verifies that the JSON and source revisions still match, then
  queues the existing import worker. **Import complete** means messages have
  been written. Semantic text, image and OCR indexing have their own progress.

Counts reflect committed checkpoints. Active elapsed time survives pause/resume;
waiting while paused does not inflate it. Speed and phase ETA appear after at
least three measured advances and one second. Unknown totals remain
indeterminate. File verification can finish its current native image check before
observing pause/cancel. Resume revalidates the source and scans to the saved
position; changing JSON requires checking it again.

## HTTP clients

Mutations require the current `X-Session-Token` from `GET /api/session` and the
existing local Host/Origin checks. The browser uses:

1. `POST /api/import-previews` with `json_path` and optional `source_root`, `scope`,
   `target_chat_id`, `create_new`, `policy`. HTTP 202 returns the preview immediately,
   including while metadata preparation is running.
2. `GET /api/import-previews/{id}` for the same preview throughout preparation and
   checking. `POST .../{pause|resume|cancel}` controls its existing checkpoint.
3. `POST /api/import-previews/{id}/apply?background=true`. HTTP 202 returns the
   preview with `operation: "apply"`; verification remains visible on this ID.
4. When its state becomes `applied`, follow `applied_import_id` to
   `GET /api/imports/{id}`. `POST /api/imports/{id}/{pause|resume|cancel}` controls
   the writing task. Completion counters and conflicts remain in its report.

Both statuses include nullable `progress`: `phase`, `completed`, nullable `total`,
`unit`, `elapsed_seconds`, `phase_elapsed_seconds`, nullable `rate` and
`eta_seconds`, and nullable `activity` for a current attachment. Progress events
contain counts and constant phase names, not message text or file paths. UI polls
every 500 ms without overlapping polls. Counters are persisted at most every
250 ms, with forced phase boundaries and authoritative committed message counts.

The compatibility endpoints `POST /api/imports` and apply without
`background=true` still perform synchronous initial verification. Use the
preview/background path for visible preparation. Old jobs upgraded from schema
14 can have `progress: null` until resumed; their completion states and
checkpoints remain valid. Migration 015 adds progress columns without rebuilding
messages or vector indexes.

## CLI

```sh
uv run telegram-search --workspace /path/to/workspace import /path/to/result.json --preview
uv run telegram-search --workspace /path/to/workspace import /path/to/result.json
```

The CLI consumes the same worker events through `tqdm` on an interactive stderr.
Stdout remains the aggregate JSON report. Redirected stderr disables the bar;
`--no-progress` disables it explicitly. Progress does not print source names,
paths or message contents.
