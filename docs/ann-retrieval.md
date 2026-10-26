# ANN retrieval implementation

This is an engineering checkpoint, not a completed ANN feature or a claim of
measured search quality. Exact retrieval remains the application default. The
background index scheduler, performance controls, UI selector and real-embedding
quality/latency comparison are still required by the product plan.

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
index creation is currently a synchronous internal primitive, not an HTTP build
handler. A successful replacement retires only older owned configurations;
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

Current internal defaults are four CPU threads, a 512 MiB RSS budget and a 60 s
per-call timeout. RSS is checked before reuse, during a wait and before accepting
even a fast reply. These are not yet wired to the planned performance settings.
The spawn mechanism uses the portable launcher's existing `freeze_support` path;
a packaged-platform smoke check is still part of final validation.

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
