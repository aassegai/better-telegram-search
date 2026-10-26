# Persistent search tab API

Tabs live in the current workspace's SQLite database. They retain editing drafts,
the exact completed snapshot, display state, and the active tab across restarts.
They do not retain a Python model session or automatically restart computation.
This document describes the backend contract; the tab interface is a separate step.

`GET /api/search-tabs` returns ordered tab metadata, `active_tab_id`, and `capacity`
(8). Result bodies are loaded separately. `POST /api/search-tabs` creates and
activates a search tab. Its optional fields are `draft`, `snapshot_id`, and `view`.
New tabs inherit the saved display defaults. The `kind` discriminator currently
contains only `search`; it leaves room for a future tab type without enabling an
agent or changing the workspace's active models.

Drafts use the same bounded fields as `SearchSpec`, but may contain incomplete
dates or an empty modality selection while being edited. These drafts cannot run:
the jobs endpoint separately validates a complete executable `SearchSpec`.

`PATCH /api/search-tabs/{id}` accepts the last known integer `revision` plus any
of `draft`, `view`, and `snapshot_id`. It increments that revision atomically;
a stale update returns 409 instead of replacing a newer draft. An omitted
snapshot leaves the completed result unchanged; explicit `null` detaches it.
Successful job publication also increments the revision and resets the view.
Clients must read fresh metadata after completion and must not blindly replay
an old view or snapshot binding after a 409 conflict.
`view` holds `visible_count` (0–100), nonnegative finite `scroll_y`, and
`display_settings_open`. Persisted view fields never become inference settings.

`POST /api/search-tabs/{id}/activate` selects a tab without running search.
`GET /api/search-tabs/{id}` validates snapshot freshness and returns metadata.
`GET /api/search-tabs/{id}/result` restores a bounded result without inference;
it accepts `offset`, `limit`, or a browse `cursor`. Send the accepted `snapshot_id`
to reject paging after a different completed search replaces it (409). An
unavailable snapshot returns its parameters/provenance with `result: null`.

`POST /api/search-tabs/{id}/jobs` accepts `SearchSpec` and returns the existing
search-job status contract (202). Progress and cancellation still use
`/api/search-jobs/{job_id}`. Every tab shares the same scheduler: one running
pipeline, at most 16 pending/running jobs in total, one unfinished job per tab.
Successful publication pins the result to its originating tab before reporting
completion. A failed or cancelled run preserves the previous completed snapshot.
On restart, unfinished tabs become `interrupted` with no automatic resubmission.

`DELETE /api/search-tabs/{id}` cancels only that tab's job, removes its durable
reference, and selects a neighboring tab if needed. Late completion cannot
recreate a deleted tab. A snapshot is collected once neither a tab, a live job,
history, nor favorites retain it. Tabs can retain results older than the ten-entry
recent history without enlarging that history. Ranked snapshots can be shared;
browse snapshots opened in another tab get separate bounded windows/cursors.

`POST /api/search-tabs/{id}/favorite` accepts `{"favorite": true|false}` and returns
snapshot metadata. It refers to the performed search, even if the draft was
edited or its recent-history record has already been evicted. It can restore that
record as a favorite without adding it to the last ten completed searches.

All mutations require the existing local-session token and origin checks.
Switching workspaces switches tab storage. Archive changes, model or retrieval
identity changes, rebuilding or clearing an index, and cache cleanup invalidate
tab results through the same revision checks as search history. Ordinary indexing
progress retains an existing completed snapshot. Drafts and explicit favorites
remain. Media files are not copied into snapshots.
