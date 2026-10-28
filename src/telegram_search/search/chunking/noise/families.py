"""Whole-form family presentation and the selected default for report families."""

from telegram_search.search.chunking.noise.messages import valid_form
from telegram_search.search.chunking.noise.normalization import run_key

EDGE_PUNCTUATION = " \t.,!;:()[]{}«»\"'“”„…"
KEPT_RESPONSES = frozenset({"да", "нет", "ага"})


def punctuation_key(form):
    # Preserve words, arguments, negations, question marks and command slashes.
    key = form.strip(EDGE_PUNCTUATION)
    return key if valid_form(key) else form


def family_default(form, seed):
    if seed is None:
        return None
    if any(
        run_key(punctuation_key(value)) in KEPT_RESPONSES
        for value in (form, seed)
        if value is not None
    ):
        return "keep"
    return "omit"
