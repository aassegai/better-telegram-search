# Statistical noise analysis: implementation checkpoint

[Русский](noise-analysis.ru.md)

The first internal stage generates evidence for a future per-chat noise policy.
It does **not** change chunking, indexing, lexical search or existing policies.
There is no user-facing analysis/apply flow yet. Automatic omission is not enabled.

`search/chunking/noise/` contains immutable candidate DTOs, bounded normalization
and indexed matching. Observed whole-message forms are compared only with approved
seeds and a caller-supplied useful lexicon. The matcher covers one insertion,
deletion, substitution or adjacent transposition; it does not implement distance
two or unrestricted Damerau–Levenshtein. Direct run-length expansion, repeated
blocks of up to four characters and an alternating Russian laughter family are
separate hypotheses, with no false claim that they are one edit away.

Every accepted input retains an unchanged hypothesis. For the synthetic `бял`,
`бля` supplies a noise transposition hypothesis and a supplied `был` supplies a
competing useful substitution hypothesis. No score or probability has been chosen.
Known words, uppercase/titlecase, numbers and mixed scripts remain evidence for
the later protection/scoring stage. Candidate proximity is never an omit decision.
No proposed form becomes a seed through a transitive similarity chain.

The caller must supply a suitable versioned useful-word lexicon before calibration;
this stage ships no new dictionary or dependency. Absence of that lexicon is
explicit in the report. No files, models, SQL databases or network are opened.

Default limits per index: 50,000 input records, 500,000 index postings, canonical
forms up to 64 characters and raw inputs up to 512 characters. Duplicate records
also spend the input limit. At most 256 candidate forms are compared per index;
truncation is explicit and deterministic and must prevent automatic omission.
Construction and lookup accept a cooperative cancellation callback. Reports
separate comparisons, signature lookups and postings read; these are measured
operations, not quality or speed claims.

Verification uses public synthetic examples and a small exhaustive independent
edit-distance reference. A frozen v2 policy JSON and its SHA-256 protect old
serialization/checkpoints. These tests do not validate an automatic omission gate
on real messages.

Remaining stages: bounded per-chat statistics with exclusion/revision fences,
competing scores and labelled calibration, frozen effective exact policies,
borrowed-context protections, atomic explicit apply/rebuild, real-token dry-run,
and a shared CLI/API/UI flow. Analysis must not mutate the live policy before
explicit application. OCR and visual indexes remain outside this filter.
