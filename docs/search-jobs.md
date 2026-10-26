# Search jobs

The browser uses managed search jobs. The legacy `GET /api/search` endpoint
remains available for existing clients; it does not provide job cancellation.

## Request and lifecycle

Create with `POST /api/search-jobs` and a version 1 `SearchSpec`, for example:

```json
{
  "schema_version": 1,
  "query": "bicycle repair",
  "scope": { "mode": "all" },
  "author_ids": [],
  "forwarded_from": [],
  "date_from": null,
  "date_to": null,
  "content_type": "all",
  "exclude_deleted": false,
  "exact_phrase": false,
  "mode": "hybrid",
  "modalities": ["text", "images", "ocr"],
  "strategy": "exact"
}
```

Omitted `page_size` and `chunk_size` inherit current workspace display settings
when the job is created. The result contains the effective captured spec. A
selected scope uses `{"mode":"selected","chat_ids":["stable-chat-id"]}`;
an empty selected list means no chats, never all chats. Empty query text browses
canonical messages without loading any inference model. Nonempty stop-word-only
queries remain searches. See [filter semantics and proposals](query-filters.md).

The create response is HTTP 202 with an opaque `job_id`. Poll
`GET /api/search-jobs/{job_id}`; the browser polls every 350 ms. States are
`queued`, `running`, `cancelling`, `cancelled`, `completed`, `failed`.
Only `completed` has a published result. A queue position is reported separately
from the current computational phase.

`POST /api/search-jobs/{job_id}/cancel` removes a queued job immediately or asks
the running job to stop. Repeated cancellation is harmless. Completed and failed
jobs remain terminal. A subsequent search is a new job with a new event and
starts from zero. Cancellation does not pause imports, sync or indexing.

Every mutation requires the current `X-Session-Token` obtained from
`GET /api/session`, and the existing local Host/Origin checks still apply.
Cancellation stays available during vector maintenance; other mutations retain
the maintenance guard.

## Cancellation boundaries and progress

Cancellation is checked while waiting for lifecycle/vector/model/compute locks,
inside SQLite execution, between bounded vector batches, between modalities,
during OCR substring verification, around reranking batches and before result
publication. A running native call cannot be forcefully interrupted; status
remains `cancelling` until it returns, then its output is discarded. Temporary
readers and acquired locks unwind before the worker takes another job.

Status reports the current phase, `completed`, `total` (nullable), `unit`, elapsed
time, measured rate and phase ETA once enough observations exist. Progress is
per phase. A completed vector phase does not mean reranking/results are ready.
Unknown work remains indeterminate rather than using a time-based percentage.

For exact retrieval, `total` is frozen from the canonical SQLite eligibility
query. `completed` counts valid eligible vectors actually scored. Narrow scopes
stream canonical IDs in bounded predicates before fetching vectors. Broad
scopes can use one sequential scan to avoid repeatedly scanning a large append
tail; canonical eligibility is still checked before cosine and global top-K.

Hidden API diagnostics include `scope_messages`, `eligible_vectors`,
`vectors_read`, `vectors_scored`, `candidates_before_rerank`, `returned` and phase
timings. `vectors_read` counts vector rows delivered by Lance to the scorer; it
does not measure physical disk bytes or internal scalar-index reads. These
counters are not GPU utilization measurements.

`strategy` records requested and effective retrieval. At this implementation
stage the scorer is exact; an ANN request reports `ann_index_unavailable` and
effective `exact`. This endpoint is not an ANN implementation or benchmark.

## Results and limits

Fetch `GET /api/search-jobs/{job_id}/result`. Regular search returns a bounded
snapshot of up to 100 candidates; use `offset` and `limit` for later pages.
Pagination does not rerun models. Browsing returns a page of individual matching
messages; pass `browse_cursor` back as the `cursor` parameter for the next page.
The keyset order defaults to newest, with timestamp/chat/message tie breakers.
The captured spec and archive epoch bind the cursor to its original scope.

Job result access rechecks the archive/model/settings revision. Changed or
expired results return HTTP 410; unfinished/stopped jobs return HTTP 409; unknown
job IDs return HTTP 404. Cancelled/failed jobs do not replace the browser's last
completed result. Draft query/filter edits do not relabel the submitted snapshot.

The service runs one search pipeline at a time and admits at most 16 running/
queued jobs. A physically bounded queue removes cancelled pending work, including
when a native call is still running. Queue overflow returns HTTP 429. Temporary
result snapshots retain up to eight entries, 32 MiB total, for ten minutes.
These are process-local limits. [Persistent search history](search-history.md)
stores the last ten completed searches and favorites independently of this TTL;
job results include their durable history/snapshot IDs. Search-history restoration
and pagination do not rerun inference.
