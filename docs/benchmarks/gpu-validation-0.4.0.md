# CUDA validation for 0.4.0

Validation on 2026-10-08 used an NVIDIA GeForce RTX 4060 Laptop GPU with
8188 MiB VRAM under WSL, driver 566.26, Python 3.12.15 and ONNX Runtime GPU
1.23.2. Inference used the existing pinned FP32 ONNX models; Torch was absent.
Only generated Russian/English text and images were processed. User Telegram
exports and databases were never opened. Numeric results are in
[gpu-load-0.4.0.json](gpu-load-0.4.0.json).

## Warm model load

Each final warm phase ran for at least 20 seconds. E5 used 32 passages with
variable lengths and a maximum padded sequence of 237 tokens; CLIP used 32
generated images per batch. OCR used the eight-image synthetic quality set,
including rotated text and a 25-line document, with a recognition-region batch
of 32. Detection remains per image.

| Model / configuration | Units per minute | Unit |
| --- | ---: | --- |
| E5, sequential preparation | 2340.72 | passage |
| E5, one-batch lookahead | 2351.50 | passage |
| CLIP, sequential preparation | 2532.42 | image |
| CLIP, one-batch lookahead | 4082.06 | image |
| OCR, one image per request | 142.67 | image |
| OCR, four images per request | 160.38 | image |

CLIP's observed throughput increased by 1.61 times, while E5's change was
negligible: its last tokenization took about 4 ms versus 0.8 s of inference.
OCR's observed gain was 1.12 times. These are short synthetic measurements,
not promised speedups for an archive. Batching preserved all eight OCR
transcriptions, and CPU and GPU also produced identical text: CER 0.1688,
WER 0.1795, eight of nine control-number occurrences found.

GPU utilization averaged 99.39–100% in the final model phases, with no samples
below 10%. Both software thermal slowdown and power-cap flags were active
throughout these phases. Earlier exploratory runs varied substantially as the
laptop warmed up, so they are not suitable for an E5 speedup claim. The final
comparison runs lookahead first and records clocks and throttle flags. No
hardware power or temperature settings were changed.

An additional [embedding compatibility check](gpu-compatibility-0.4.0.json)
compared CPU ONNX Runtime 1.30.0 reference vectors with CUDA 1.23.2: E5 small
and base passages and queries, CLIP text and CLIP images all passed. Acceptance
requires maximum absolute error at most 0.0001 and cosine similarity at least
0.99999. This supports resuming the existing pinned FP32 indexes across CPU/GPU;
it does not establish compatibility for different models or precision.

## Mixed indexing and CPU search

A fresh temporary export contained 448 messages and 64 unique photos. E5 used
GPU batches of 32, CLIP GPU batches of 16, and OCR GPU requests of four images
and 32 text regions. Indexing models shared the GPU; query encoders used CPU.
The per-model CUDA arena setting was 4096 MiB, and the application RAM budget
was 8192 MiB. Arena limits are per model, not an aggregate VRAM ceiling.

The complete pipeline finished in 103.23 seconds, including cold preparation,
a deliberate pause and CPU searches:

- 381 text chunks, all 64 image embeddings and all 64 OCR results were saved.
  All 48 nonempty OCR results also received semantic embeddings.
- Text and media were paused with work pending. In-flight results flushed;
  the checkpoint then remained identical for two seconds. Resume completed
  all queues without discarding earlier results.
- Indexing failures and query exceptions were zero. All 167 combined queries
  returned matches. During indexing, 166 queries correctly warned about partial
  index coverage. Final separate semantic text, image and semantic OCR probes
  returned results without warnings or fallback, using CPU query providers.
- Combined-query p50/p95 were 0.085/0.306 seconds, with a 15.84-second maximum
  during startup/contention. Queries cycle through four strings and reuse the
  embedding cache; these timings do not predict arbitrary uncached queries or
  searches over a large archive.
- Device-wide GPU utilization averaged 99.01%; sampled VRAM peaked at 7823 MiB.
  Sampled parent-plus-children RSS peaked at 4646 MiB. GPU temperature peaked
  at 89°C, with thermal and power limits active. Device telemetry also includes
  desktop and other system GPU use; RSS sums are not PSS or a hard memory bound.

A larger exploratory workload of 1200 text messages and 256 photos did not
finish within its 240-second budget, without recorded indexing failures. It is
not counted as a passing end-to-end run. The smaller complete run validates
orchestration and recovery, rather than completion time for a large export.
Long-duration leak testing and simultaneous CPU + GPU OCR throughput remain
separate experiments.

## Reproduction

Use a dedicated ignored uv environment and verified public model bundles:

```sh
UV_PROJECT_ENVIRONMENT=workspace/gpu-env uv sync --locked --extra gpu --extra ocr
timeout --kill-after=10s 10m workspace/gpu-env/bin/python -u scripts/benchmark_gpu_load.py \
  --models-workspace workspace \
  --ocr-models /path/to/verified/paddle-onnx-bundle \
  --seconds 20 --prefetch-first --output workspace/gpu-load.json
```

The process-group watchdog is required to bound native inference and shutdown;
the script's internal deadline only bounds progress polling. Telemetry must
produce valid samples for the run to pass. Model files may be hard-linked into
the temporary workspace, but the source model cache is read-only. Reports
contain aggregate counts, timings and public configuration, never OCR text,
message content or source paths.

To repeat the cross-runtime check, first generate reference vectors in the CPU
environment, then compare them in the GPU environment:

```sh
.venv/bin/python scripts/benchmark_gpu.py --workspace workspace --cpu-only \
  --vectors-output workspace/cpu-reference.npz --output workspace/cpu-reference.json
workspace/gpu-env/bin/python scripts/benchmark_gpu.py --workspace workspace \
  --reference workspace/cpu-reference.npz --output workspace/gpu-compatibility.json
```
