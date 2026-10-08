This separate uv project is for maintainers exporting BERTA and Giga ONNX models
on CPU. Its PyTorch/Transformers dependencies never enter the application or
portable builds. Use Python 3.12 and the checked-in uv lock.

```bash
uv sync --project tools/model-export --locked
uv run --project tools/model-export --locked python tools/model-export/export.py \
  --model berta --source workspace/model-export/berta \
  --output workspace/model-export/output
```

Repeat with `--model giga` and a separate source directory. All generated weights,
reviewed remote modules and reports belong in ignored `workspace/` or an external
scratch directory. Never commit them. The export code checks every upstream file
against a pinned revision, size and hash before loading the model. Giga's two
reviewed adapter files run only in this isolated environment, in offline mode.
The application uses ONNX only.

The recipe must reproduce the manifest's exact graph hash and pass FP32 reference
parity at variable batch/sequence lengths, including 512 tokens. A mismatched
export blocks publication. The CI workflow publishes both verified graphs as
`model-bundles-v1`, a prerelease separate from application updates. Existing assets
are immutable. Source license metadata and model cards remain in each runtime bundle.

SigLIP 2's existing public FP16 pair is pinned in `siglip2_model.json`; no separate
conversion is required. CPU was checked with ORT 1.23.2 and 1.30.0. SigLIP 2 disables
LayerNormFusion and SimplifiedLayerNormFusion on 1.23.2 to avoid an optimizer crash.
Provider validation on CUDA/CoreML uses public probes before model activation;
hardware performance and compatibility still require actual target-device tests.
