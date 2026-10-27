"""Bounded candidate evidence is distinct from a decision to omit a message."""

from dataclasses import dataclass
from math import isfinite
from typing import Literal

CANDIDATE_VERSION = "one-edit-runs-periods-v1"
ANALYSIS_VERSION = "bounded-chat-statistics-v1"


@dataclass(frozen=True)
class AnalysisOptions:
    max_messages: int = 100000
    max_forms: int = 50000
    time_limit_seconds: int = 300

    def __post_init__(self):
        for value, minimum, maximum in (
            (self.max_messages, 1, 1000000),
            (self.max_forms, 1, 50000),
            (self.time_limit_seconds, 1, 3600),
        ):
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError("Invalid noise analysis limits")


@dataclass(frozen=True)
class CandidateLimits:
    max_form_chars: int = 64
    max_input_chars: int = 512
    max_lexicon_records: int = 50000
    max_index_postings: int = 500000
    max_candidates: int = 256

    def __post_init__(self):
        for value, maximum in (
            (self.max_form_chars, 128),
            (self.max_input_chars, 4096),
            (self.max_lexicon_records, 200000),
            (self.max_index_postings, 2000000),
            (self.max_candidates, 4096),
        ):
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError("Invalid noise candidate limits")
        if self.max_input_chars < self.max_form_chars:
            raise ValueError("Invalid noise candidate limits")


@dataclass(frozen=True)
class Hypothesis:
    form: str
    source: Literal["noise_seed", "useful_word", "unchanged"]
    operation: str
    # Run/period patterns can involve several edits; do not label them distance 1.
    distance: int | None


@dataclass(frozen=True)
class CandidateReport:
    form: str | None
    noise: tuple[Hypothesis, ...]
    useful: tuple[Hypothesis, ...]
    unchanged: Hypothesis | None
    known_word: bool
    lexicon_available: bool
    flags: tuple[str, ...]
    complete: bool
    comparisons: int
    signature_lookups: int
    postings_read: int
    reason: str | None = None
    stage: Literal["candidate"] = "candidate"
    version: str = CANDIDATE_VERSION


def validate_evidence(value, observed_form):
    """Validate the complete persisted schema before exposing it to a renderer."""
    from telegram_search.search.chunking.classification import whole_word

    def valid_form(form):
        return isinstance(form, str) and 1 <= len(form) <= 64 and whole_word(form) == form

    def number(item):
        return type(item) in {int, float} and abs(item) <= 10**12 and isfinite(item)

    def hypothesis(item, source):
        return (
            isinstance(item, dict)
            and set(item) == {"form", "source", "operation", "distance", "corpus_count", "score"}
            and valid_form(item["form"])
            and item["source"] == source
            and type(item["operation"]) is str
            and item["operation"]
            in {
                "exact",
                "unchanged",
                "insertion",
                "deletion",
                "substitution",
                "transposition",
                "lengthening",
                "repeated_block",
                "laughter_pattern",
            }
            and (
                item["distance"] is None
                or (type(item["distance"]) is int and item["distance"] in {0, 1})
            )
            and type(item["corpus_count"]) is int
            and 0 <= item["corpus_count"] <= 10**9
            and number(item["score"])
        )

    required = {
        "schema_version",
        "decision",
        "reason",
        "calibrated",
        "score_version",
        "primary_seed",
        "noise",
        "useful",
        "unchanged",
        "margin",
        "complete",
        "known_word",
        "lexicon_available",
    }
    if (
        not isinstance(value, dict)
        or set(value) != required
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or type(value["decision"]) is not str
        or value["decision"] not in {"keep", "review", "omit"}
        or not isinstance(value["reason"], str)
        or not 1 <= len(value["reason"]) <= 128
        or not value["reason"].isascii()
        or not isinstance(value["score_version"], str)
        or not 1 <= len(value["score_version"]) <= 128
        or not value["score_version"].isascii()
        or any(
            type(value[name]) is not bool
            for name in ("calibrated", "complete", "known_word", "lexicon_available")
        )
        or (value["primary_seed"] is not None and not valid_form(value["primary_seed"]))
        or (value["margin"] is not None and not number(value["margin"]))
        or not isinstance(value["noise"], list)
        or len(value["noise"]) > 16
        or not isinstance(value["useful"], list)
        or len(value["useful"]) > 16
        or not all(hypothesis(item, "noise_seed") for item in value["noise"])
        or not all(hypothesis(item, "useful_word") for item in value["useful"])
        or not hypothesis(value["unchanged"], "unchanged")
        or value["unchanged"]["form"] != observed_form
    ):
        raise ValueError("Invalid persisted noise evidence")
    return value
