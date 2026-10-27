# Jina text and OCR reranking

[Русский](jina-reranking.ru.md) · [Model migration](model-migration.en.md)

Jina v2 is an optional pairwise reranker for English text and OCR search. Giga
remains the Russian recommendation and the existing default. Changing the
interface language changes the recommendation, not the selected model. Neither
reranker changes the text embedding space or requires rebuilding an index.

## Use

1. Open **Settings → Models → Result reranking**.
2. Select **Jina v2 · English**, then **Save model settings**.
3. Select **Prepare Jina**. This verifies/downloads the pinned model and runs a
   local smoke test. The model path and download size are shown in its card.
4. Enable **Jina reranking** and save. Preparation does not enable it automatically.

Model selection, preparation and enabling are separate actions. An existing model
download must finish before selecting another profile. A failed preparation
remains visible and cannot count as ready. Basic search remains available when
reranking is disabled, unprepared or over its memory budget.

The model is [jinaai/jina-reranker-v2-base-multilingual](https://huggingface.co/jinaai/jina-reranker-v2-base-multilingual/blob/9cfeff2df7d40d1b78e75e5e9cebec92a99813c9/README.md),
pinned at `9cfeff2df7d40d1b78e75e5e9cebec92a99813c9`. Its weights use
**CC-BY-NC-4.0 for noncommercial use**; consult the model terms for commercial use.
The application displays this condition before preparation. The model is optional.

## Processing and limits

The official FP32 ONNX file and matching tokenizer/configuration files total
1,131,141,915 bytes, about 1,079 MiB. Size and SHA-256 checks cover every file.
The application does not download or run the repository's custom Python code.
Runtime dependencies remain ONNX Runtime/tokenizers; Torch is not installed.
Offline preparation uses the verified local model cache.

Unlike Giga's independent query/document embeddings, Jina receives an actual
query/document pair with the XLM-RoBERTa `<s>A</s></s>B</s>` format. It returns one
logit per pair, converted to a float32 sigmoid score. Higher means more relevant.
It does not produce index vectors and is never passed to the text indexer.

- Up to 50 already-filtered candidates per text/OCR branch, in batches of at most
  four and no larger than the configured text batch size.
- Query limited to 512 tokens; pair limited to 1,024 tokens. After bounding the
  query, only the document is truncated. The input character bounds are 8,192
  for a query and 32,768 for each document, before tokenization.
- Truncation is reported for candidates and query. There is no sliding-window
  aggregation of long passages in this adapter.
- Stable candidate IDs, count, shape and finite scores are validated. Equal scores
  preserve original candidate order. Images and exact phrases bypass reranking;
  the unprocessed tail keeps its order, followed by the existing modality fusion.
- The bounded score cache includes model identity, archive/settings revision and
  full query/document hashes. Changing the query cannot reuse another query's scores.
  “Show more” reads the completed result and does not rerun Jina.

Author/date/chat/forwarded filters and source eligibility are checked before and
after inference. Cancellation discards late scores before cache/publication.
Changing the profile or enable state advances a persisted revision, so changing
it back during a search cannot publish results under the wrong model. History
records the manifest actually used for a successful rerank.

Jina inherits the text query device, CPU threads and RAM/VRAM limits. Its session
unloads after the configured idle period. The application checks RAM before
loading; failures retain the base order with an explanation. CPU was run on this
host; CUDA/CoreML were not. A selected GPU must pass a CPU-reference score probe.
An explicit GPU failure is reported, while Auto can fall back to CPU before scoring.

## CPU evidence and reproduction

[Public smoke/latency report](benchmarks/jina-cpu.json): Linux x86_64, ORT 1.30.0,
four CPU threads. Four obvious authored queries (three EN, one RU) put the intended
candidate first. Single-item versus padded-batch score difference was zero; a long
pair was bounded and reported both truncation flags.

The first call took 4.686 seconds including model loading. Twelve subsequent
short batches of four pairs had p50 113.081 ms and p95 157.727 ms. Sampled peak
whole-process RSS, including the long-pair probe, was about 1.82 GiB. Verification
of downloaded files is outside these timing samples. This excludes search, SQLite,
UI and the application's score cache, and does not predict long-passage throughput.
Four smoke queries do not establish Telegram relevance or a quality threshold.

[CPU runtime parity](benchmarks/jina-runtime-parity.json) also checks ORT 1.23.2
in a separate uv environment. The same bundle and 16 pair scores differed by at
most `2.3841858e-7` from ORT 1.30.0; padding and long-pair checks passed on both.
This is runtime compatibility evidence, not a GPU or relevance evaluation. The
secondary run overlapped tests, so its latency is not used for speed comparison.

Prepare the public pinned files in a dedicated benchmark workspace, then run:

```sh
uv run --no-sync python - <<'PY'
from pathlib import Path
from telegram_search.config.model_registry import rerank_spec
from telegram_search.inference.bundles import BundleStore
BundleStore(Path('/tmp/bts-jina-runtime')).prepare(rerank_spec('jina-v2'))
PY
uv run --no-sync python scripts/benchmark_jina.py
```

The script itself verifies local files without auto-downloads. The only other
accepted model workspace is `workspace/benchmarks/jina`, via `--model-workspace`.
No archive or working database is accepted. Model paths containing links/junctions
are rejected. The fixed public report can be replaced only by a report with the
same benchmark marker; alternate output must be under ignored `workspace/` or
`plans/`. Publication uses the shared bounded JSON/atomic output guards. Public
reports contain versions, hashes, aggregate timings and synthetic scores, without
local paths or user data.

UltraBERT and user-provided model adapters remain separate implementation work;
this built-in pairwise adapter does not claim to load arbitrary checkpoints.
