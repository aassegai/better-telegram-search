# Noise analysis cost and offline quality diagnostics

[Русский](noise-evaluation.ru.md) · [Feature contract](noise-analysis.md)

These tools do not change rules, rebuild indexes or enable a new omission gate.
They need the existing uv environment, without models, Torch, GPU or network.

## Reproduce the CPU benchmark

```sh
uv run --no-sync python scripts/benchmark_noise.py
```

The generated-only [artifact](benchmarks/noise-synthetic-cpu.json) records four
workloads, in separate subprocesses and temporary workspaces. Every vocabulary
entry occurs before later phrase rows; inputs and non-timing counters are
deterministic. No archive path is accepted. The benchmark uses the actual import,
statistics, candidate/scoring and private-report services.

| Messages | Unique whole forms | First analysis, s | Repeated analysis, s | First sampled process RSS, MiB |
| ---: | ---: | ---: | ---: | ---: |
| 10,000 | 32 | 1.990 | 2.038 | 99.3 |
| 100,000 | 32 | 10.755 | 10.991 | 100.7 |
| 100,000 | 10,000 | 15.045 | 15.058 | 100.6 |
| 100,000 | 50,000 | 26.421 | 26.853 | 102.2 |

All four runs have full source coverage; first/repeated counters match. Some
hypothesis lists remain limited, as expected. The reference has 39,532 forms and
359,130 useful-index postings; approved seeds have 177 postings. A separate
512-form shared-signature probe examines at most 256 candidates per index/query
and marks incomplete evidence. Million-character and repeated-block inputs are
rejected before candidate comparisons; their measured costs are in the artifact.

“First” means the first analysis in a new subprocess **after import**, with OS
caches left intact. Both passes rebuild their candidate index. Wall time includes
admission, report publication and report-counter collection; reported thread CPU
time is sampled by the analyzer before terminal publication. RSS is sampled for
the whole process during analysis, excluding setup/import peaks. It is not an
isolated allocator measurement. Generated high-cardinality words have sparse
matches; the dense probe is separate. These results do not predict a real chat's
latency, omission quality, embedding throughput or GPU speedup.

The command can replace a report bearing its matching version marker. It refuses
other existing files, symlinks/junctions and arbitrary public destinations. Its
public output is fixed to the artifact above; custom outputs must be in ignored
`workspace/` or `plans/`. Publication uses an atomic temporary-file replacement.

## Run a diagnostic evaluation

```sh
uv run --no-sync python scripts/evaluate_noise.py \
  tests/fixtures/synthetic/noise-evaluation.json \
  --output workspace/evaluation/synthetic-noise.json
```

The fixture and its supplied rankings are invented examples that test metric
arithmetic. They are **not** human-labelled Telegram data or an observed retrieval
improvement. Default JSON output contains aggregates, public reference/version
metadata and limitations; it omits forms, contexts, scope/query/document IDs and
paths. Keep private annotation files and reports in ignored `workspace/` or
`plans/`; the exact public synthetic fixture is the only other permitted input.
Do not publish private annotations or run them as public CI input.

The parser accepts regular JSON files up to 16 MiB, at most 10,000 cases, 64 scopes
and 100,000 total frequency records (50,000 per scope). It rejects duplicate keys,
NaN/Infinity, unsupported fields, unsafe paths and in-place/unrecognized-file
overwrites. The default time limit is 300 seconds, maximum 3,600. Computation and
retrieval loops check it, including immediately before atomic publication.

## Annotation schema, version 1

Use the public fixture as the complete machine-readable template:

- `kind`: `synthetic` or `annotated`; `label_source`: respectively `synthetic` or
  `human_reviewed`. The latter is a declaration, not verified human provenance.
- `partition_by`: must include `family`; optionally also `scope` and `period`.
- `scopes`: opaque `id` and normalized whole-form `frequencies`. Each record has
  nonnegative integer `whole`, `eligible` and `phrase` counts; eligible cannot
  exceed whole. These are corpus priors, not counts of labelled sample rows.
- `cases`: unique `id`, `scope`, `family`, `period`, `split` (`development`/`test`),
  original `text`, gold `label` (`keep`/`noise`/`uncertain`), boolean
  `has_attachment`/`protected`, and original `parent_text` (empty if absent).
  Text/parent limits are 16,384 characters. Labels describe individual occurrences;
  keep useful captions, code and meaningful answers protected in the gold set.

Declared groups cannot cross partitions. Duplicate canonical whole forms and
NFKC/casefold/whitespace-normalized phrases are forbidden across partitions, even
under renamed groups. Derived noise-seed overlap is also rejected. The report
separately shows whether scope and period are disjoint. Validation cannot prove
that supplied counts, labels or group identifiers came from the claimed corpus.

Cases are checked with the actual scorer and exact-policy protections. Existing
approved-seed decisions are computed independently of candidate input limits;
source-limited occurrences are counted explicitly. They cannot validate a policy
compiled from an incomplete source. Frequency, absence from the reference and a
nearby seed never turn a new form into an automatic omission here.

## Metrics and limits of interpretation

Four ablations retain exact seeds, seeds plus one-edit candidates, seeds plus
repetitions, or all candidates. Mechanisms overlap: a doubled letter can be both
an insertion and a lengthening. These are **candidate-stage** ablations with the
novel-form gate off, not four newly activated removal policies.

Per split, the report includes omission precision on decisive labels, precision
treating uncertain omissions as errors, useful false-omission rate, noise recall,
review rate, novel-noise candidate coverage, and protected/incomplete/source-limit
counts. Zero denominators produce `null`. Occurrences may be correlated; these
descriptive fractions are not independent-sample confidence bounds.

Optional `retrieval` data supplies graded judgments (0–3) and before/after rankings
of stable canonical message IDs. All ranked IDs must be judged, with no duplicates.
Declared source, embedding-space and filter hashes must match before/after;
policies may differ. Query families cannot cross splits or conflict with case
families. Up to 1,000 queries and 100 results/query are supported. Recall and
nDCG at 1/5/10 use macro averages over queries with positive judgments; unscorable
queries are counted. DCG uses `(2^grade - 1) / log2(rank + 1)`. Rankings and their
provenance are supplied, **not re-executed or independently verified**.

Every report remains `calibration_status=not_validated` and
`new_auto_omit_enabled=false`, even with declared human labels. No thresholds are
selected or installed. A real conservative gate still needs preregistered
parameters, independent reviewed data and controlled retrieval measurements.
