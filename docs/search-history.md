# Saved searches

[Русская версия](search-history.ru.md)

**Search history** keeps the last ten completed searches and all favorites in the
current workspace. A favorite has a filled yellow star. Starring a result applies
to the completed search even when the query box contains a different draft.
Repeating the same conditions updates one history entry and keeps its star.
Failed and stopped searches do not displace completed searches.

**Open search** restores the request, available results and loaded pagination
without invoking a model. **Repeat search** explicitly starts a new search.
An unsuccessful repeat keeps the previous result and its favorite association.
Empty results and browsing with an empty query are saved too.

Each snapshot contains the captured filters, modes, modalities, exact/ANN request,
display limits, completion time, model versions and index coverage. Normal search
holds up to 100 results. Browsing keeps the most recently loaded window of up to
100 messages and its continuation cursor. Regular search restores the number of
results already displayed. Snapshots have a 32 MiB per-result limit and do not
copy image/audio files. Completion order is stored independently of wall-clock
time, so adjusting the system clock does not remove the newest search.

Snapshots survive app restarts and temporary job-result expiry. Completing more
indexing does not silently update an existing snapshot: repeat to see new matches.
Archive edits/deletions, author exclusions, changed model spaces/OCR versions or
retrieval settings invalidate the saved result. The request and favorite remain.
CPU/GPU limits, batch sizes and display defaults preserve existing captured results.
Opening or paging rechecks compatibility before returning stored messages.

**Settings → Database → History and saved results** has separate actions:

- **Clear saved results** removes persistent and temporary result caches while
  preserving requests and favorites.
- **Clear history** removes ordinary history and keeps favorites.
- **Clear favorites** removes stars; the last ten ordinary searches remain.

An open result can outlive deletion of its history entry through its bounded
temporary job snapshot. Its history star is detached; deleting history is distinct
from clearing result caches. Unreferenced persistent snapshots are collected.

## Local HTTP API

Mutations use the existing session token and local Host/Origin guards.

- `GET /api/search-history?offset=0&limit=50`: paged metadata for the union of
  recent and favorite searches; result bodies are not loaded into this list.
- `GET /api/search-history/{id}`: metadata and restored results, or `result: null`
  with `cache_available: false` and a reason when unavailable.
- `GET /api/search-history/{id}?offset=20&limit=20&snapshot_id=...`: cached search
  pagination pinned to the accepted snapshot. Browsing uses `cursor` instead of
  an offset. Repeating a query cannot mix a newer snapshot into an older page.
- `GET /api/search-history/{id}/status?snapshot_id=...`: metadata only, including
  current favorite/cache status. The UI checks it every three seconds.
- `POST /api/search-history/{id}/favorite` with `{"favorite": true|false}`: toggle
  a completed search's favorite status; HTTP 204 on success.
- `DELETE /api/search-history/collections/{history|favorites|cache}`: clear the
  chosen collection; HTTP 204 on success.

Completed search-job results include `history_id`, `snapshot_id`, `completed_at`,
`favorite`, `applied_models` and `snapshot_coverage`. Old job IDs are process-local;
use history to restore across restarts. The legacy synchronous `GET /api/search`
remains a temporary-cache interface and does not create history entries.

Migration 016 adds SQLite snapshot, history and owner tables without rebuilding
message or vector indexes. Stored queries/results are private workspace data and
are covered by the workspace's existing Git exclusion.
