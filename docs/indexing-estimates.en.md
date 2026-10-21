# Indexing time estimates

English · [Русский](indexing-estimates.md)

Estimates use up to 24 recent successful warm batches for the matching model,
device and settings, starting after three measurements. Batch sizes, memory
backoff limits, CPU threads, GPU limits and OCR settings separate observations.
Global queues use the **current** configuration of the remaining work rather
than reusing a historical large batch's speed for a smaller batch.

Batch duration includes resource contention, preparation, reads and publication.
Preparing the next batch can overlap inference; only time that actually delays
the current batch contributes. One-time model warmup and failed batches do not
calibrate throughput. Samples are weighted by work and recency. Isolated stalls
are bounded, while sustained changes update the prediction.

- **Text:** remaining padded token slots are counted for each actual batch,
  excluding completed prefixes and obsolete generations. Unbuilt days use a
  provisional workload calibrated from the archive; measured chunk-building
  time is added separately.
- **Images:** unique available photos are counted; the visual encoder uses
  fixed-size inputs.
- **OCR:** unique unprocessed images are counted. Their text complexity is
  unknown before recognition. Hybrid CPU/GPU lanes are measured independently
  and their throughput is combined.
- **Semantic OCR:** recognized text length determines the remaining workload.
  Undiscovered text is provisional. A global forecast waits for more data when
  incompatible configurations have not been measured.

The UI shows a rounded range, not a promised completion time. This is a practical
range based on observed throughput, not a statistical confidence interval.
The full pipeline's lower bound allows overlap; its upper bound includes serial
execution and the OCR-to-text dependency.

Failures leave the runnable queue until explicitly retried. A pause that prevents
completion hides the estimate, including future semantic OCR. While waiting for RAM,
the UI shows app memory use, the configured threshold and available system memory
instead of an ETA. The threshold applies to the summed RSS of the app and OCR child
processes; free system RAM does not override it. To continue, pause indexing and
raise the threshold under shared indexing resources. Processing resumes automatically
when memory falls below the threshold. Stale observations
stop producing a numeric forecast after ten typical batch durations, bounded
between one minute and one hour.

Intermediate workload volumes are cached for up to five seconds, independently
of progress. Global media grouping invalidates on archive, model, pause or
configuration changes. Stored observations contain only bounded numeric data and
configuration identifiers, without messages, recognized text, paths or images.
Legacy lifetime-average rates are ignored. Updating preserves indexes and
indexing checkpoints.
