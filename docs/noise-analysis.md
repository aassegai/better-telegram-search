# Statistical noise analysis: implementation checkpoint

[Русский](noise-analysis.ru.md)

The CPU analysis service now collects a bounded, private report for one chat.
It **does not change** chunking, source messages, lexical search, existing
policies or indexing checkpoints until an explicit apply action. The local API
now supports exact-policy drafts, overrides and text-only apply/rebuild. A complete
UI, real-token dry-run and calibration are still being implemented.
**Automatic omission of new forms is disabled.** Doubtful forms stay.

## Evidence and decisions

`search/chunking/noise/` separates whole-message normalization, indexed candidate
matching, statistics, lexicon loading and scoring. It covers one insertion,
deletion, substitution or adjacent transposition; direct letter-run expansion,
repeated blocks of up to four characters and alternating Russian laughter have
separate operation labels. There is no distance-two, transliteration, layout
correction or transitive expansion of seeds. Every form retains an unchanged
hypothesis; synthetic `бял` can compete with both `бля` and `был`.

Counts separate whole-message occurrences, occurrences eligible for filtering,
phrase evidence, distinct days and salted author counts. Reports contain no full
messages, neighboring text, author names or author IDs. Attachment captions,
structured links/code/mentions and replies to questions are protected per
occurrence. New forms also preserve case, number, mixed-script and compound-term
signals. Excluded senders are excluded from both observations and parent lookup.

The current evidence score is `log1p(whole_count + phrase_count)` with equal
error-operation weights. It is **uncalibrated, not a probability**. Useful and
unchanged competitors and the score margin remain visible. Known forms and
unknown forms without a noise hypothesis stay; novel ambiguous forms go to
review. Only the pre-existing approved exact seeds can be labelled omit, and
protected occurrences still stay. Analysis never activates these labels.
Family counts are descriptive and cannot decide the fate of individual variants.

## Local API

All routes are scoped to `/api/chats/{chat_id}/noise-analyses` and retain the
application's Host/Origin/local-session guards. Mutations require
`X-Session-Token` from `/api/session`.

| Request | Result |
| --- | --- |
| `POST` with optional limits below | `202`, job ID and progress |
| `GET` | At most eight saved jobs |
| `GET /{id}` | State, elapsed time, bounded progress and summary |
| `POST /{id}/cancel` | Cooperative cancellation, also during vector maintenance |
| `GET /{id}/forms?decision=review&offset=0&limit=50` | Paginated forms, evidence and protections |
| `GET /{id}/families?offset=0&limit=50` | Descriptive family aggregates |
| `DELETE /{id}` | Remove a stopped job and its owned private report |
| `GET /{id}/draft` | Lightweight override/revision/application metadata |
| `PATCH /{id}/draft` | Replace overrides with `{ "revision": 0, "overrides": { "бял": "keep" } }` |
| `POST /{id}/preview` | Explicit policy preview with `{ "revision": 1 }` |
| `POST /{id}/apply` | Explicit text rebuild with `{ "revision": 1 }` |

Defaults: `max_messages=100000`, `max_forms=50000`, `time_limit_seconds=300`.
Allowed maxima: one million messages, 50,000 forms in each whole/phrase vocabulary,
one hour. Pages contain at most 100 entries. At most eight reports are saved,
with one active job per chat and one CPU analysis worker across the workspace.
A report is capped at 128 MiB. Delete unused reports before starting a ninth.
Partial coverage/candidate/size/time limits are explicit; they never authorize
omission. Timing and sampled RSS are measured costs, not claimed GPU savings.

Short canonical reads use timestamp/message-ID keysets. Text processing runs
outside archive connections and write/lifecycle locks. Detailed private reports
live in `workspace/data/noise-reports/`; only small job metadata uses the main DB.
A busy main writer cannot kill the worker. Ready is published after durable
terminal persistence. Permanent SQL errors get one guarded failure-write attempt;
if that also fails, saved jobs show a fixed failure, new analysis admission stops
and an application restart is required. There is no endless terminal retry.
Cancel and source changes discard applicability; interrupted
jobs are not automatically resumed on restart. Polling and `/api/doctor` do not
start a scan or download models.

The report is fenced by chat revision, exclusions, policy and the archive epoch.
Reading it after a source/filter change is rejected. The epoch is deliberately
conservative: a mutation in another chat can also stale the report. Report paths
are owned by the service, never supplied by the client; symlinks/junctions,
wrong identity/version, oversized or malformed reports are rejected.

## Pinned useful-word reference and attribution

The bundled reference contains 39,532 normalized whole forms selected from the
first 20,000 Russian and 20,000 English entries of
[wordfreq 3.1.1](https://pypi.org/project/wordfreq/3.1.1/), by Robyn Speer. It is a
limited frequency reference, **not a spelling oracle**; absence is never evidence
of noise. The 757,479-byte JSON embeds upstream source credits and licence notices,
source data hashes, normalization and selection changes. Derived data is provided
under [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/); wordfreq code
is Apache 2.0. Embedded credits include the upstream corpus sources and freely
available SUBTLEX data by Marc Brysbaert et al. Preserve this attribution when
redistributing the derived data. The JSON is included in native bundles.

Reproduce the exact artifact with the separate locked build environment:

```sh
uv run --locked --project tools/noise-lexicon python tools/noise-lexicon/prepare.py
```

Expected SHA-256: `a296a46493fbfbb2475742f3abbb7bd05ca1743d059f293acf34b0761a3ed602`.
Runtime validates that hash. The application does not depend on or import wordfreq;
its build-only environment is ignored. Runtime makes no network requests here.

## Verification and remaining stages

Synthetic tests cover independent edit-distance matching, protections, competing
scores, rare forms, exclusion/revision fences, bounded pagination, cancellation
in each CPU phase, SQLite contention, report validation and migration from schema
17. A frozen v2 policy JSON/hash protects existing checkpoints. No real archive is
used in these tests, and synthetic tests do not validate a real omission gate.

Remaining: labelled development/test calibration and a conservative gate,
real-encoder token/padding dry-run and the UI.
OCR and visual indexes remain outside this text filter. No performance or
retrieval-quality improvement is claimed before measurement.

## Frozen exact policy and explicit apply

A separate `NoisePolicy` format now composes the unchanged v2 episode policy with
canonical exact omit/keep sets and a versioned protection contract. Its cached
JSON/hash contains effective rules only. Manual keep wins, including for an
existing seed. Corpus counts, timestamps and analysis IDs cannot alter identity.
Legacy/v2 JSON and checkpoints keep their old identity and borrowed-context path.

For the new format only, borrowed parents pass the same protections as owned
messages. An omitted parent still remains a dependency; changing it invalidates
the owning segment and can make it useful again. A bounded extra lookup protects
a borrowed answer to a question without recursive classification. Import and
model rebuild retain an explicitly selected new policy, pauses and other indexes.

A full, current report with a stored SHA-256 is required for preview, override
changes and apply. Older reports remain reviewable; re-run analysis to apply them.
Limited reports cannot be applied. No policy changes on application startup.

At most 1,000 canonical observed forms may have `keep`/`omit` overrides. Save uses
a draft revision; stale edits return `409`. Preview compiles effective rules
without applying them. Report hashing and compilation hold no main write or
lifecycle lock. Apply rechecks source/draft revisions inside a short transaction,
saves an immutable policy and schedules all chat days with set-based SQL. Existing
pauses, batches, devices, OCR and visual indexes are preserved.

Manual decisions from the currently selected policy survive re-analysis unless
explicitly overridden. Unchanged effective rules do not rebuild chunks or change
query snapshots, regardless of changed statistics. Re-applying the same applied
draft is a no-op even after a later import. No per-form confirmation is mandatory;
review forms stay automatically, while manual overrides are optional. This API
workflow does not validate a new automatic omission gate or claim GPU savings.

## Standalone CLI

Stop the application before using these commands. They acquire the same workspace
writer lease, so a second process cannot recover a live application's jobs. The
CLI uses the API's analysis/policy services without loading models or a web server.

```sh
uv run telegram-search --workspace ./workspace noise analyze --chat-id CHAT_ID
uv run telegram-search --workspace ./workspace noise list --chat-id CHAT_ID
uv run telegram-search --workspace ./workspace noise status --chat-id CHAT_ID --job-id JOB_ID
uv run telegram-search --workspace ./workspace noise override --chat-id CHAT_ID --job-id JOB_ID --revision 0 --keep FORM
uv run telegram-search --workspace ./workspace noise preview --chat-id CHAT_ID --job-id JOB_ID --revision 1
uv run telegram-search --workspace ./workspace noise apply --chat-id CHAT_ID --job-id JOB_ID --revision 1
```

`analyze` waits for completion; Ctrl+C cancels and joins its worker before releasing
the lease. Analysis limits have the same names/defaults as the API. A partial
report is useful for review but cannot be applied. A failed/stale analysis exits
with code 1. `override` supports repeated `--keep`, `--omit`, and `--clear` options;
keep wins if the same form is passed to both keep and omit.

Default JSON output contains aggregates, revisions and opaque job IDs, never chat
names/IDs, author identities, source paths or forms. To intentionally inspect forms,
use `noise report --chat-id CHAT_ID --job-id JOB_ID --show-private`, with optional
`--kind forms|families`, `--decision keep|review|omit`, `--offset` and `--limit`.
`noise delete --chat-id CHAT_ID --job-id JOB_ID` removes a stopped report.
