# OCR performance — 0.3.5

These measurements use generated Russian/English text images and public, pinned
models. No Telegram exports or message content were used. Hardware: RTX 4060
Laptop 8 GB, Linux, ONNX Runtime GPU 1.23.2, FP32. See
[validation metadata](ocr-hybrid-gpu-validation.json).

## Implemented

GPU recognition now groups all regions of an image by the existing fixed input
width buckets, with at most 32 regions per batch. Previously groups of eight
regions were split into smaller width groups. Recognition order, padding and
model files are unchanged; CPU keeps its existing grouping. A bounded crop budget
and persistent per-width OOM backoff limit memory use.

For a generated image with 70 text lines, the median of three warm runs improved
from about 1.10 s to 0.51 s (2.18×). A three-line image improved from 0.12 s to
0.10 s. Transcription matched the previous implementation. These two synthetic
images are a regression check, not an estimate for a real archive.

The optional **CPU + GPU** mode creates two isolated workers. Each claims a
different image from the same content-addressed queue, using the same model and
cache identity. A real-model smoke check confirmed simultaneous CPU/CUDA work,
equal transcription, distinct child processes and complete process cleanup.
CPU threads are divided between workers; this mode requires at least two.
A faster archive-wide rate is not guaranteed when CPU preprocessing or memory is
already saturated. Pause stops both lanes; completed recognition remains cached.

CPU and GPU have separate compute queues; work targeting the same GPU remains
serialized. Auto reserves both queues because a model or restarted OCR child can
fall back to CPU. Explicit hybrid workers do not silently fall back.

E5 allocation backoff was validated after warm batches 1/16/32/64: batch 128
failed in ONNX Runtime's 4 GB CUDA arena; the error was recognized and all 128
passages completed as two batches of 64. The cap survives later day workers and
OCR passage embedding; an explicit text-batch setting or a new encoder resets it.

`/api/doctor` reports the last OCR read, compute-wait, inference, publish and
pipeline timings. It also separates CLIP/E5 time in the shared media cycle.
These fields contain bounded numeric metadata, without recognized text or paths.

## Further experiments

- Prefetch/decode images and batch compatible recognition regions across images.
  Keep bounded RAM and current-ref/pause guards; compare throughput including
  detection and publication, not just GPU kernels.
- Test device-side CTC argmax and ONNX I/O binding to reduce output transfers.
  [ONNX Runtime I/O binding](https://onnxruntime.ai/docs/performance/tune-performance/iobinding.html)
  documents the cost of default CPU inputs/outputs for GPU inference.
- Benchmark pinned multilingual PP-OCRv5 mobile recognition against the existing
  small eslav recognizer on Russian/English photos. The
  [official multilingual models](https://github.com/PaddlePaddle/PaddleOCR/blob/main/docs/version3.x/algorithm/PP-OCRv5/PP-OCRv5_multi_languages.en.md)
  include Russian; version alone is insufficient evidence of greater speed.
  Measure character error, throughput, model size and memory before changing the default.
- FP16 or smaller detector resolution may help, but require quality validation
  and a distinct cache identity if outputs/preprocessing change. Preserve old caches.
- CUDA graphs need further graph work: the current recognizer has CPU shape
  operations, and graph capture requires all nodes on CUDA and stable addresses/shapes.
  See [CUDA graph requirements](https://onnxruntime.ai/docs/execution-providers/CUDA-ExecutionProvider.html#using-cuda-graphs-preview).

No model replacement, FP16 conversion or recognition-resolution reduction is
included in 0.3.5. The application still performs inference through ONNX Runtime
without a Torch dependency.
