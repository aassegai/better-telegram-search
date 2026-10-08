Pinned ONNX artifacts for optional model preparation in Better Telegram Search.
These are model weights, not an application update. This prerelease is excluded
from the application updater.

- BERTA: main text/OCR encoder, MIT, FP16 weights with FP32 Softmax/LayerNorm.
- Giga: optional bidirectional text/OCR reranker, MIT, FP16 weights with the
  upstream FP32 RMSNorm, attention Softmax and rotary frequency computations.
- Source revisions, tokenizer files, graph IO and exact SHA-256/byte counts are
  pinned in the application's manifests. Both exports reproduce the reviewed
  checksums and pass CPU reference parity before publication.
- SigLIP 2 uses the separately pinned public ONNX Community text/vision pair
  under the upstream Apache-2.0 license.

The application downloads ONNX/tokenizer/processor files only. It runs no remote
Python modeling code and needs no PyTorch or Transformers. CPU execution may use
ORT precision casts; FP16 weights do not imply native CPU FP16 arithmetic.
CUDA and CoreML require validation on the actual device before activation.
