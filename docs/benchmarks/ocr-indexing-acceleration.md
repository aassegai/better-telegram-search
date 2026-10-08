# OCR and indexing pipeline

CPU validation on 2026-10-07 uses generated Russian/English images and the existing
pinned ONNX OCR bundle. No Telegram archive, GPU inference, model conversion or
precision change was used. Runtime dependencies remain managed by `uv`; neural
inference remains ONNX Runtime. Measurement environment: Python 3.12.3, ONNX
Runtime CPU 1.30.0 and tesserocr 2.11.0.

## Implemented

- OCR uses an indexed `pending/running/done/failed` queue with atomic claims.
  Startup recovers interrupted claims. Content deduplication, OCR versions and
  completed caches are preserved. Claim tokens reject stale publication or release
  after deleting and reimporting an image.
- OCR, CLIP and OCR → E5 run as independent stages. Recognition is searchable by
  words immediately after publication. Recognition and semantic OCR have separate
  pause/resume/retry actions; pausing text E5 does not pause OCR → E5.
- Device reservations are FIFO for background tasks, with interactive requests
  taking priority. Separate stages still serialize inference on the same device.
- E5 prepares the next tokenized batch and CLIP reads/preprocesses the next image
  batch while the current batch runs. Each stage retains at most one lookahead
  batch. OCR overlaps one CPU decode with detection and can group recognition
  regions across up to four images. Width buckets, padding and region order remain
  unchanged; detection itself is not batched.
- OCR bounds raw IPC inputs to 32 MiB, targets a 12-million-pixel decoded group
  plus one decode lookahead, and targets 8 million pixels of queued crops. A
  single image/crop may exceed its group target within the existing per-image
  decode and configured maximum-edge limits. Region batches
  are at most 32. Native batch failure backs off to individual images; region OOM
  retains a smaller batch cap. Corrupt images do not invalidate good neighbors.
- Chat settings expose OCR image/region batch sizes. Optional CPU workers are
  configured globally, with one total CPU thread budget divided among processes.
  The default remains one worker and one image, with the existing automatic region
  batch (CPU 8, GPU 32). Increasing a batch or worker count is a tuning option,
  not a guaranteed speedup.
- OCR publishes up to four completed results in a short transaction outside
  inference. Pausing flushes completed results. Shutdown kills an unresponsive
  child, saves completed replies and returns cancelled work to `pending`.
- A persistent OCR child has a 1024-image recycle ceiling, an RSS guard and request
  timeout. RAM throttling can unload idle sessions, so the model retaining memory
  does not permanently block the operation that would release it. CPU workers
  retain per-image timeouts; a timed-out native multi-image request retries with
  one image per request.
- Status includes separate queues, rolling throughput, p50/p95, numeric phase
  timings, cold starts, retries, RSS and CPU usage. Attachments, messages and unique
  available images have distinct counts. GPU utilization/VRAM remain `null` when
  unmeasured. Whole-media ETA adds stage estimates and projects future semantic
  OCR using observed text coverage; it stays unknown until rates are available.
  This is an estimate, not a completion deadline, and excludes text-message E5.

These mechanisms address idle gaps caused by SQL selection, CPU preparation,
stage coupling and repeated model startup. They do not guarantee flat GPU usage:
varying text/image shapes, CPU detection/CTC, transfers and checkpoints still take
time. Subsequent CUDA load and mixed-pipeline validation is recorded separately
in [GPU validation for 0.4.0](gpu-validation-0.4.0.md).

## CPU measurements

The numeric report is [ocr-indexing-acceleration-cpu.json](ocr-indexing-acceleration-cpu.json).
Eight generated images include short RU/EN text, multiline screenshots, small
text, a blank image, a 25-line document and rotated text. Five warm trials per
configuration use four ONNX CPU threads.

| Images / region batch | Warm images/min | Same transcription as baseline |
| --- | ---: | --- |
| 1 / 8 | 279.86 | yes |
| 2 / 8 | 258.83 | yes |
| 4 / 8 | 273.53 | yes |
| 4 / 16 | 236.96 | yes |

CER is 0.1688, WER 0.1795, and 8 of 9 control-number occurrences are found in
every configuration. These absolute errors reflect this small, partly rotated
synthetic set; they are not a general OCR quality score. Cross-image batching
preserves the baseline here but is not consistently faster on CPU. Native
allocators retain memory between configurations, so in-process RSS snapshots in
this report are not isolated peak-memory comparisons.

For a synthetic queue of 40,060 images with 40,000 already completed, selection
p50/p95 changes from 18.5271/26.1234 ms for the previous `NOT EXISTS … LIMIT 1`
scan to 0.0046/0.0065 ms for indexed selection of up to four pending images.
This measures only SELECT latency, not claim transactions or total indexing time.

An additional [OCR end-to-end report](ocr-indexing-end-to-end-cpu.json) uses a fresh
temporary archive: 28 attachments, 25 unique images, 24 successful recognitions
and one file changed after import. The same total budget of four inference
threads is split across processes. Each configuration is one cold-inclusive run;
small timing differences are not evidence of a stable improvement.

| CPU workers | Completed images/min | Sampled parent + children RSS | Lexical search p95 |
| --- | ---: | ---: | ---: |
| 1 | 196.59 | 464 MiB | 18.47 ms |
| 2 | 204.50 | 775 MiB | 19.21 ms |
| 4 | 150.38 | 1203 MiB | 15.47 ms |

The four-worker configuration is slower and uses more memory on this dataset.
All three retain the same CER/WER and find 24 of 27 control-number occurrences.
Interactive device reservation p95 is 456–488 ms. This is waiting between active
OCR requests, not semantic-search response time; running inference is not
preempted. The memory figures are sampled RSS sums, not PSS or hard peak bounds.

For 130 requests through the actual ONNX child, the 128-image recycle interval
starts two children and takes 30.17 s; the 1024 ceiling starts one and takes
29.63 s. Sampled child RSS maxima are 395/394 MiB. This confirms one avoided
warmup for this run, not a broad speedup or a long-duration leak guarantee. The
RSS guard and request timeout remain active independently of the ceiling.

The existing Tesseract alternative was also compared on the same generated
archive, using its pinned RU/EN dictionaries and the same four-thread total
budget ([report](ocr-indexing-tesseract-cpu.json)).

| Tesseract workers / threads each | Completed images/min | Sampled total RSS |
| --- | ---: | ---: |
| 1 / 4 | 350.61 | 136 MiB |
| 2 / 2 | 578.27 | 199 MiB |
| 4 / 1 | 606.97 | 308 MiB |

CER/WER are 0.0188/0.0192 in all three configurations, with 24 of 27 control
occurrences found. This document-like dataset favors Tesseract; it does not
establish quality or speed on photographs and memes. Four workers also raise
interactive reservation p95 to 830 ms versus 211 ms with two, despite lexical
search p95 staying below 17 ms. Worker count must balance throughput, RAM and
query waiting. No model or worker default is changed based on this small sample.

## Reproduction

Prepare the pinned public OCR bundle in an ignored workspace, then run:

```sh
uv sync --locked --extra semantic --extra ocr
uv run --no-sync python scripts/benchmark_ocr.py --models /path/to/pinned/ocr/bundle
uv run --no-sync python scripts/benchmark_ocr_indexing.py --models /path/to/pinned/ocr/bundle
uv run --no-sync python scripts/benchmark_ocr_indexing.py --engine tesseract --section indexing --models /path/to/pinned/tessdata
```

The second script creates a fresh temporary database and export. It measures OCR
selection, read, child IPC, inference, SQLite/FTS publication, limited CPU workers,
interactive device waiting and lexical search. It also compares child recycling
at 128 versus 1024 using 130 generated requests. It does not benchmark CLIP, E5
query latency or an entire archive. Reports contain numeric aggregates and public
configuration only, without recognized text or input paths.

## Validation and remaining experiments

Regression coverage includes atomic claims, startup recovery, version/cache
preservation, duplicate content, corrupt images, region mapping, OOM backoff,
independent pauses/retries, deleted/reimported chats, prefetch during inference,
native child cancellation, bounded memory recovery and Windows LF/CRLF startup.
Browser checks cover per-chat settings, integer/range validation, persistence,
late responses after closing a dialog and Russian/English strings.

The CPU implementation covers A01–A05, A07–A09. The subsequent
[RTX 4060 CUDA report](gpu-validation-0.4.0.md) adds utilization, VRAM, batch
comparisons and CPU queries during GPU indexing. A simultaneous CPU + GPU OCR
comparison and an isolated long-duration memory run are still required before
treating the recycle ceiling as a validated leak bound.

A10–A13 remain experiments: a separate fast/accurate pass, resolution/INT8/FP16
quality comparisons, other multilingual models, device-side CTC, I/O binding and
CUDA graphs. They must preserve old caches and use a new identity when model,
precision or preprocessing changes. No default model or precision was changed
without quality evidence. See the
[ONNX quantization guidance](https://onnxruntime.ai/docs/performance/model-optimizations/quantization.html),
[I/O binding documentation](https://onnxruntime.ai/docs/performance/tune-performance/iobinding.html)
and [CUDA graph requirements](https://onnxruntime.ai/docs/execution-providers/CUDA-ExecutionProvider.html#using-cuda-graphs-preview).
