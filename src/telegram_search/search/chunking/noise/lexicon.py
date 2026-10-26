"""Pinned, attributed frequency lexicon; absence is never evidence of noise."""

import hashlib
import json
from dataclasses import dataclass
from importlib.resources import files

from telegram_search.search.chunking.noise.contracts import CandidateLimits
from telegram_search.search.chunking.noise.normalization import bounded_form

LEXICON_ID = "wordfreq-3.1.1-ru-en-top20k-whole-v1"
LEXICON_SHA256 = "a296a46493fbfbb2475742f3abbb7bd05ca1743d059f293acf34b0761a3ed602"


@dataclass(frozen=True)
class Lexicon:
    forms: tuple[str, ...]
    id: str = LEXICON_ID
    sha256: str = LEXICON_SHA256
    license: str = "CC-BY-SA-4.0"


def load_lexicon(check=lambda: None):
    check()
    resource = files("telegram_search.config").joinpath("noise_lexicon.json")
    with resource.open("rb") as stream:
        encoded = stream.read(2 * 1024**2 + 1)
    if len(encoded) > 2 * 1024**2 or hashlib.sha256(encoded).hexdigest() != LEXICON_SHA256:
        raise ValueError("Noise lexicon integrity check failed")
    data = json.loads(encoded)
    if (
        data.get("schema_version") != 1
        or data.get("id") != LEXICON_ID
        or data.get("license") != "CC-BY-SA-4.0"
        or not isinstance(data.get("upstream_data_attribution"), str)
        or not data["upstream_data_attribution"]
        or not isinstance(data.get("forms"), list)
        or not 1 <= len(data["forms"]) <= 50000
    ):
        raise ValueError("Invalid noise lexicon")
    limits, previous = CandidateLimits(), None
    for form in data["forms"]:
        check()
        if bounded_form(form, limits) != form or (previous is not None and previous >= form):
            raise ValueError("Invalid noise lexicon form")
        previous = form
    return Lexicon(tuple(data["forms"]))
