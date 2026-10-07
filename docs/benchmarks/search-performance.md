# Exact search performance — 0.3.7

The previous implementation divided eligible IDs into groups of 512 and launched
a separate native LanceDB cosine search for each group. A large corpus therefore
incurred repeated table scans and query planning while holding the lifecycle lock.

The replacement reads one Arrow stream, checks each bounded group of IDs against
the canonical SQLite snapshot, computes cosine distances for eligible vectors,
and retains only the global top candidates. Publication generations, embedding
spaces, dialog/author/date/content filters, and OCR versions remain authoritative.
Eligibility applies before top-k; excluded near neighbours cannot displace valid
results. The lifecycle lock continues to protect source/model changes during a
search. This change reduces the work performed under that lock.

One group contains at most 512 vectors. Distances use actual vector norms rather
than assuming normalization; equal distances use deterministic ID ordering. The
implementation uses NumPy and LanceDB's
[streaming batch API](https://lancedb.github.io/lancedb/python/python/#lancedb.query.LanceEmptyQueryBuilder.to_batches).
Models, vector schemas, inference devices, and stored embeddings are unchanged.

The measurements below are medians of three warm trials with alternating old/new
execution order. Inputs are generated FP32 vectors of dimension 384. A temporary
SQLite primary-key whitelist marks 80% as published, and both paths retain the
same top 100. See the [machine-readable report](search-performance-037.json).

| Vectors | Previous | Single pass | Speedup |
| --- | --- | --- | --- |
| 16,384 | 0.682 s | 0.080 s | 8.55× |
| 65,536 | 13.458 s | 0.404 s | 33.33× |

These CPU measurements cover retrieval and a synthetic SQLite eligibility check.
They exclude E5/CLIP inference, real archive joins, context rendering, competing
indexing work, and cold model loading. They do not establish the cause or duration
of a particular user's slow request. No Telegram exports, messages, model weights,
or user database were read. The benchmark creates temporary vector tables and
deletes them when it finishes.

Reproduce from the repository's uv environment:

```sh
uv run --no-sync python scripts/benchmark_search.py --output workspace/search-benchmark.json
```

Regression tests compare distances and filtered ranking with native LanceDB,
exercise non-normalized vectors, Arrow offsets and oversized batches, and reject
unpublished vectors and obsolete OCR versions before selecting top-k. Small FP32
distance differences from native kernels are expected; tied candidates have
deterministic ordering.
