# ANN comparison with real text embeddings

[Русский](ann-benchmark.ru.md) · [Retrieval implementation](ann-retrieval.md)

`scripts/benchmark_ann_embeddings.py` compares cosine IVF_FLAT, IVF_HNSW_SQ and
IVF_HNSW_FLAT using BERTA embeddings of authored generated Russian texts. The
embeddings come from the application's pinned ONNX model, tokenizer, prefixes,
masked-mean pooling and float32 normalization, with CPU inference only.
This is an index experiment, not a Telegram relevance evaluation.

## Recorded run

[Full aggregate report](benchmarks/ann-embeddings.json): Linux x86_64, LanceDB
0.39.0, ONNX Runtime 1.30.0, four CPU threads. BERTA produced 8,192 distinct
768-dimensional float32 vectors in 343.451 seconds, including query inference.
Retrieval timings below exclude inference. There are 16 queries per scope.

| Index | Build, s | All index files, MiB | Sampled build process RSS, MiB | All-scope native warm p50/p95, ms, 32 probes |
| --- | ---: | ---: | ---: | ---: |
| IVF_FLAT | 0.472 | 24.24 | 484.7 | 38.015 / 44.137 |
| IVF_HNSW_SQ, refinement 4 | 0.551 | 7.54 | 431.4 | 40.152 / 45.280 |
| IVF_HNSW_FLAT | 0.822 | 25.55 | 474.6 | 34.069 / 36.612 |

All-scope recall was 1.0 for every candidate/probe setting. Filters expose a
different result. Mean strict recall@10 with 32 probes:

| Scope | Eligible vectors | IVF_FLAT | IVF_HNSW_SQ | IVF_HNSW_FLAT |
| --- | ---: | ---: | ---: | ---: |
| Text | 6,553 | 1.00000 | 1.00000 | 1.00000 |
| OCR text | 1,639 | 1.00000 | 1.00000 | 1.00000 |
| One chat | 2,048 | 1.00000 | 1.00000 | 0.98125 |
| Narrow text date | 703 | 1.00000 | 1.00000 | 1.00000 |
| Author | 1,170 | 1.00000 | 1.00000 | 1.00000 |
| Forwarded source | 2,731 | 0.99375 | 1.00000 | 0.99375 |
| Combined | 293 | 0.99375 | 0.95000 | 0.93750 |

The worst individual query at 32 probes was 0.9, 0.2 and 0.0 respectively;
the two last failures occurred in the small combined scope. At 8 probes the
lowest scope means were 0.55625, 0.68750 and 0.70000. With 64 probes all scopes
and queries reached 1.0 in this run. Every candidate returned all 16 known-nearest
fresh-tail probes, and canonical deletions stayed excluded.

The real IVF application path used exact for the narrow-date and combined scopes.
With 32 probes, its all-scope subsequent p50 was 59.512 ms. Forwarded filtering
required six bounded native queries: its subsequent application p50 was 217.616
ms, while the baseline exact pass p50 was 54.530 ms. These are different passes,
not a paired warm speedup ratio; the result still identifies costly scope/IPC
work rather than a demonstrated application acceleration. A smaller SQ index
alone does not justify switching the application to HNSW.

## Reproduce

Use the isolated uv environment with the locked semantic dependencies. Prepare
the public pinned model in the dedicated benchmark workspace:

```sh
uv sync --locked --extra semantic
uv run --no-sync python - <<'PY'
from pathlib import Path
from telegram_search.config.model_registry import model_spec
from telegram_search.inference.bundles import BundleStore
BundleStore(Path('/tmp/bts-ann-benchmark')).prepare(model_spec('berta'))
PY
uv run --no-sync python scripts/benchmark_ann_embeddings.py
```

The complete three-candidate command uses LanceDB 0.39.0. The macOS Intel pin
0.25.3 supports IVF_FLAT/IVF_HNSW_SQ but cannot run the IVF_HNSW_FLAT candidate;
do not interpret its unsupported-type failure as a recall result.

The script verifies local model files and does not download automatically. The
other accepted model workspace is `workspace/benchmarks/ann` in this repository;
pass it with `--model-workspace`. It rejects links/junctions and arbitrary model
workspace paths. No archive or application database is accepted. The corpus
comes from the public synthetic relevance fixture plus deterministic templates.
The main application does not need to run.

The default workload has 8,192 distinct generated texts and 16 paraphrase queries.
Options allow 4,096–32,768 rows, 4–24 queries and 1–8 CPU threads. All candidates
share the exact same input/vector hashes, model space and query vectors. Each
candidate gets a fresh spawned process, SQLite witness table and LanceDB store
inside an automatically removed temporary directory. A candidate has a 900-second
execution limit; interrupted/timed-out children are killed and joined before
cleanup. Inference time and memory are reported separately from retrieval.

The report uses the fixed public destination
`docs/benchmarks/ann-embeddings.json`; an alternate report must be under ignored
`workspace/` or `plans/`. Only a JSON report with this benchmark's version marker
can be replaced. Publication is atomic. The report contains aggregate timings,
versions, counts and hashes, without text, queries, author IDs or local paths.

## What is measured

The common table contains text rows and rows labelled as semantic OCR. For each
candidate the benchmark builds 64 cosine partitions, then tests 8, 32 and 64
probes. HNSW uses `m=20`, `ef_construction=300`, query `ef=128`; SQ refines four
times the requested limit against original vectors. The index size is the sum
of native `_indices` files, including the scalar ID index. The sampled build RSS
is the whole candidate process, including imported libraries and vector data.

Queries use the application's shared canonical prefilter/merge implementation:

- Whole corpus, text only, OCR only and one chat.
- A narrow text-row date range, author, forwarded source and combined chat/author/date.
- Fresh unindexed vectors and canonical deletions after the index was built.

SQLite eligibility is checked in batches of at most 512 IDs. Coarse predicates
are used only after the shared metadata proof succeeds. Other scopes use bounded
ID prefilters before each native top-K. Every result is rechecked. An out-of-scope
result aborts the experiment. Both strategies retain the same SQLite read snapshot
for a scope; exact cosine is the application's `VectorStore.exact_filtered`.

`recall_at_10` is strict ID overlap with exact top-10, divided by the actual exact
result count. It is not semantic recall against human judgments. Near-equal
float32 distances can change ordering at the cutoff. Full-partition probing is
a control, not the intended acceleration setting. Mean and minimum query recall
are both retained; one scope's good mean does not excuse another scope's failure.

The native second pass supplies warm p50/p95 timings, including metadata proof,
SQLite callbacks, bounded queries and merging. The first native call is reported
separately. It is **not OS-cold**: imports, index building and baseline queries
may already warm caches. Exact timings are from the baseline pass.

Only compatible IVF_FLAT also enters the actual application's dispatcher and
spawned query child. Its first call includes child startup; subsequent p50/p95,
recall, effective strategy and fallback reasons are recorded independently.
Small scopes use the application's explicit exact fallback, even though the
native-only experiment still tests ANN there. HNSW's in-process numbers must not
be presented as end-to-end application latency. Query child RSS is not included
in the sampled parent RSS.

Fresh-tail probes deliberately append query-role embeddings as known-nearest
vectors. All must appear in their matching query's top-10 without rebuilding.
Two vectors remain on disk but are removed from canonical eligibility. This
tests tail inclusion and filtering, not the semantic quality of new passages.

## Limits and decision

The texts and their metadata are generated; repeated topic templates are not
representative of a real archive distribution. OCR-labelled rows use generated
text, without an OCR engine. The witness table represents already-published
vectors; it does not validate the production chunk/message joins or same-message
filter semantics. SigLIP image embeddings and audio are outside this run.
Native centroid training is not seeded/shared between candidate builds; differences
at low probe counts cannot be attributed solely to the HNSW graph or quantization.
This is one build per candidate and one host, not a repeated-trial confidence bound.
Top-100 candidate recall, larger corpora and OS-cold application runs remain open.

Keep full exact search as the application default. A change of algorithm/default
requires broader text and actual image workloads, large-scope latency and memory
measurements, and explicit per-scope quality acceptance. This report alone does
not choose HNSW or validate Telegram relevance.

Upstream contracts: [LanceDB Python API](https://lancedb.github.io/lancedb/python/python/)
and [Lance vector scanner/prefilter](https://lancedb.github.io/lance/api/py_modules.html).
