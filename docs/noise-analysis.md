# Statistical noise analysis: implementation checkpoint

[Русский](noise-analysis.ru.md)

The CPU analysis service now collects a bounded, private report for one chat.
It **does not change** chunking, source messages, lexical search, existing
policies or indexing checkpoints. A complete analysis/apply UI and CLI, real-token
dry-run, calibration and frozen exact policies are still being implemented.
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

Remaining: labelled development/test calibration and conservative gate, immutable
exact effective policies, overrides, borrowed-context protection, atomic explicit
text-only apply/rebuild, real-encoder token/padding dry-run and shared UI/CLI.
OCR and visual indexes remain outside this text filter. No performance or
retrieval-quality improvement is claimed before measurement.

## Exact-policy foundation (not an apply workflow)

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

This is an internal compiler/runtime foundation with synthetic tests. No analysis
report can yet activate it, and no user's existing policy is switched on startup.
The future apply action must check report integrity, scope/revision, overrides and
idempotence atomically before an explicit text-only rebuild.
