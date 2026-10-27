import math
from collections import Counter
from copy import deepcopy

import pytest

from telegram_search.search.chunking.noise.candidates import CandidateGenerator
from telegram_search.search.chunking.noise.contracts import AnalysisOptions, validate_evidence
from telegram_search.search.chunking.noise.lexicon import load_lexicon
from telegram_search.search.chunking.noise.scoring import evaluate
from telegram_search.search.chunking.noise.statistics import MAX_TEXT_CHARS, collect_batch


def observation(text, **extra):
    return {
        "text": text,
        "has_photo": False,
        "has_attachment": False,
        "protected": False,
        "parent_question": False,
        "day": "2026-01-01",
        "author_id": "synthetic-author",
        **extra,
    }


def test_lexicon_is_pinned_attributed_local_and_cancellable():
    lexicon = load_lexicon()
    assert len(lexicon.forms) == 39532
    assert {"для", "рука", "мама", "класс", "hello"} <= set(lexicon.forms)
    assert lexicon.license == "CC-BY-SA-4.0"
    assert lexicon.forms == tuple(sorted(set(lexicon.forms)))
    calls = 0

    def cancel():
        nonlocal calls
        calls += 1
        if calls == 4:
            raise RuntimeError("synthetic stop")

    with pytest.raises(RuntimeError, match="synthetic stop"):
        load_lexicon(cancel)
    assert calls == 4


def test_statistics_count_whole_forms_and_phrase_evidence_separately():
    batch = collect_batch(
        [
            observation("бял"),
            observation("бял"),
            observation("он был рядом"),
            observation("бял тут"),
        ],
        "salt",
    )
    assert Counter(item.form for item in batch.observations) == {"бял": 2}
    assert batch.phrases == {"он": 1, "был": 1, "рядом": 1, "бял": 1, "тут": 1}
    assert not batch.limited_messages
    assert all(item.eligible for item in batch.observations)
    first = batch.observations[0]
    assert "synthetic-author" not in first.author_key and len(first.author_key) == 64
    assert (
        collect_batch([observation("бял")], "another").observations[0].author_key
        != first.author_key
    )


@pytest.mark.parametrize(
    ("text", "extra", "protection"),
    [
        ("бял", {"has_photo": True}, "attachment"),
        ("бял", {"has_attachment": True}, "attachment"),
        ("бял", {"protected": True}, "structured_entity"),
        ("бял", {"parent_question": True}, "question_reply"),
        ("Алла", {}, "titlecase"),
        ("ИИ", {}, "uppercase"),
        ("4060", {}, "number"),
        ("8ГБ", {}, "alphanumeric"),
        ("бlя", {}, "mixed_scripts"),
        ("foo-bar", {}, "compound_term"),
    ],
)
def test_per_occurrence_protections(text, extra, protection):
    item = collect_batch([observation(text, **extra)], "salt").observations[0]
    assert not item.eligible and protection in item.protections


def test_existing_seeds_keep_case_contract_but_protected_captions_and_replies_survive():
    batch = collect_batch(
        [
            observation("СУКА"),
            observation("сука", has_attachment=True),
            observation("сука", parent_question=True),
        ],
        "salt",
    )
    assert [item.eligible for item in batch.observations] == [True, False, False]
    assert all(item.form == "сука" for item in batch.observations)


def test_input_and_phrase_work_are_bounded_without_extracting_urls_as_whole_forms():
    batch = collect_batch(
        [
            observation("а" * (MAX_TEXT_CHARS + 1)),
            observation("слово " * 300),
            observation("https://example.org/lol"),
            observation("бял", metadata_limited=True),
        ],
        "salt",
    )
    assert batch.limited_messages == 3 and not batch.observations
    assert batch.phrases == {"слово": 256}


def test_scores_compete_with_useful_and_unchanged_without_new_automatic_omission():
    report = CandidateGenerator(useful_words=("был",)).analyze("бял")
    count = {"eligible_count": 3}
    noisy = evaluate(report, count, lambda form: {"бля": 1000, "был": 1, "бял": 3}.get(form, 0))
    useful = evaluate(report, count, lambda form: {"бля": 1, "был": 1000, "бял": 3}.get(form, 0))
    unchanged = evaluate(report, count, lambda form: {"бля": 1, "бял": 1000}.get(form, 0))
    assert noisy["margin"] > 0 > useful["margin"]
    assert unchanged["margin"] < 0
    for result in (noisy, useful, unchanged):
        assert result["decision"] == "review" and not result["calibrated"]
        assert result["unchanged"]["form"] == "бял"
        assert math.isfinite(result["margin"])
    assert noisy["noise"][0]["score"] == math.log1p(1000)


def test_known_words_and_unknowns_keep_and_only_preapproved_eligible_seed_can_omit():
    generator = CandidateGenerator(
        useful_words=("для", "пол", "рука", "суда", "мама", "папа", "Алла")
    )
    for form in ("для", "пол", "рука", "суда", "мама", "папа", "Алла", "новыйтермин", "ля"):
        assert (
            evaluate(generator.analyze(form), {"eligible_count": 1}, lambda _: 10)["decision"]
            == "keep"
        )
    assert (
        evaluate(generator.analyze("сука"), {"eligible_count": 1}, lambda _: 1)["reason"]
        == "approved_seed"
    )
    assert (
        evaluate(generator.analyze("сука"), {"eligible_count": 0}, lambda _: 1)["decision"]
        == "keep"
    )


@pytest.mark.parametrize(
    "values", [{"max_messages": True}, {"max_forms": 0}, {"time_limit_seconds": 3601}]
)
def test_analysis_limits_are_strict(values):
    with pytest.raises(ValueError):
        AnalysisOptions(**values)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("unchanged",), None),
        (("noise",), ["bad"]),
        (("decision",), []),
        (("schema_version",), True),
        (("complete",), 0),
        (("reason",), "\ud800"),
        (("margin",), float("inf")),
        (("known_word",), "false"),
        (("primary_seed",), "<html>"),
        (("unchanged", "form"), "другой"),
        (("unchanged", "corpus_count"), True),
        (("noise", 0, "operation"), []),
    ],
)
def test_complete_persisted_evidence_schema_rejects_malformed_nested_payloads(path, value):
    evidence = CandidateGenerator(useful_words=("был",)).analyze("бял")
    original = evaluate(evidence, {"eligible_count": 1}, lambda _: 1)
    assert validate_evidence(original, "бял") is original
    malformed = deepcopy(original)
    target = malformed
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValueError, match="persisted"):
        validate_evidence(malformed, "бял")
