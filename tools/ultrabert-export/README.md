# UltraBERT export

This isolated uv project converts the pinned public UltraBERT-Embed-150M checkpoint
to ONNX on CPU. Its Torch and Transformers dependencies are excluded from the
application and portable builds. The BERTA/Giga export environment stays separate.

```bash
uv sync --project tools/ultrabert-export --locked
uv run --project tools/ultrabert-export --locked pytest --noconftest -q tests/test_ultrabert_export.py
uv run --project tools/ultrabert-export --locked python tools/ultrabert-export/export.py \
  --source workspace/ultrabert-source --output workspace/ultrabert-export
```

Use a **new output directory** on each run. Generated graphs, downloaded source
files and references belong in ignored `workspace/` or an external scratch folder.
The exporter reserves its output directory before downloading or loading anything,
verifies all source hashes in `../model-export/ultrabert_source.json`, and stages
only pinned data files for the native Transformers loader. Downloaded upstream
Python is checked for provenance but never executed; `trust_remote_code=False`.

The ONNX graph stores 169 matrices as FP16 and casts them back to FP32 before
computation. Activations, RoPE, RMSNorm, Softmax and one-dimensional parameters stay
FP32. CLS pooling and L2 normalization also use FP32. This reduces storage and
download size; it does not promise FP16 execution, lower resident memory or speed.
Full FP16 arithmetic was rejected because it failed native-reference parity.

Four public probes cover variable batch sizes and sequence lengths through 512
tokens. The native model is the reference; the exportable attention wrapper must
match it before conversion. The final graph must reproduce the exact manifest
SHA-256 and satisfy normalized-output error ≤0.003 and cosine ≥0.9999. These
quantization checks are separate from stricter CPU/GPU same-graph checks.

`--review-output` permits a first unpublished artifact for inspection. Such a
report explicitly has `publication_ready=false`; the publisher rejects it.
The `Verified UltraBERT ONNX bundle` workflow exports with the locked CPU
environment and publishes `model-bundles-v2` as a prerelease separate from app
updates. It never replaces different existing assets. Reproduce the graph and
review changes before updating its publication request or manifest.

Application runtime reports and reproduction commands are in
[UltraBERT documentation](../../docs/ultrabert.md).
