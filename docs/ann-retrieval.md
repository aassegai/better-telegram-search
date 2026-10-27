# ANN retrieval implementation

This is an engineering checkpoint, not a completed ANN feature or a claim of
measured search quality. Exact retrieval remains the application default. Native
retrieval, background building, resource configuration and their APIs are
implemented, including the UI controls and selector. The broader real-embedding
quality/latency acceptance remains required by the product plan; the isolated
[generated-text comparison](ann-benchmark.md) documents its reproducible protocol
and limits.

## Implemented query path

`VectorStore.search_filtered` dispatches the requested strategy for text, image
and semantic OCR branches. Without a search execution it retains exact behavior.
Lexical and filter-only searches do not acquire an ANN strategy just because a
request contains `strategy: "ann"`. Each dense branch records what it actually
executed; a request can therefore report `ann`, `exact`, `mixed`, or `none`.

The initial compatible index is cosine `IVF_FLAT`. It retains float32 vectors,
without PQ/SQ compression, so its approximation is partition selection. This is
an initial comparison candidate; it does not settle the choice against HNSW.
Index ownership names include the algorithm version, dimension and partition
count. Status also checks native index type, metric, dimension and indexed rows.
An unknown or ambiguous vector index causes an explicit exact fallback. Native
index creation has a synchronous internal primitive; HTTP preparation schedules
it in an isolated background worker. A successful replacement retires older owned configurations;
unrelated indexes are preserved. Dropping old versions physically is separate.

## Canonical filters before candidates

The caller retains its SQLite read transaction and lifecycle lock; VectorStore
holds its own lock and sends the native table version to the query child.

- A metadata-only scan checks at most 512 IDs at a time against the canonical
  SQLite predicate. If every row in the coarse Lance predicate is allowed, that
  predicate can safely be used for one native prefiltered query.
- If this proof fails, canonical IDs are streamed into predicates of at most
  512 IDs. Every native query uses `prefilter=True`; bounded top-K lists are merged
  across those disjoint batches. No archive-sized Python set or SQL `IN` is built.
- Every returned ID is checked again against the same canonical snapshot. A
  disagreement causes a fresh exact scan; it does not silently discard hits from
  a global top-K. A failed native call also restarts exact from the snapshot,
  rather than reusing a partially consumed canonical cursor.
- A small scope uses exact directly. A missing/incompatible index, native error,
  timeout or memory limit also has a fixed fallback reason.

SQLite remains authoritative for the same-message witness, chunk generation,
space, author/source/date filters and ready media references. Media hashes can
belong to several chats; their Lance `chat_id` is not treated as authorization.
The metadata proof is recomputed for every query, not reused after source changes.
It and repeated narrow ID predicates have a cost that must be measured alongside
native ANN time.

Normal native search includes the unindexed append/replacement tail; the query
does not use `fast_search`. A float32 fixed-size-list dimension guard applies to
ANN, both exact paths and upsert. An incompatible table fails closed instead of
reinterpreting its vector storage under a different dimension.

## Cancellation and diagnostics

Native queries run in a reusable, CPU-only spawned process. IPC has a 1 MiB frame
limit. The query child checks out the caller's Lance version; it releases its
table and query builder after each reply. The parent polls cancellation while an
exchange thread handles pipe IO. Cancellation kills and joins the child, so a
discarded response cannot leave the native computation running. An idle child is
closed at service shutdown. Native stderr and exceptions are not exposed through
the protocol; fixed error reasons do not contain queries, SQL or local paths.

Application queries use `cpu_threads` and the smaller of `ann_memory_limit_mib`
and the global memory threshold, with a 60 s per-call timeout. A standalone
VectorStore retains its four-thread/512 MiB defaults. RSS is checked before reuse,
during a wait and before accepting even a fast reply.
The spawn mechanism uses the portable launcher's existing `freeze_support` path;
a packaged-platform smoke check is still part of final validation.

## Background lifecycle and API

`AnnIndexService` handles the active text/OCR and visual embedding spaces, one
native build at a time. It uses a separate low-priority CPU child, not the query
child. Native training holds neither the archive lifecycle lock nor the in-process
vector lock; exact and existing-index queries can proceed, as can vector appends.
Metadata checks coalesce frequent writes. The last indexed population determines
the capacity comparison, so 32 fresh rows do not retrain the index repeatedly.

All destructive vector operations first invalidate the matching writer lease and
kill/join its process. A nonterminal stop rejects destructive maintenance instead
of dropping its handle and assuming success. OS stop/observation errors retain the
handle and are reported
as `ann_stop_failed`; they cannot terminate the scheduler or interrupt publication
of a model after its SQLite commit. Destructive operations remain blocked until
termination is confirmed. Process startup checks cancellation
under that same process-state lock, closing the cancel-before-start race. Empty
segment/media pruning remains read-only and does not starve a build. Model-space
changes cancel only the previous space; the visual-model setter also covers device
changes that explicitly rebuild into a different space. Late replies cannot mark
a cleared table or old model ready.

Shutdown waits for the builder to finish stopping before ImportService releases
the workspace writer lock. A child watches its launcher sentinel and exits if the
launcher dies abruptly. During update verification the scheduler is held along
with other background work; successful update confirmation starts it.

`GET /api/ann-indexes` is an async, memory-only status route. It does not acquire
SQLite, the lifecycle lock, a model or a native vector-table handle. Each entry
reports the space/roles, state, known row/index coverage, elapsed time, and a null
training total. There is no invented percentage or batch ETA for native training.
Read routes retain the application's Host/Origin protections; mutations also
require the local session token.

- `POST /api/ann-indexes/prepare` accepts `{}` for all current spaces or
  `{"space_id":"..."}` for a known space and returns 202. A manual preparation
  works without enabling automatic maintenance. Repeated requests coalesce.
- `POST /api/ann-indexes/cancel` stops preparation and persists
  `ann_indexing_enabled: false`, preventing automatic resubmission after restart.
- `PATCH /api/settings` accepts the settings below. ANN changes can be saved while
  model indexing runs. Resource notifications preserve config-write order, so an
  earlier resource save cannot undo a later stop. Changed build limits stop and
  reschedule a manual in-flight build with the new limits. Changing unrelated
  display/GPU settings does not retry a failed ANN build.

| Setting | Default | Bounds / effect |
| --- | --- | --- |
| `ann_indexing_enabled` | false | Automatic build/update; persisted boolean |
| `ann_build_threads` | 2 | 1–32, capped by `cpu_threads` |
| `ann_memory_limit_mib` | 1024 | 256–65536, capped by `memory_limit_mib` per child |
| `ann_partitions` | 64 | 1–4096, target capacity for native training |
| `ann_probes` | 32 | 1–4096, query probes capped by actual partitions |
| `ann_rebuild_percent` | 10 | 1–100, automatic tail threshold; at least 1024 new rows |

Missing indexes build automatically only when enabled and at least 1024 vectors
exist. A usable index is maintained when its unindexed tail reaches the threshold
or its training configuration needs updating. Fresh rows remain searchable before
that point. A build error requires explicit retry or a relevant settings change,
rather than endlessly restarting an over-budget process. On startup an existing
native index is adopted; enabled automatic work reconciles missing/outdated
indexes. An interrupted manual-only request has no partial-training checkpoint;
it can be prepared again. New settings need no SQL schema migration. Probe and
partition changes invalidate search snapshots; CPU/RAM/build scheduling changes
preserve them.

## Interface

Each search tab stores its own **More complete · full search** / **Faster · ANN**
choice. New tabs default to full search. Browsing, exact phrases and keyword-only
text/OCR disable the control with an explanation. Image-description retrieval is
semantic even in words mode, so selecting images still allows ANN. Selecting ANN
does not prepare a model, build an index or start a search automatically.

The completed result retains its effective strategy and human-readable fallback
reason independently of edits to the next query. Restoring a saved snapshot uses
the strategy metadata captured in that result. Native progress shows unknown
totals and completed native calls or metadata checks, never an invented number
of vector comparisons.

Global **Performance → Search acceleration · ANN** owns automatic preparation,
CPU/RAM limits, advanced partitions/probes/update threshold, and per-space
preparation/retry. Settings save only edited ANN fields. Manual preparation uses
saved settings; unsaved edits must be saved first. Status shows queued, building,
cancelling, failed, ready or insufficient vectors. Native training progress is
indeterminate with elapsed time. Stop disables automatic building and requests
termination; a cancelling writer remains visible until it actually stops.

Polling is independent of the main search screen, single-flight, cancellable on
unmount and guarded against replies that predate an accepted action. The panel
works in Russian/English and both color themes. Its current partition/probe
defaults remain preliminary until the real-embedding comparison below.

Lance does not expose the internal number of distance calculations or vectors
read through this API. After a native attempt, aggregate `vectors_read` and
`vectors_scored` are therefore `null`, including when a later branch uses exact.
Separate counters retain known metadata IDs read, native calls/candidates and
exact reads/comparisons. A native phase has an unknown total and no invented ETA.

## Verification and index candidates

Synthetic real-Lance tests cover compatible indexes, canonical prefilters,
modality boundaries, escaped IDs, fresh/replaced vectors, missing/small/foreign
indexes, full retry after a failed second native batch, dimension failures,
configuration replacement, process reuse, cancellation and fast-reply RSS limits.
All-partition tests check correctness against exact; they are not recall evidence
for approximate production settings. The semantic CI job includes these tests.
Lifecycle tests additionally cover real native open→append→index commit, no-op
cleanup, real PID termination before drop/prune/compaction, a late process-start
race, model changes, resource rescheduling, update-confirmation startup, writer
shutdown ownership, parent death and settings notification order.
Fault-injection tests cover stop/observation exceptions, retained writer ownership,
rejected deletion and consistent SQLite/runtime model publication after a commit.

A separate compatibility probe built 4,096 synthetic 32-dimensional vectors and
queried a three-ID native prefilter, on both supported Lance versions:

| Candidate | LanceDB 0.25.3 | LanceDB 0.39.0 |
| --- | --- | --- |
| IVF_FLAT | Build/query passed | Build/query passed |
| IVF_HNSW_SQ | Build/query passed, `ef=128`, refinement 2 | Build/query passed, `ef=128`, refinement 2 |
| IVF_HNSW_FLAT | Unsupported index type | Build/query passed |

This probe establishes API compatibility only. It does not compare meaningful
recall, memory, index size or latency. HNSW_SQ is a cross-version comparison
candidate; HNSW_FLAT requires a capability check and a compatible fallback on
macOS Intel, where the project pins LanceDB 0.25.3. The final comparison must use
real model embeddings, equal scopes/snapshots and warm/cold measurements,
including canonical-filter overhead, fresh updates and the query process.

Relevant upstream contracts: [Python index/query API](https://lancedb.github.io/lancedb/python/python/)
and [Lance scanner prefilter API](https://lancedb.github.io/lance/api/py_modules.html).
