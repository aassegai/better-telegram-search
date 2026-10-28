# Automatic vector-file maintenance

Small indexing batches no longer have to leave thousands of tiny files in an
active Lance table. The application checks manifest statistics at startup, then
at most once a minute or after 64 writes. At 128 small fragments, it compacts one
active table before returning to indexing. The same mechanism covers text,
semantic OCR and image vectors. No model downloads or embedding regeneration occur.

Maintenance uses a CPU child with at most four threads, half the configured
application memory budget (capped at 2 GiB), and a 120-second time limit. Failure
defers the next attempt for five minutes; it does not pause the indexing queue.
Interactive reads remain available. Publishers wait before taking the lifecycle
lock, keeping settings and SQLite access responsive. Shutdown cancels the child
and retains the workspace writer lease until its termination is confirmed.

Published days leave durable physical-cleanup records. Background cleanup combines
up to 64 days into one predicate per vector space. It runs after 32 queued days,
60 seconds, or when indexing is idle/paused. Canonical SQLite generations already
exclude superseded vectors from search. Acknowledgment checks the exact chat,
day, space and generation, so retries cannot discard newer cleanup requests.

Automatic compaction retains seven days of versions and does not delete young
unverified files. A temporary version tag additionally protects readers of an
old, otherwise age-eligible latest version. The tag is released after the child
stops and existing readers drain; interrupted attempts reuse one reserved tag.
On Lance 0.25/0.39, native GC may reject tagged historical versions *after*
committing compaction. Only that specific refusal plus a confirmed new commit is
accepted: file compaction succeeds and historical cleanup is deferred. Other
native errors remain failures. User tags are preserved.

The UI shows **Index maintenance** and hides the old indexing ETA during the
operation. `/api/indexing/health` and `/api/doctor` expose
`vector_maintenance` in worker health. Completed vectors and progress remain valid.

## Reproduce the synthetic CPU benchmark

```sh
uv run --extra semantic python scripts/benchmark_vector_maintenance.py
```

The script creates and removes its own temporary database. It never opens an
application workspace, export, model or GPU. It compares 32 separate empty cleanup
queries, one batched query, and that query after compaction. It also verifies IDs,
vector values and an actual scan of a snapshot opened before maintenance.

[Windows sample](benchmarks/vector-maintenance-synthetic-windows.json): 256 files
became one; cleanup took 0.655 s separately, 0.035 s batched, and 0.0073 s after
compaction. Maintenance itself took 4.91 s. These are synthetic storage timings,
not a promised speedup for whole-archive indexing or continuous GPU utilization.
