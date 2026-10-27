# Statistical noise analysis: implementation checkpoint

[Русский](noise-analysis.ru.md)

[Reproducible CPU benchmark and offline evaluation](noise-evaluation.md)

The CPU analysis service now collects a bounded, private report for one chat.
Source messages and keyword search are always preserved. Analysis leaves active
rules and indexing checkpoints unchanged; explicit apply rebuilds only text.
The local API, per-chat settings UI and standalone CLI share the draft, override
and apply services. Real-token dry-run is available in the standalone CLI and
as a cancellable background job in chat settings. Calibration remains pending.
**Automatic omission of new forms is disabled.** New ambiguous forms stay unless
you explicitly choose an omit rule; previously selected manual rules are retained.

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
| `POST /{id}/dry-runs` | `202`, CPU comparison with draft revision and optional message/time limits |
| `GET /{id}/dry-runs` | Latest comparison or `null`; no scan/model preparation |
| `GET /{id}/dry-runs/{comparison_id}` | Scoped state, progress and measured result |
| `POST /{id}/dry-runs/{comparison_id}/cancel` | Cooperative cancellation, including stale work and vector maintenance |

Defaults: `max_messages=100000`, `max_forms=50000`, `time_limit_seconds=300`.
Allowed maxima: one million messages, 50,000 forms in each whole/phrase vocabulary,
one hour. Pages contain at most 100 entries. At most eight reports are saved,
with one active job per chat and one CPU analysis worker across the workspace.
A report is capped at 128 MiB. Delete unused reports before starting a ninth.
Source, candidate, detail and time limits are explicit. Source/time limits block
application. Bounded hypothesis lists never authorize automatic omission of new
forms. Timing and sampled RSS are measured costs, not claimed GPU savings.

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

Remaining: labelled development/test calibration, CPU corpus benchmarks and the
validated conservative automatic gate.
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

A current report with full source coverage and a stored SHA-256 is required for
preview, override changes and apply. Older reports remain reviewable; re-run
analysis when they lack the required metadata. Source-limited reports cannot be
applied. No policy changes on application startup.

The versioned `source-and-evidence-v1` summary separates `source_complete` from
`evidence_complete`. `complete` retains its strict meaning: any limit makes it
false. When every message/form was collected but a hypothesis/detail list is
bounded (`candidate_limit`/`payload_limit`), the job can be `ready` with
`policy_applicable=true`. `limited_evidence_forms` counts affected forms. This
permits only approved seeds and explicit overrides, with the new automatic gate
off. Ordinary short words such as `да` can have over 16 useful hypotheses; their
limited display must not block exact rules for an otherwise fully scanned chat.
Scores remain uncalibrated. A candidate search that stopped before finding a
match is reviewable, not evidence that no noise hypothesis exists.

Message/form/phrase/text/time limits still prevent application. Unknown limit or
coverage versions, contradictory flags and versionless hybrid summaries fail
closed. Old full checksum-bearing reports remain compatible; old partial reports
are never silently upgraded. The UI explains when the source is complete but
evidence is limited, and why an incompatible report requires a new analysis.

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
CLI uses the API's analysis/policy services without loading inference models or a
web server. The explicit dry-run loads only an already downloaded tokenizer.

```sh
uv run telegram-search --workspace ./workspace noise analyze --chat-id CHAT_ID
uv run telegram-search --workspace ./workspace noise list --chat-id CHAT_ID
uv run telegram-search --workspace ./workspace noise status --chat-id CHAT_ID --job-id JOB_ID
uv run telegram-search --workspace ./workspace noise override --chat-id CHAT_ID --job-id JOB_ID --revision 0 --keep FORM
uv run telegram-search --workspace ./workspace noise preview --chat-id CHAT_ID --job-id JOB_ID --revision 1
uv run telegram-search --workspace ./workspace noise apply --chat-id CHAT_ID --job-id JOB_ID --revision 1
```

`analyze` waits for completion; Ctrl+C cancels and joins its worker before releasing
the lease. Analysis limits have the same names/defaults as the API. A source-partial
report is useful for review but cannot be applied. A failed/stale analysis exits
with code 1. `override` supports repeated `--keep`, `--omit`, and `--clear` options;
keep wins if the same form is passed to both keep and omit.

Default JSON output contains aggregates, revisions and opaque job IDs, never chat
names/IDs, author identities, source paths or forms. To intentionally inspect forms,
use `noise report --chat-id CHAT_ID --job-id JOB_ID --show-private`, with optional
`--kind forms|families`, `--decision keep|review|omit`, `--offset` and `--limit`.
`noise delete --chat-id CHAT_ID --job-id JOB_ID` removes a stopped report.

## Real-token comparison (standalone CLI)

After saving overrides, compare the frozen proposal without applying it:

```sh
uv run telegram-search --workspace ./workspace noise dry-run --chat-id CHAT_ID --job-id JOB_ID --revision 1 --max-messages 100000 --time-limit-seconds 300
```

The command requires a current source-complete analysis and the active text model's local
tokenizer. It verifies both tokenizer files against the pinned model manifest,
the active space identity and the installed tokenizer version. Missing or
incompatible files cause a fixed error; nothing is downloaded, and no ONNX
weights, inference sessions or GPU are loaded. As with the other standalone
commands, stop the application first. Ctrl+C releases the workspace lease.

Two streaming CPU passes use the actual historical/current and proposed chunk
builders. Counts include prefixes, special tokens, author labels, borrowed
context, overlap and long-message splitting. Ordinal batches flush at each UTC
day. `batch_basis` explicitly fixes the configured batch size: runtime OOM backoff
and future settings changes cannot be predicted offline. Token/padding reductions
are **not** elapsed-time or GPU-speed estimates.

`before` and `after` contain chunks, tokens, batches, padded slots, padding,
distinct own messages and the worker's source-minus-own skipped count. Repeated
overlap/split parts count once; borrowed context never becomes an own message.
`noise_only_chunks` means all model parts are exact approved/proposed noise forms,
without borrowed context; it does not mean that these vectors can safely be
deleted. Classification counters are unavailable for legacy windows, as indicated
by `classification_available`. Review and protected occurrences are descriptive,
not an additional skipped total.

`delta` is **after minus before**, plus removed/restored distinct own messages.
A message cap compares the same chronological prefix in both passes, with
`comparison_complete=true`, `complete=false`, `limits=["message_limit"]`.
Time or input-length limits can stop a pass; then there is no delta. Oversized
text/author/ID/metadata stops measurement instead of silently truncating input.
Context can lie outside the measured prefix, as in normal indexing; sender
exclusions still apply to every lookup.
Any source, exclusions, draft, model or configured batch change rejects a completed
comparison. Main DB reads use short keyset pages; CPU work holds no archive
connection, lifecycle lock or write transaction. Distinct IDs use a separate
unnamed temporary SQLite database capped at 128 MiB, with a bounded context cache.

Output contains aggregates and hashes only, including elapsed and thread CPU
seconds, sampled **whole-process** peak RSS and baseline. No raw message, author,
chat ID or source path is printed. The immutable analysis report, active policy,
indexing queues/checkpoints and original messages are unchanged. The command does
not persist a comparison. The application's background API/UI stores one latest
result per analysis, separately from its immutable report.

## Background comparison in chat settings

After saving overrides, use **Compare chunks and tokens** in the noise panel.
Limits default to 100,000 source messages and 300 seconds; they are independent
of analysis limits. The first pass has indeterminate progress with measured
message counts. The second pass can use the first pass's source count. No ETA is
invented. Cancellation leaves the index and current rules unchanged.

The result compares chunks, tokens and distinct own messages; measurement details
include padding, skipped/protected counts, CPU time and process RSS. Limited
prefixes and unfinished passes are explicitly marked. A delta is shown only for
a completed, source-verified comparison, including a clearly labelled limited
prefix. Save new overrides before rerunning; comparisons do not apply rules.

Schema 20 adds a separate `noise_dry_runs` table. At most one latest comparison
is retained per saved analysis (at most eight). A replacement removes only the
previous stopped comparison. One CPU comparison worker owns at most eight tasks;
the analysis worker has the same independent owner limit. Limits hold even if
chat deletion cascades metadata away. Active comparisons protect parent report
deletion and orphan pruning until they release it. Status reads hold no lifecycle
lock or private report/tokenizer file. Temporary status errors retain a known
task's Cancel action and keep parent deletion protected.

Admission captures the parent analysis, draft revision, source fence, active
embedding space and configured batch size. The worker verifies these before and
after local tokenizer loading and before publishing in a short transaction.
Changed scopes hide old results; stale active work remains cancellable. Startup
marks abandoned jobs interrupted instead of restarting the comparison. The shared
terminal publisher handles SQLite BUSY without losing the worker, and makes only
one guarded failure-write attempt after a permanent error. If persistence still
fails, admission stops and restart is required; no result is exposed as durable.
No ONNX weights, GPU sessions or downloads are started by this comparison.

## Chat settings

Open a chat's indexing settings and expand **Semantic noise filter**. Run analysis
with the current sender exclusions; limits are editable and the UI defaults to
one million messages and 300 seconds. Progress shows measured phase/counts and
elapsed time, with cancellation, without an invented completion estimate.

Review paginated forms or families. Each form separates the analysis decision
from the draft rule and any previously selected rule. An ambiguous new suggestion
can therefore retain an earlier explicit omission. A new local override wins;
choosing **No new override** restores the inherited/default rule. Captions,
entities and meaningful replies retain per-occurrence protections.

Optional edits are preserved across polling and pages. Save overrides, then
preview the rules and explicitly apply/rebuild text. The preview belongs to one
draft revision: refreshing a changed draft invalidates it, and Apply cannot submit
a newer unreviewed revision. Preview totals include inherited rules. No repeated
rebuild runs when effective rules are unchanged.

Source-partial and older checksum-free reports are read-only. Source/exclusion changes
hide old evidence and require a new analysis. Closing settings or changing chat,
policy or sender scope cannot publish a late response into a newer view. The UI
is available in Russian/English and both themes. Forms are shown literally and
never passed through application-label translation.
