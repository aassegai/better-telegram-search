"""Uncalibrated evidence scores are never probabilities or a new omission gate."""

import math
from dataclasses import asdict

from telegram_search.search.chunking.policy import APPROVED_FILLER

SCORE_VERSION = "unit-error-weights-log1p-v2-uncalibrated"


def evaluate(report, count, frequency):
    def score(hypothesis):
        observed = frequency(hypothesis.form)
        return {**asdict(hypothesis), "corpus_count": observed, "score": math.log1p(observed)}

    noise = sorted(
        (score(item) for item in report.noise), key=lambda item: (-item["score"], item["form"])
    )
    useful = sorted(
        (score(item) for item in report.useful), key=lambda item: (-item["score"], item["form"])
    )
    unchanged = score(report.unchanged)
    best_noise = noise[0] if noise else None
    competitor = max([unchanged["score"], *(item["score"] for item in useful)])
    decision, reason = "review", "uncalibrated"
    if count["eligible_count"] == 0:
        decision, reason = "keep", "protected_only"
    elif report.form in APPROVED_FILLER:
        decision, reason = "omit", "approved_seed"
    elif report.known_word:
        decision, reason = "keep", "lexicon_form"
    elif len(report.form) < 3:
        decision, reason = "keep", "short_new_form"
    elif not report.complete:
        decision, reason = "review", "candidate_limit"
    elif not noise:
        decision, reason = "keep", "no_noise_hypothesis"
    return {
        "schema_version": 1,
        "decision": decision,
        "reason": reason,
        "calibrated": False,
        "score_version": SCORE_VERSION,
        "primary_seed": best_noise["form"] if best_noise else None,
        "noise": noise[:16],
        "useful": useful[:16],
        "unchanged": unchanged,
        "margin": best_noise["score"] - competitor if best_noise else None,
        "complete": report.complete and len(noise) <= 16 and len(useful) <= 16,
        "known_word": report.known_word,
        "lexicon_available": report.lexicon_available,
    }
