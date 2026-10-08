# Text and visual models

New preparations recommend BERTA FP16 for text/semantic OCR and SigLIP 2
base/patch16/224 for images. Legacy E5 small/base and CLIP remain available.
Updating the application never switches models or rebuilds indexes automatically.

| Model | Role | Dimensions | Preparation |
| --- | --- | --- | --- |
| BERTA | Messages and semantic search of recognized OCR | 768 | Settings → Text model |
| SigLIP 2 | Image description search, including Russian queries | 768 | Settings → Image model |
| Giga | Optional text/OCR reranking | 1024 | Settings → Refine results |

Switching text models requires explicit reindexing. BERTA gets a separate space;
E5 vectors are never renamed or mixed with it. Message and semantic OCR vectors
are rebuilt. Recognition results, original images, author exclusions, chunking
policy, batches, devices and pauses remain. The chunk budget is 8 meaningful
messages and 480 tokens of the selected tokenizer, including prefix/special tokens.
Retained short context consumes tokens.

To switch CLIP → SigLIP 2, select the model and confirm rebuilding the image index.
Only visual vectors change; OCR recognition is not repeated. Preparation checks
files, ONNX contracts and inference probes before activation. Failed preparation
keeps the existing models usable. Repeated preparation without a profile uses the
active model, including recovery of a missing or damaged bundle.

## Giga

Prepare the model, then enable “Giga reranking”. It is off by default. It encodes
the query and up to 50 permitted candidates per text/OCR branch, sorts by cosine,
and passes those ranks to the existing modality RRF. Visual ranks remain unchanged.
Exact phrases and image-only searches bypass Giga. Primary indexes/generations do
not change, and the unprocessed tail keeps its original order.

Giga inherits the text query device, CPU threads and RAM/VRAM limits. Both preparation
and queries check memory; unavailable models, resource limits or inference failures
preserve the base ranking with an explanation. The 512-token limit includes the
query instruction, and truncation is reported. Candidate vectors use an in-memory
LRU capped at 2048 entries/16 MiB or a smaller share of the RAM budget. Keys include
model, content and workspace revision. Filters are checked before and after ranking.
“Show more” reads the completed SearchCache and never reruns inference.

Search responses expose `rerank_applied`, `rerank_reason`, `rerank_branches`;
branch metadata includes candidate count, elapsed time and truncation. The UI
marks results reranked with Giga.

## Runtime and device validation

The user environment uses ONNX Runtime and tokenizers, without Torch/Transformers.
Only pinned ONNX/tokenizer/processor files and model metadata are downloaded. Paths
are shown in settings. BERTA/Giga use separately verified ONNX bundles; SigLIP 2
uses a pinned matching public pair. Every file is checked by size/SHA-256, with
verified local cache support for offline preparation.

The graphs use mixed precision: FP16 weights, declared FP32 stability operations,
FP32 pooling/L2 and FP32 stored embeddings. CPU ORT may insert precision casts and
execute FP32 operations; FP16 weights do not imply native CPU FP16 arithmetic.

Linux x86_64 CPU was checked on ORT 1.23.2/1.30.0: variable batches, up to 512 tokens,
SigLIP 2 fixed64, output shapes/finiteness/norms, tokenizer/reference and image
processor/reference parity. Maximum normalized-vector difference between runtimes
was 0.000372; minimum cosine exceeded 0.999999. BERTA/Giga exports reproduce their
pinned hashes and pass FP32 source-model parity. A synthetic archive passed indexing,
BERTA RU/EN retrieval, SigLIP 2 image retrieval and Giga reranking through application
adapters. These are correctness checks, not a quality evaluation of a private archive.

A separate public synthetic set contains 144 messages, 54 labeled queries and
2 empty-filter checks. BERTA's meaning and hybrid search reached Recall@10 = 1.0
and nDCG@10 = 0.992. On this machine p95 was 50.25 ms and 27.88 ms respectively;
repeated queries may use the cache. Run `scripts/benchmark_semantic.py
--profile berta --workspace <workspace with model> --output <local report>`.
These numbers do not predict latency or relevance on a large personal archive.

Only SigLIP 2 on ORT 1.23.2 disables `LayerNormFusion` and
`SimplifiedLayerNormFusion` to avoid a CPU optimizer crash. CUDA/CoreML were not
run in this task. Before activation, the application compares public probes on the
selected device with CPU. Explicit GPU requests fail visibly; Auto may fall back
to CPU. New profile tolerances do not weaken legacy E5 checks.

## CLI and maintainer exports

```bash
uv run telegram-search --workspace ./workspace prepare-model --profile berta --reindex
uv run telegram-search --workspace ./workspace prepare-media --kind images --profile siglip2 --reindex
```

The separate CPU-only uv export project is documented in
[tools/model-export](../tools/model-export/README.md). The application never performs
that export. ONNX bundles use the separate `model-bundles-v1` prerelease, excluded
from application updates. No new application release version has been declared yet.
