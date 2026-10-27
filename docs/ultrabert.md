# UltraBERT text retrieval

UltraBERT-Embed-150M is the built-in recommendation for English text retrieval.
BERTA remains the Russian recommendation. UI language changes recommendations
only; it never changes the selected model or rebuilds an index.

In **Settings → Models → Text model**, select **UltraBERT-Embed-150M**. An existing
text index requires explicit permission to switch models and rebuild text.
The index includes message text and semantic OCR; source messages, images and the
recognized OCR cache are preserved. BERTA, UltraBERT and legacy E5 vectors have
different spaces even when dimensions match. Existing E5 indexes remain usable.

The application downloads a verified ONNX bundle of about 306 MB, including its
tokenizer and upstream metadata. Inference uses ONNX Runtime; Torch and remote
model Python are absent from the application. The model path appears in its
details. Offline use and local bundle import follow the same checksum checks.

The graph stores matrices as FP16 and computes in FP32. This reduces download and
disk size, without a claim of FP16 computation or lower runtime RAM. The encoder
uses the first CLS/BOS token, float32 L2 normalization and the upstream query
instruction. BOS and padding both have ID zero; the attention mask distinguishes
them. Documents have no instruction prefix. Model capacity is 32,768 tokens; the
application deliberately keeps queries at 512 and document chunks at 480 tokens.
The existing eight-content-message chunk limit is unchanged.

## Verification

- [Native-reference CPU report](benchmarks/ultrabert-cpu.json): exact tokenizer IDs,
  maximum normalized-vector error 0.000166 on four public probes.
- [ORT 1.23.2/1.30.0 parity](benchmarks/ultrabert-runtime-parity.json): maximum
  vector difference below 0.0000005 on the same graph.
- [English retrieval smoke](benchmarks/ultrabert-retrieval-en.json): 144 authored
  messages across three temporary chats, 54 ranked queries and two empty-scope
  checks. Meaning/hybrid Recall@10 and nDCG@10 were 1.0. This small authored set is
  not independent evidence of real-archive quality. Repeated queries can use cache;
  the recorded latency should not be treated as a cold-query comparison.

All measurements use CPU. CUDA/CoreML performance is unverified; the application
checks same-graph CPU/provider compatibility before activating another provider.

To reproduce the English retrieval smoke, prepare the bundle in a scratch
workspace first, then run:

```bash
uv run telegram-search --workspace ./workspace prepare-model --profile ultrabert
uv run python scripts/benchmark_semantic.py --workspace ./workspace \
  --profile ultrabert --language en --output ./workspace/ultrabert-retrieval-en.json
```

Use a scratch workspace for this command: the preparation command enables its
model. The benchmark creates and removes a separate temporary database and imports
only [public synthetic messages](../tests/fixtures/synthetic/relevance.en.json).
For conversion and model-asset publication see the
[isolated export tool](../tools/ultrabert-export/README.md).

Source: [pinned UltraBERT checkpoint](https://huggingface.co/dmis-lab/UltraBERT-Embed-150M/tree/b06b36724c5417a1ec1b7863dd4fa542b74e4899),
Apache-2.0. No automatic download or index migration follows a language change.
