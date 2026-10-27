from dataclasses import replace
from itertools import product, repeat
from pathlib import Path

import pytest

from telegram_search.search.chunking.classification import classify
from telegram_search.search.chunking.noise.candidates import CandidateGenerator, one_edit
from telegram_search.search.chunking.noise.contracts import CandidateLimits
from telegram_search.search.chunking.policy import DEFAULT_POLICY, ChunkPolicy
from telegram_search.search.chunks import SourceMessage


def osa_distance(left, right):
    """Independent matrix reference; radius one also covers adjacent swaps."""
    matrix = [list(range(len(right) + 1))]
    for i, a in enumerate(left, 1):
        row = [i]
        for j, b in enumerate(right, 1):
            row.append(min(row[j - 1] + 1, matrix[i - 1][j] + 1, matrix[i - 1][j - 1] + (a != b)))
            if i > 1 and j > 1 and a == right[j - 2] and left[i - 2] == b:
                row[j] = min(row[j], matrix[i - 2][j - 2] + 1)
        matrix.append(row)
    return matrix[-1][-1]


def vocabulary(max_length):
    return tuple(
        "".join(chars) for size in range(1, max_length + 1) for chars in product("abc", repeat=size)
    )


def test_previous_policy_json_and_identity_remain_byte_compatible():
    value = (
        (Path(__file__).parent / "fixtures/synthetic/chunking-v2-policy.json")
        .read_text(encoding="utf-8")
        .strip()
    )
    restored = ChunkPolicy.from_json(value)
    expected = "03a84672c6f17ddc9cceb4aafdc442ce145777d867e69d606c7e1a8db2f73735"
    assert restored.identity == DEFAULT_POLICY.identity == expected
    assert restored.json == DEFAULT_POLICY.json == value
    # Preparing hypotheses cannot mutate old rules or start applying typos.
    generator = CandidateGenerator(useful_words=("был", "сука"))
    generator.analyze("бял")
    message = SourceMessage("synthetic", 1, 1750000000, "Synthetic author", "бял")
    assert classify(message, restored).include
    assert not classify(replace(message, text="сука"), restored).include


@pytest.mark.parametrize(
    ("observed", "seed", "operation"),
    [
        ("бял", "бля", "transposition"),
        ("блля", "бля", "insertion"),
        ("пздец", "пиздец", "deletion"),
        ("пизднц", "пиздец", "substitution"),
    ],
)
def test_required_one_edit_candidates_preserve_direction(observed, seed, operation):
    report = CandidateGenerator().analyze(observed)
    assert any(
        h.form == seed and h.operation == operation and h.distance == 1 for h in report.noise
    )
    assert report.stage == "candidate" and report.unchanged.form == observed


def test_radius_one_and_index_completeness_against_independent_reference():
    words = ("", *vocabulary(3))
    for original, observed in product(words, repeat=2):
        assert (one_edit(observed, original) is not None) == (osa_distance(observed, original) <= 1)
    seeds = vocabulary(3)
    generator = CandidateGenerator(seeds=seeds)
    for observed in vocabulary(4):
        report = generator.analyze(observed)
        expected = {word for word in seeds if osa_distance(observed, word) <= 1}
        actual = {h.form for h in report.noise if h.distance is not None}
        assert report.complete and actual == expected


def test_ambiguous_typo_keeps_both_useful_and_noise_hypotheses_and_unchanged():
    generator = CandidateGenerator(useful_words=("был", "для", "пол", "рука", "суда"))
    report = generator.analyze("бял")
    assert [(h.form, h.operation) for h in report.noise] == [("бля", "transposition")]
    assert [(h.form, h.operation) for h in report.useful] == [("был", "substitution")]
    assert report.unchanged.source == "unchanged" and report.unchanged.form == "бял"
    assert report.known_word is False and report.lexicon_available
    assert report.comparisons == 2 and report.complete


@pytest.mark.parametrize(
    "text", ["для", "пол", "рука", "суда", "мама", "папа", "Алла", "CUDA", "ИИ", "ООО"]
)
def test_correct_words_have_exact_useful_evidence_even_if_near_noise(text):
    report = CandidateGenerator(useful_words=(text,)).analyze(text)
    assert report.known_word and report.unchanged.form == report.form
    assert any(h.form == report.form and h.distance == 0 for h in report.useful)
    assert report.stage == "candidate"  # Proximity never itself becomes omit.


@pytest.mark.parametrize(
    ("text", "seed", "operation"),
    [
        ("бллля", "бля", "lengthening"),
        ("пиииздец", "пиздец", "lengthening"),
        ("ахахахахаха", "хаха", "laughter_pattern"),
        ("lollollol", "lol", "repeated_block"),
    ],
)
def test_bounded_run_and_repetition_families_do_not_claim_one_edit(text, seed, operation):
    report = CandidateGenerator().analyze(text)
    assert any(
        h.form == seed and h.operation == operation and h.distance is None for h in report.noise
    )


def test_run_matching_preserves_legal_double_letters_and_unknown_terms():
    generator = CandidateGenerator(useful_words=("класс", "мама", "папа", "Алла"))
    report = generator.analyze("клааасс")
    assert any(h.form == "класс" and h.operation == "lengthening" for h in report.useful)
    assert not generator.analyze("клааас").useful
    for text in ("мамамама", "папапапа", "Алла", "новыйтермин", "4060"):
        report = generator.analyze(text)
        assert not report.noise and report.unchanged.form == report.form


def test_no_transitive_classes_transliteration_or_homoglyph_normalization():
    generator = CandidateGenerator(seeds=("бля",))
    assert generator.analyze("бял").noise
    assert generator.analyze("бил").noise == ()  # One edit from бял, two from бля.
    assert CandidateGenerator(seeds=("cyka",)).analyze("сука").noise == ()
    mixed = generator.analyze("бlя")
    assert mixed.form == "бlя" and "mixed_scripts" in mixed.flags
    assert mixed.unchanged.form == "бlя"


def test_whole_word_normalization_preserves_case_signals_and_never_extracts_urls_or_phrases():
    generator = CandidateGenerator()
    report = generator.analyze("«БЯЛ!!!»")
    assert report.form == "бял" and "uppercase" in report.flags
    assert generator.analyze("ＬＯＬ").form == "lol"  # Existing NFKC contract.
    assert generator.analyze("ХЕХ").form == "хех"
    for text in (
        "бля не работает",
        "https://example.org/lol",
        "@lol",
        "/lol",
        "print(lol)",
        "foo.bar",
    ):
        report = generator.analyze(text)
        assert report.form is None and report.noise == () and report.useful == ()
        assert report.reason == "not_whole_word"


def test_short_and_unknown_forms_keep_unchanged_hypothesis_without_a_lexicon():
    generator = CandidateGenerator()
    for text in ("ля", "xy", "0", "Термин"):
        report = generator.analyze(text)
        assert not report.lexicon_available and report.unchanged.form == report.form
    assert "number" in generator.analyze("4060").flags
    assert "alphanumeric" in generator.analyze("8ГБ").flags


def test_overflow_is_explicit_deterministic_and_cannot_silently_drop_competing_words():
    limits = CandidateLimits(max_candidates=1)
    first = CandidateGenerator(seeds=("ab", "ac"), useful_words=("ad",), limits=limits).analyze(
        "ad"
    )
    second = CandidateGenerator(seeds=("ac", "ab"), useful_words=("ad",), limits=limits).analyze(
        "ad"
    )
    assert first == second and not first.complete and first.known_word
    assert first.comparisons <= 2 and first.noise[0].form == "ab"
    long = CandidateGenerator().analyze("а" * 100000)
    assert not long.complete and long.form is None and long.reason == "input_limit"


def test_lexicon_limits_include_duplicate_records_and_index_postings():
    with pytest.raises(ValueError, match="record limit"):
        CandidateGenerator(seeds=repeat("ab"), limits=CandidateLimits(max_lexicon_records=3))
    with pytest.raises(ValueError, match="index limit"):
        CandidateGenerator(seeds=("abcdef",), limits=CandidateLimits(max_index_postings=2))
    with pytest.raises(ValueError, match="whole word"):
        CandidateGenerator(useful_words=("https://example.org",))
    for values in ({"max_candidates": True}, {"max_form_chars": 0}, {"max_input_chars": 1}):
        with pytest.raises(ValueError, match="limits"):
            CandidateLimits(**values)


def test_cancellation_is_checked_during_construction_and_query():
    calls = 0

    def cancel():
        nonlocal calls
        calls += 1
        if calls == 3:
            raise RuntimeError("synthetic cancellation")

    with pytest.raises(RuntimeError, match="synthetic cancellation"):
        CandidateGenerator(check=cancel)
    generator = CandidateGenerator(useful_words=("был",))
    calls = 0
    with pytest.raises(RuntimeError, match="synthetic cancellation"):
        generator.analyze("бял", check=cancel)
    assert calls == 3


def test_lookup_is_indexed_rather_than_all_pairs_against_the_lexicon():
    generator = CandidateGenerator(useful_words=(f"topicx{i:05d}" for i in range(5000)))
    report = generator.analyze("zzzzzzzzzzzz")
    assert report.complete and report.lexicon_available
    assert report.comparisons < 10 and report.postings_read < 20
    assert report.signature_lookups <= 2 * (len(report.form) + 2)
