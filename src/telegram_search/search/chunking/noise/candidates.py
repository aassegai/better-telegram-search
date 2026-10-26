"""One-edit and explicit repetition hypotheses, never transitive noise classes."""

from dataclasses import dataclass

from telegram_search.search.chunking.classification import whole_word
from telegram_search.search.chunking.noise.contracts import (
    CandidateLimits,
    CandidateReport,
    Hypothesis,
)
from telegram_search.search.chunking.noise.normalization import (
    bounded_form,
    is_lengthening,
    laughter_seed,
    repeated_seed,
    run_key,
    structural_flags,
)
from telegram_search.search.chunking.policy import APPROVED_FILLER


def one_edit(observed, original):
    """Operation from original to observed, or None if it needs more than one.

    Adjacent transposition is supported. This is only a radius-one matcher, not
    an implementation of unrestricted Damerau-Levenshtein for larger radii.
    """
    if observed == original:
        return "exact"
    if len(observed) == len(original):
        differences = []
        for i, (left, right) in enumerate(zip(observed, original, strict=True)):
            if left != right:
                differences.append(i)
                if len(differences) > 2:
                    return None
        if len(differences) == 1:
            return "substitution"
        if len(differences) == 2:
            a, b = differences
            if b == a + 1 and observed[a] == original[b] and observed[b] == original[a]:
                return "transposition"
        return None
    if abs(len(observed) - len(original)) != 1:
        return None
    longer, shorter = (
        (observed, original) if len(observed) > len(original) else (original, observed)
    )
    prefix = 0
    while prefix < len(shorter) and longer[prefix] == shorter[prefix]:
        prefix += 1
    if longer[prefix + 1 :] != shorter[prefix:]:
        return None
    return "insertion" if len(observed) > len(original) else "deletion"


def _signatures(form):
    return {form, *(form[:i] + form[i + 1 :] for i in range(len(form)))}


@dataclass(frozen=True)
class _Matches:
    matches: tuple[Hypothesis, ...]
    complete: bool
    comparisons: int
    lookups: int
    postings_read: int


class _FormIndex:
    def __init__(self, forms, source, limits, check):
        self.source, self.limits = source, limits
        values = set()
        for i, value in enumerate(forms):
            check()
            if i >= limits.max_lexicon_records:
                raise ValueError("Noise lexicon record limit exceeded")
            normalized = bounded_form(value, limits)
            if normalized is None:
                raise ValueError("Noise lexicon entry is not a bounded whole word")
            values.add(normalized)
        self.forms = frozenset(values)
        self.by_signature, self.by_runs = {}, {}
        self.postings = 0
        for form in sorted(values):
            check()
            signatures = _signatures(form)
            self.postings += len(signatures) + 1
            if self.postings > limits.max_index_postings:
                raise ValueError("Noise lexicon index limit exceeded")
            for signature in signatures:
                self.by_signature.setdefault(signature, []).append(form)
            self.by_runs.setdefault(run_key(form), []).append(form)

    def query(self, form, check):
        pool = {form} if form in self.forms else set()
        lookups = postings_read = 0
        complete = True
        for key in [*sorted(_signatures(form)), None]:
            check()
            lookups += 1
            bucket = (
                self.by_runs.get(run_key(form), ())
                if key is None
                else self.by_signature.get(key, ())
            )
            for candidate in bucket:
                postings_read += 1
                if postings_read % 256 == 0:
                    check()
                if candidate in pool:
                    continue
                if len(pool) >= self.limits.max_candidates:
                    complete = False
                    break
                pool.add(candidate)
            if not complete:
                break
        matches = []
        for candidate in sorted(pool):
            check()
            operation = one_edit(form, candidate)
            if operation is not None:
                matches.append(
                    Hypothesis(candidate, self.source, operation, int(operation != "exact"))
                )
            elif is_lengthening(form, candidate):
                matches.append(Hypothesis(candidate, self.source, "lengthening", None))
        return _Matches(tuple(matches), complete, len(pool), lookups, postings_read)


class CandidateGenerator:
    """Compare only with confirmed seeds and a caller-supplied useful lexicon.

    No model, file, network, SQL or runtime policy is opened here. A candidate
    report is evidence, and cannot itself authorize excluding a message.
    """

    def __init__(
        self, *, seeds=APPROVED_FILLER, useful_words=None, limits=None, check=lambda: None
    ):
        self.limits = limits or CandidateLimits()
        self.noise = _FormIndex(seeds, "noise_seed", self.limits, check)
        self.useful = _FormIndex(
            () if useful_words is None else useful_words, "useful_word", self.limits, check
        )

    def analyze(self, text, *, check=lambda: None):
        check()
        form = bounded_form(text, self.limits)
        if form is None:
            original = (
                whole_word(text)
                if isinstance(text, str) and len(text) <= self.limits.max_input_chars
                else None
            )
            limited = isinstance(text, str) and (
                len(text) > self.limits.max_input_chars
                or (original is not None and len(original) > self.limits.max_form_chars)
            )
            return CandidateReport(
                None,
                (),
                (),
                None,
                False,
                bool(self.useful.forms),
                (),
                not limited,
                0,
                0,
                0,
                "input_limit" if limited else "not_whole_word",
            )
        noise = self.noise.query(form, check)
        useful = self.useful.query(form, check)
        matched = {item.form: item for item in noise.matches}
        for seed, operation in (
            (repeated_seed(form, self.noise.forms), "repeated_block"),
            (laughter_seed(form, self.noise.forms), "laughter_pattern"),
        ):
            if seed is not None and seed not in matched:
                matched[seed] = Hypothesis(seed, "noise_seed", operation, None)
        return CandidateReport(
            form,
            tuple(matched[key] for key in sorted(matched)),
            useful.matches,
            Hypothesis(form, "unchanged", "unchanged", 0),
            form in self.useful.forms,
            bool(self.useful.forms),
            structural_flags(text, form),
            noise.complete and useful.complete,
            noise.comparisons + useful.comparisons,
            noise.lookups + useful.lookups,
            noise.postings_read + useful.postings_read,
        )
