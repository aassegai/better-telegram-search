"""Descriptive offline metrics, never a calibrated gate or runtime policy update."""

import math
from dataclasses import replace

from telegram_search.evaluation.noise_schema import MODES, SPLITS, require, validate_dataset
from telegram_search.search.chunking.classification import classify, whole_word
from telegram_search.search.chunking.legacy import SourceMessage
from telegram_search.search.chunking.noise.candidates import CandidateGenerator, one_edit
from telegram_search.search.chunking.noise.compile import compile_policy
from telegram_search.search.chunking.noise.lexicon import load_lexicon
from telegram_search.search.chunking.noise.normalization import (
    is_lengthening,
    laughter_seed,
    repeated_seed,
)
from telegram_search.search.chunking.noise.scoring import SCORE_VERSION, evaluate
from telegram_search.search.chunking.noise.statistics import collect_batch
from telegram_search.search.chunking.policy import APPROVED_FILLER, DEFAULT_POLICY


def ablation(evidence, mode):
    if mode == "full_candidates" or evidence.form is None:
        return evidence

    def included(item):
        observed, seed = evidence.form, item.form
        if observed == seed:
            return True
        if mode == "seeds_plus_typos":
            return one_edit(observed, seed) is not None
        if mode == "seeds_plus_repetitions":
            return (
                is_lengthening(observed, seed)
                or repeated_seed(observed, {seed}) == seed
                or laughter_seed(observed, {seed}) == seed
            )
        return False

    return replace(evidence, noise=tuple(item for item in evidence.noise if included(item)))


def ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def decision_metrics(rows):
    total = len(rows)
    noise = sum(row["gold"] == "noise" for row in rows)
    useful = sum(row["gold"] == "keep" for row in rows)
    omit = sum(row["decision"] == "omit" for row in rows)
    true_omit = sum(row["decision"] == "omit" and row["gold"] == "noise" for row in rows)
    false_omit = sum(row["decision"] == "omit" and row["gold"] == "keep" for row in rows)
    uncertain_omit = sum(row["decision"] == "omit" and row["gold"] == "uncertain" for row in rows)
    novel_noise = sum(row["gold"] == "noise" and not row["seed"] for row in rows)
    novel_candidates = sum(
        row["gold"] == "noise" and not row["seed"] and row["candidate"] for row in rows
    )
    return {
        "labelled_occurrences": total,
        "gold_noise": noise,
        "gold_useful": useful,
        "gold_uncertain": total - noise - useful,
        "automatic_omissions": omit,
        "true_omissions": true_omit,
        "false_useful_omissions": false_omit,
        "uncertain_omissions": uncertain_omit,
        "omission_precision_over_decisive_labels": ratio(true_omit, true_omit + false_omit),
        "precision_treating_uncertain_as_errors": ratio(true_omit, omit),
        "useful_false_omission_rate": ratio(false_omit, useful),
        "noise_omission_recall": ratio(true_omit, noise),
        "review_rate": ratio(sum(row["decision"] == "review" for row in rows), total),
        "novel_noise_candidate_coverage": ratio(novel_candidates, novel_noise),
        "novel_automatic_omissions": sum(
            row["decision"] == "omit" and not row["seed"] for row in rows
        ),
        "protected_occurrences": sum(row["protected"] for row in rows),
        "incomplete_evidence_occurrences": sum(not row["complete"] for row in rows),
        "source_limited_occurrences": sum(row["source_limited"] for row in rows),
    }


def retrieval_metrics(retrieval, cutoffs, check=lambda: None):
    if retrieval is None:
        return None
    output = {"provenance": "supplied_rankings_only_not_reexecuted", "splits": {}}
    for split in sorted(SPLITS):
        queries = [query for query in retrieval["queries"] if query["split"] == split]
        metrics = {}
        for cutoff in cutoffs:
            check()
            values = {name: {"recall": [], "ndcg": []} for name in ("before", "after")}
            skipped = 0
            for query in queries:
                check()
                relevance = query["relevance"]
                positives = sum(grade > 0 for grade in relevance.values())
                if not positives:
                    skipped += 1
                    continue
                ideal = sum(
                    (2**grade - 1) / math.log2(rank + 2)
                    for rank, grade in enumerate(sorted(relevance.values(), reverse=True)[:cutoff])
                )
                for name in values:
                    ranking = query[name][:cutoff]
                    values[name]["recall"].append(
                        sum(relevance[doc] > 0 for doc in ranking) / positives
                    )
                    dcg = sum(
                        (2 ** relevance[doc] - 1) / math.log2(rank + 2)
                        for rank, doc in enumerate(ranking)
                    )
                    values[name]["ndcg"].append(dcg / ideal)
            result = {}
            for name, measured in values.items():
                result[name] = {
                    metric: sum(items) / len(items) if items else None
                    for metric, items in measured.items()
                }
            result["delta"] = {
                metric: result["after"][metric] - result["before"][metric]
                if result["before"][metric] is not None
                else None
                for metric in ("recall", "ndcg")
            }
            result["eligible_queries"] = len(queries) - skipped
            result["queries_without_positive_judgments"] = skipped
            metrics[str(cutoff)] = result
        output["splits"][split] = metrics
    return output


def evaluate_dataset(data, *, cutoffs=(1, 5, 10), generator=None, check=lambda: None):
    require(isinstance(cutoffs, (tuple, list)) and 1 <= len(cutoffs) <= 10)
    require(
        all(type(value) is int and 1 <= value <= 100 for value in cutoffs)
        and len(set(cutoffs)) == len(cutoffs)
    )
    scopes = validate_dataset(data, check)
    if generator is None:
        lexicon = load_lexicon(check)
        generator = CandidateGenerator(useful_words=lexicon.forms, check=check)
        reference = {"id": lexicon.id, "sha256": lexicon.sha256}
    else:
        reference = {"id": "provided_generator_not_pinned", "sha256": None}
    results = {mode: {split: [] for split in SPLITS} for mode in MODES}
    seed_splits = {}
    for case in data["cases"]:
        check()
        form = whole_word(case["text"])
        evidence = generator.analyze(
            form if form is not None and len(form) <= 64 else case["text"], check=check
        )
        source_limited = bool(
            collect_batch(
                [
                    {
                        "text": case["text"],
                        "has_photo": False,
                        "has_attachment": case["has_attachment"],
                        "protected": case["protected"],
                        "parent_question": "?" in case["parent_text"],
                        "day": "evaluation",
                        "author_id": "evaluation",
                    }
                ],
                "evaluation",
                check,
            ).limited_messages
        )
        # Validate derived candidate-family overlap as well as caller-declared
        # groups. Renaming the same seed's family cannot fake held-out quality.
        for hypothesis in evidence.noise:
            require(seed_splits.setdefault(hypothesis.form, case["split"]) == case["split"])
        source = SourceMessage(
            "evaluation",
            2,
            100,
            "",
            case["text"],
            has_attachment=case["has_attachment"],
            protected=case["protected"],
        )
        parent = (
            SourceMessage("evaluation", 1, 99, "", case["parent_text"])
            if case["parent_text"]
            else None
        )
        protected = False
        counts = scopes[case["scope"]]
        if form is not None and len(form) <= 64:
            proposed = compile_policy(DEFAULT_POLICY, omit=(form,))
            eligible = not classify(source, proposed, parent).include
            protected = not eligible
            require(not eligible or counts[form]["eligible"] > 0)
        for mode in MODES:
            filtered = ablation(evidence, mode)
            baseline = classify(source, DEFAULT_POLICY, parent)
            if baseline.role == "filler" and not baseline.include:
                decision, complete = "omit", evidence.complete
            elif evidence.form is None:
                decision = "keep"
                complete = evidence.complete
            else:

                def frequency(value, counts=counts):
                    count = counts.get(value, {})
                    return count.get("whole", 0) + count.get("phrase", 0)

                payload = evaluate(
                    filtered, {"eligible_count": counts[form]["eligible"]}, frequency
                )
                decision, complete = payload["decision"], payload["complete"]
            # Corpus-level decisions never override per-occurrence protection.
            if protected:
                decision = "keep"
            # This evaluator mirrors the deployed OFF gate; candidate ablations
            # cannot silently turn a new typo/repetition into an omission.
            require(decision != "omit" or form in APPROVED_FILLER)
            results[mode][case["split"]].append(
                {
                    "gold": case["label"],
                    "decision": decision,
                    "candidate": bool(filtered.noise),
                    "seed": form in APPROVED_FILLER,
                    "protected": protected,
                    "complete": complete,
                    "source_limited": source_limited,
                }
            )
    partition = {}
    for field in ("family", "scope", "period"):
        groups = {
            split: {case[field] for case in data["cases"] if case["split"] == split}
            for split in SPLITS
        }
        partition[field + "_disjoint"] = not (groups["development"] & groups["test"])
    return {
        "schema_version": 1,
        "evaluation_version": "noise-diagnostics-v1",
        "dataset_kind": data["kind"],
        "label_source": data["label_source"],
        "label_source_verification": "declared_only_not_independently_verified",
        "score_version": SCORE_VERSION,
        "reference": reference,
        "partition": partition,
        "calibration_status": "not_validated",
        "new_auto_omit_enabled": False,
        "ablation_semantics": "candidate_stage_only_with_novel_auto_gate_off",
        "decisions": {
            mode: {split: decision_metrics(rows) for split, rows in values.items()}
            for mode, values in results.items()
        },
        "retrieval": retrieval_metrics(data.get("retrieval"), sorted(cutoffs), check),
        "limitations": [
            "Descriptive occurrence metrics, not independent-sample confidence bounds.",
            "Uncertain labels are excluded from decisive-label precision and reported separately.",
            "Synthetic annotations cannot validate a real omission gate.",
            "Source-limited cases use existing exact-policy behavior, not a compilable new gate.",
            "Supplied rankings are checked for matching declared source/model/filter snapshots; "
            "they are not recomputed.",
            "No threshold selection, model inference, policy changes or new automatic omissions.",
        ],
    }
