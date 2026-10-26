"""Signatures preserve the whole-word contract and never rewrite source text."""

import unicodedata
from itertools import groupby

from telegram_search.search.chunking.classification import whole_word


def bounded_form(text, limits):
    if not isinstance(text, str) or len(text) > limits.max_input_chars:
        return None
    form = whole_word(text)
    return form if form is not None and len(form) <= limits.max_form_chars else None


def runs(form):
    """Run lengths of an already bounded, normalized form."""
    return tuple((char, sum(1 for _ in group)) for char, group in groupby(form))


def run_key(form):
    return "".join(char for char, _ in runs(form))


def is_lengthening(observed, original):
    left, right = runs(observed), runs(original)
    return (
        len(left) == len(right)
        and left != right
        and all(
            a == b and count_a >= count_b
            for (a, count_a), (b, count_b) in zip(left, right, strict=True)
        )
    )


def structural_flags(text, form):
    flags = []
    if text.isupper() and sum(char.isalpha() for char in text) >= 2:
        flags.append("uppercase")
    if text.istitle():
        flags.append("titlecase")
    if form.isdecimal():
        flags.append("number")
    elif any(char.isdigit() for char in form):
        flags.append("alphanumeric")
    scripts = {
        "latin" if "LATIN" in unicodedata.name(char, "") else "cyrillic"
        for char in form
        if "LATIN" in unicodedata.name(char, "") or "CYRILLIC" in unicodedata.name(char, "")
    }
    if len(scripts) > 1:
        flags.append("mixed_scripts")
    return tuple(flags)


def repeated_seed(form, confirmed):
    for width in range(1, min(4, len(form) // 2) + 1):
        block = form[:width]
        if len(form) % width == 0 and form == block * (len(form) // width) and block in confirmed:
            return block
    return None


def laughter_seed(form, confirmed):
    # A bounded whole-form family; no entropy rule or Unicode/transliteration map.
    if len(form) < 4 or any(char not in "ах" for char in form):
        return None
    if any(a == b for a, b in zip(form, form[1:], strict=False)):
        return None
    return next((seed for seed in ("хаха", "ахаха", "ахах", "хах") if seed in confirmed), None)
