"""Build the attributed local lexicon; no archives or private workspace are read."""

import hashlib
import importlib.metadata
import json
import sys
from pathlib import Path

from wordfreq import available_languages, top_n_list

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from telegram_search.search.chunking.classification import whole_word  # noqa: E402


def main():
    distribution = importlib.metadata.distribution("wordfreq")
    if distribution.version != "3.1.1":
        raise RuntimeError("The lexicon builder requires pinned wordfreq 3.1.1")
    forms, counts, hashes = set(), {}, {}
    for language in ("ru", "en"):
        selected = top_n_list(language, 20000, wordlist="best")
        normalized = {whole_word(word) for word in selected}
        normalized = {word for word in normalized if word is not None and len(word) <= 64}
        forms.update(normalized)
        counts[language] = len(normalized)
        hashes[language] = hashlib.sha256(
            Path(available_languages(wordlist="best")[language]).read_bytes()
        ).hexdigest()
    description = distribution.metadata.get_payload()
    license_marker = "## License"
    if license_marker not in description:
        raise RuntimeError("The pinned source attribution is missing")
    payload = {
        "schema_version": 1,
        "id": "wordfreq-3.1.1-ru-en-top20k-whole-v1",
        "source": "https://pypi.org/project/wordfreq/3.1.1/",
        "source_version": distribution.version,
        "source_data_sha256": hashes,
        "selection": {"languages": ["ru", "en"], "top_n_per_language": 20000, "wordlist": "best"},
        "normalization": "whole-unicode-word-nfkc-casefold-yo-v1",
        "modifications": (
            "Select and deduplicate whole-word forms, NFKC/casefold/yo normalize; "
            "no frequency values or runtime tokenization are supplied."
        ),
        "license": "CC-BY-SA-4.0",
        "license_url": "https://creativecommons.org/licenses/by-sa/4.0/",
        "attribution": (
            "wordfreq 3.1.1 by Robyn Speer. This derived lexicon retains the data-source "
            "credits and licence notices from the pinned package description below. "
            "SUBTLEX by Marc Brysbaert et al. is freely available data."
        ),
        "upstream_code_license": distribution.read_text("LICENSE.txt"),
        "upstream_data_attribution": license_marker + description.split(license_marker, 1)[1],
        "normalized_counts": counts,
        "forms": sorted(forms),
    }
    target = ROOT / "src/telegram_search/config/noise_lexicon.json"
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    target.write_bytes(encoded)
    print(
        json.dumps(
            {
                "forms": len(forms),
                "bytes": len(encoded),
                "sha256": hashlib.sha256(encoded).hexdigest(),
            }
        )
    )


if __name__ == "__main__":
    main()
