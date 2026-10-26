# SQLite indexing contention

## Confirmed failure and limits of the diagnosis

In the reported Windows 0.6.3 traceback, `semantic-index` failed during cleanup
with `sqlite3.OperationalError: database is locked`. Its exception handler then
tried to save the error in the same database. A second unguarded failure escaped
and terminated the thread. Queue flags alone continued to suggest that indexing
was active. A synthetic SQLite database with an external `BEGIN IMMEDIATE` writer
reproduced this failure before the fix.

The supplied diagnostic snapshot does **not** identify the Windows writer or
prove that sender exclusions caused that particular lock. The model was prepared
and CUDA was selected, but the indexing session was unloaded. A dead worker can
explain the absence of subsequent text GPU batches. Media pauses and the OCR
semantic pause must be checked separately; they are not evidence of the same
failure.

Historical OCR timings (selection 5.58 s, publication 1.27 s, inference 0.28 s)
include time outside model execution. Selection includes acquiring the lifecycle
lock; publication includes waiting for it, eligibility checks, SQLite/FTS writes
and commit. These values do not measure writer ownership alone. Neither the
original Windows database nor its WAL/SHM files were accessed for this change.

## Recovery behavior

- The text worker distinguishes SQLite `BUSY`/`LOCKED`, including extended codes,
  from model failures. It retries automatically with a 250 ms connection wait and
  a 250 ms–2 s backoff. Other threads keep their existing connection timeout.
- Reporting a failure no longer requires SQLite to be writable. In-memory worker
  health remains available if persisting the error also fails. Logs use fixed
  messages, without message text, SQL parameters or archive paths.
- Interrupted work is returned to `pending` only if its ID, generation and claim
  attempt still match. Its build stage and chunk checkpoint remain intact. A
  model failure still pauses indexing before another day can fail, including when
  publishing that pause must wait for SQLite.
- An explicit resume/retry can restart a dead text worker and recover its own
  abandoned attempt. Newer attempts, model changes and invalidated generations
  are not overwritten. Ordinary retries do not switch models or vector spaces.

The dialog shows a database wait without the old ETA, or a stopped indexer with
a restart action. In an **already running, unpatched 0.6.3 process**, a terminated
thread still requires an ordinary application restart; new source changes cannot
repair that process remotely. Do not delete the database or its `-wal`/`-shm`
files to recover a worker.

## Transaction audit

| Operation | Finding and current behavior |
| --- | --- |
| Vector cleanup | Previously, after acknowledging the first item, the next native Lance deletion/prune ran with the SQLite writer transaction still open. Each item now performs native work before its own short acknowledgment transaction. A pass reads at most 64 chat deletions and 64 segment cleanups. Segment acknowledgments check space and generation. |
| OCR claim | Previously, `BEGIN IMMEDIATE` preceded candidate selection, including joins, author exclusions and ordering. Version seeding now commits separately; candidate selection is read-only. A short writer transaction revalidates at most four candidate hashes, the active OCR version, global/chat pauses, eligibility and batch settings before conditional claims. Concurrent claim collisions have bounded retries. |
| OCR publication | Recognition and photo reads occur outside SQLite transactions. A publication commits at most four photo results, including OCR FTS/gram triggers; metrics are recorded after commit. Its recorded duration also includes lifecycle-lock waiting. |
| Sender exclusions | Changes to exclusions, message FTS, context invalidation and affected day generations remain atomic under the lifecycle lock. Breaking these into independent commits could expose stale searchable results. The affected-message query uses the covering `messages_author(chat_id,author_id,timestamp)` index. This path can still be expensive for a large archive/context dependency set. |
| Rate samples | Persistent numeric rate observations have bounded retention (1024 rows globally). Some rate writes can overlap other writers; they are not proof of the reported Windows blocker. |

A local synthetic audit of 40,000 messages, 20,000 affected author messages and
100 days measured exclusion at 0.180 s total / 0.142 s in the writer transaction,
and restoration at 0.166 s / 0.136 s. This fixture had no previously built semantic
context dependencies; these single observations are **not** a production latency
bound or a benchmark of the user's archive. The configured OCR selection plan
used `ocr_work_pending`, `media_by_blob`, message and exclusion keys, with a
temporary sort for the trailing chat ordering.

## Lightweight status

`GET /api/indexing/health` uses the same local-app authentication as other APIs.
It is an async route that reads only process memory, without SQLite, the lifecycle
lock, filesystem access or GPU/model probing. Its `semantic` object includes:

- `enabled`, `alive`, `stopped` and `phase`;
- `waiting_reason`, `database_wait_seconds` and `retry_in_seconds`;
- `heartbeat_age_seconds` and a generic `error`.

`enabled` describes whether this runtime starts the background worker, not whether
a particular dialog/model has been enabled. A heartbeat is the last phase update,
not an inference watchdog: a long legitimate batch can have an old heartbeat.
Full semantic status and `/api/doctor` also include worker health but perform
additional database/status checks, so they can still take longer. No automatic
thread replacement is triggered solely by heartbeat age.

## Verification

`tests/test_sqlite_contention.py` holds real SQLite writer locks to check surviving
cleanup/error-report failures, automatic recovery, preserved checkpoints, deferred
model-error pauses and explicit dead-thread recovery with attempt fences. It also
checks that an independent writer can proceed during a later native cleanup call,
and that health responds with both SQLite and the lifecycle lock held.

`tests/test_ocr_queue.py` checks a real competing writer during configured candidate
selection, concurrent nonduplicate claims, and pause/author/version changes between
selection and claim. Existing OCR lifecycle, cache and exclusion tests protect
publication. Browser checks cover the wait/recovery UI, RU/EN, narrow dark layouts
and health polling while the ordinary dialog status request is blocked.
