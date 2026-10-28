"""Bounded normalized observations in private reports; source messages stay intact."""

import hashlib
from collections import Counter
from dataclasses import dataclass

from telegram_search.search.chunking.classification import WORD, whole_word
from telegram_search.search.chunking.noise.messages import message_form
from telegram_search.search.chunking.noise.normalization import structural_flags
from telegram_search.search.chunking.policy import APPROVED_FILLER
from telegram_search.shared.text import normalize_text

MAX_TEXT_CHARS = 16384
MAX_METADATA_CHARS = 65536
MAX_UNIQUE_WORDS = 4
STATISTICS_SCOPE = "short-messages-under-five-unique-v1"


@dataclass(frozen=True)
class Observation:
    form: str
    eligible: bool
    protections: tuple[str, ...]
    day: str
    author_key: str
    signals: tuple[str, ...] = ()


@dataclass(frozen=True)
class BatchStatistics:
    observations: tuple[Observation, ...]
    phrases: dict[str, int]
    limited_messages: int
    skipped_long_messages: int = 0
    single_word_messages: int = 0


def collect_batch(rows, salt, check=lambda: None, *, include_messages=False):
    observations, phrases, limited, skipped = [], Counter(), 0, 0
    single_words = 0
    for row in rows:
        check()
        text = row["text"]
        if len(text) > MAX_TEXT_CHARS or row.get("metadata_limited", False):
            limited += 1
            continue
        normalized = normalize_text(text)
        uses = Counter()
        for number, word in enumerate(WORD.finditer(normalized)):
            if number % 128 == 0:
                check()
            uses[word.group()] += 1
            if len(uses) > MAX_UNIQUE_WORDS:
                break
        if len(uses) > MAX_UNIQUE_WORDS:
            skipped += 1
            continue
        single = whole_word(text)
        form = message_form(text) if include_messages else single
        if form is not None:
            if (single is not None and len(form) > 64) or len(text) > 512:
                limited += 1
                continue
            reasons = []
            if row["has_attachment"] or row["has_photo"]:
                reasons.append("attachment")
            if row["protected"]:
                reasons.append("structured_entity")
            if row["parent_question"]:
                reasons.append("question_reply")
            # Already approved seeds retain their existing protection semantics.
            if single is not None and form not in APPROVED_FILLER:
                reasons.extend(structural_flags(text, form))
                if any(char in form for char in "-'’"):
                    reasons.append("compound_term")
            observations.append(
                Observation(
                    form,
                    not reasons,
                    tuple(reasons),
                    row["day"] or "unknown",
                    hashlib.sha256((salt + "\0" + (row["author_id"] or "")).encode()).hexdigest(),
                    ("bot_command",) if row.get("bot_command", False) else (),
                )
            )
            single_words += int(single is not None)
        elif single is not None:
            # A word over the bounded identity size is a local skip, not a
            # reason to abort scanning the rest of the dialog.
            limited += 1
        elif (
            include_messages
            and uses
            and (
                len(text) > 512 or len(" ".join(normalized.split())) > 256 or len(text.split()) > 32
            )
        ):
            limited += 1
        if single is None and not row["protected"] and len(text.split()) > 1:
            phrases.update({value: count for value, count in uses.items() if len(value) <= 64})
    return BatchStatistics(tuple(observations), dict(phrases), limited, skipped, single_words)
