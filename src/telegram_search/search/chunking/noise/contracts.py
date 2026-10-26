"""Bounded candidate evidence is distinct from a decision to omit a message."""

from dataclasses import dataclass
from typing import Literal

CANDIDATE_VERSION = "one-edit-runs-periods-v1"


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
