"""Bounded whole-message families and conservative automatic command decisions."""

import math
import re

from telegram_search.search.chunking.noise.candidates import one_edit
from telegram_search.search.chunking.noise.messages import APPROVED_MESSAGES, is_message_form
from telegram_search.search.chunking.noise.normalization import is_lengthening, run_key

COMMAND = re.compile(r"/[a-z][a-z0-9_]{0,63}(?:@[a-z0-9_]{1,64})?\Z")


def auto_message_reason(form, count, seed=None, *, useful_words=None):
    if count["eligible_count"] <= 0:
        return None
    if form in APPROVED_MESSAGES:
        return "approved_message_seed"
    if seed in APPROVED_MESSAGES:
        left, right = form.split(), seed.split()
        pairs = list(zip(left, right, strict=False))
        changed = [(a, b) for a, b in pairs if a != b]
        # Only an internal letter typo in one sufficiently long word. An added
        # negation/question/number/argument or punctuation is never authorized.
        if (
            len(left) == len(right)
            and len(changed) == 1
            and all(a.isalpha() and b.isalpha() and min(len(a), len(b)) >= 4 for a, b in changed)
            and all(a[0] == b[0] and a[-1] == b[-1] for a, b in changed)
            and useful_words is not None
            and all(a not in useful_words for a, _ in changed)
            and (one_edit(form, seed) == "transposition" or is_lengthening(form, seed))
            and count["eligible_count"] >= 3
            and count.get("eligible_days", 0) >= 2
        ):
            return "confirmed_message_family"
    # Repetition alone cannot distinguish everyday conversation from spam.
    # A new automatic family requires actual Telegram bot-command entities,
    # a whole-command shape and repeated eligible use on different days.
    if (
        COMMAND.fullmatch(form)
        and count["eligible_count"] >= 10
        and count.get("eligible_days", 0) >= 2
        and count.get("bot_commands", 0) == count["whole_count"]
    ):
        return "structured_bot_command"
    return None


class MessageCandidates:
    max_postings = 100000
    max_key_chars = 4 * 1024**2

    def __init__(self, counts, *, useful_words=None, check=lambda: None):
        self.index, self.counts = {}, {}
        self.useful_words = None if useful_words is None else frozenset(useful_words)
        self.complete = True
        postings = key_chars = 0
        # Approved examples are always anchors. The remaining anchors are
        # observed frequent messages, not a hardcoded command whitelist.
        ordered = [(form, 0) for form in sorted(APPROVED_MESSAGES)]
        ordered += [(row["form"], row["whole_count"]) for row in counts]
        for form, count in ordered:
            check()
            if not is_message_form(form) or form in self.counts:
                continue
            signatures = {form, *(form[:i] + form[i + 1 :] for i in range(len(form)))}
            signatures.add("\0runs:" + run_key(form))
            weight = sum(len(key) for key in signatures)
            if (
                postings + len(signatures) > self.max_postings
                or key_chars + weight > self.max_key_chars
            ):
                self.complete = False
                break
            self.counts[form] = count
            for key in signatures:
                self.index.setdefault(key, []).append(form)
            postings += len(signatures)
            key_chars += weight

    def evaluate(self, form, count, frequency, *, check=lambda: None):
        pool = set()
        complete = self.complete
        keys = {form, *(form[:i] + form[i + 1 :] for i in range(len(form)))}
        keys.add("\0runs:" + run_key(form))
        for key in sorted(keys):
            check()
            for other in self.index.get(key, ()):
                pool.add(other)
                if len(pool) > 128:
                    complete = False
                    break
            if len(pool) > 128:
                break
        matches = []
        for other in sorted(pool):
            check()
            operation = one_edit(form, other)
            if operation is None and is_lengthening(form, other):
                operation = "lengthening"
            if operation is not None and (
                other == form
                or other in APPROVED_MESSAGES
                or self.counts[other] >= count["whole_count"] * 3
            ):
                matches.append((other, operation))
        matches.sort(
            key=lambda pair: (pair[0] not in APPROVED_MESSAGES, -frequency(pair[0]), pair[0])
        )
        seed = matches[0][0] if matches else None
        automatic = (
            auto_message_reason(form, count, seed, useful_words=self.useful_words)
            if complete
            else None
        )
        # Exact approved seeds do not depend on the bounded similarity index.
        if form in APPROVED_MESSAGES:
            automatic = auto_message_reason(form, count, form)
        decision = "omit" if automatic else "review" if count["eligible_count"] else "keep"
        reason = automatic or ("repeated_message" if count["eligible_count"] else "protected_only")

        def evidence(value, source, operation, distance):
            uses = frequency(value)
            return {
                "form": value,
                "source": source,
                "operation": operation,
                "distance": distance,
                "corpus_count": uses,
                "score": math.log1p(uses),
            }

        return {
            "schema_version": 2,
            "decision": decision,
            "reason": reason,
            "calibrated": False,
            "score_version": "whole-message-families-v1",
            "primary_seed": seed,
            "noise": [
                evidence(
                    other,
                    "noise_seed",
                    operation,
                    None if operation == "lengthening" else int(operation != "exact"),
                )
                for other, operation in matches[:16]
            ],
            "useful": [],
            "unchanged": evidence(form, "unchanged", "unchanged", 0),
            "margin": None,
            "complete": complete and len(matches) <= 16,
            "known_word": False,
            "lexicon_available": False,
        }
