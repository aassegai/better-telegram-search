import copy
import json
import pickle
from dataclasses import replace

import pytest
from test_chunking_policy import Words, message

from telegram_search.search.chunking.builder import EpisodeBuilder
from telegram_search.search.chunking.classification import classify
from telegram_search.search.chunking.noise.compile import NoisePolicy, compile_policy
from telegram_search.search.chunking.policy import DEFAULT_POLICY, ChunkPolicy
from telegram_search.shared.text import serialize


def test_exact_policy_is_canonical_frozen_and_independent_of_audit_counts():
    first = compile_policy(DEFAULT_POLICY, omit=("бял", "блля", "сука"), keep=("бял", "сука"))
    second = compile_policy(DEFAULT_POLICY, omit=("блля", "блля"), keep=("сука", "бял"))
    assert first.identity == second.identity and first.json == second.json
    assert first.omit == ("блля",) and first.keep == ("бял", "сука")
    assert first.identity != DEFAULT_POLICY.identity
    assert first.base.identity == DEFAULT_POLICY.identity
    assert ChunkPolicy.from_json(first.json) == first
    assert copy.deepcopy(first) == first and pickle.loads(pickle.dumps(first)) == first
    assert first.max_tokens == 480 and first.max_messages == 8
    assert first.json is first.json  # No 50K-form serialization on every chunk.
    assert compile_policy(first, omit=("бллля",)).omit == ("бллля",)
    assert compile_policy(DEFAULT_POLICY, omit=("блля",)).identity != first.identity
    with pytest.raises(TypeError):
        compile_policy(DEFAULT_POLICY, omit=("блля",), counts={"блля": 20})


@pytest.mark.parametrize(
    "forms",
    [
        ("Бял",),
        ("бял!",),
        ("два слова",),
        ("https://example.org/lol",),
        ("а" * 65,),
        (None,),
        "бял",
    ],
)
def test_policy_accepts_only_bounded_canonical_exact_forms(forms):
    with pytest.raises(ValueError):
        compile_policy(DEFAULT_POLICY, omit=forms)


@pytest.mark.parametrize("field", ["max_tokens", "max_messages", "gap_seconds", "support_count"])
def test_new_policy_rejects_bool_numeric_budgets_without_changing_v2(field):
    with pytest.raises(ValueError):
        compile_policy(replace(DEFAULT_POLICY, **{field: True}))


def test_policy_rejects_unknown_versions_noncanonical_sets_and_audit_payloads():
    encoded = compile_policy(DEFAULT_POLICY, omit=("бял",)).json
    for key, value in (
        ("version", "unknown"),
        ("protections", "unknown"),
        ("omit", ["бял", "бял"]),
        ("created_at", 42),
    ):
        data = json.loads(encoded)
        data[key] = value
        with pytest.raises(ValueError):
            NoisePolicy.from_json(serialize(data))
    with pytest.raises(ValueError):
        NoisePolicy.from_json("x" * (8 * 1024**2 + 1))


def test_classification_is_exact_whole_message_only_and_manual_keep_wins(monkeypatch):
    from telegram_search.search.chunking.noise.candidates import CandidateGenerator

    def forbidden(*args, **kwargs):
        pytest.fail("Runtime classification must not run candidate matching")

    monkeypatch.setattr(CandidateGenerator, "analyze", forbidden)
    policy = compile_policy(DEFAULT_POLICY, omit=("бял",), keep=("сука",))
    assert not classify(message(1, "бял!!!"), policy).include
    assert classify(message(1, "сука"), policy).include
    assert classify(message(1, "бял не работает"), policy).include
    assert classify(message(1, "бялл"), policy).include
    assert not classify(message(1, "бял"), policy).counts
    assert classify(message(1, "Содержательная подпись", has_photo=True), policy).counts


@pytest.mark.parametrize(
    ("text", "extra"),
    [
        ("Бял", {}),
        ("БЯЛ", {}),
        ("бlя", {}),
        ("4060", {}),
        ("8гб", {}),
        ("foo-bar", {}),
        ("бял", {"has_photo": True}),
        ("бял", {"has_attachment": True}),
        ("бял", {"protected": True}),
    ],
)
def test_new_exact_forms_preserve_protected_occurrences(text, extra):
    from telegram_search.search.chunking.classification import whole_word

    policy = compile_policy(DEFAULT_POLICY, omit=(whole_word(text),))
    assert classify(message(1, text, **extra), policy).include
    assert classify(
        message(1, "бял"),
        compile_policy(DEFAULT_POLICY, omit=("бял",)),
        parent=message(2, "Вопрос?"),
    ).include
    # Existing seeds retain their separately accepted case behavior.
    assert not classify(message(1, "СУКА"), policy).include


@pytest.mark.parametrize("neighbor", [False, True])
def test_omitted_borrowed_parent_stays_a_dependency_and_does_not_reenter_text(neighbor):
    parent = message(1, "бял", content_hash="old")
    answer = message(2, "термин", reply_to=None if neighbor else 1)
    policy = compile_policy(DEFAULT_POLICY, omit=("бял",))
    builder = EpisodeBuilder(
        Words(),
        policy,
        lookup=lambda chat, mid: parent,
        neighbor_lookup=(lambda item: parent) if neighbor else None,
    )
    chunk = list(builder.build([answer], 1, "space"))[0]
    assert "бял" not in chunk.text and "термин" in chunk.text
    assert chunk.dependencies == ((1, "old"),)
    assert [part.message_id for part in chunk.parts] == [2]
    assert builder.source_messages == 1 and builder.skipped_messages == 0
    changed = replace(parent, text="Полезная фраза", content_hash="new")
    builder.lookup = lambda chat, mid: changed
    builder.neighbor_lookup = (lambda item: changed) if neighbor else builder.neighbor_lookup
    updated = list(builder.build([answer], 1, "space"))[0]
    assert "Полезная фраза" in updated.text
    assert updated.dependencies == ((1, "new"),) and updated.id != chunk.id


def test_borrowed_reply_protection_uses_one_nonrecursive_lookup_and_tracks_question():
    question = message(1, "Какой вариант?", content_hash="question")
    parent = message(2, "бял", reply_to=1, content_hash="answer")
    owner = message(3, "термин", reply_to=2)
    seen = []

    def lookup(chat, mid):
        seen.append(mid)
        return {1: question, 2: parent}.get(mid)

    policy = compile_policy(DEFAULT_POLICY, omit=("бял",))
    chunk = list(EpisodeBuilder(Words(), policy, lookup=lookup).build([owner], 1, "space"))[0]
    assert "бял" in chunk.text and "Какой вариант?" in chunk.text
    assert chunk.dependencies == ((1, "question"), (2, "answer"))
    assert len(seen) <= 4
    assert chunk.source_parts[0].message_id == 3


def test_v2_borrowed_context_behavior_and_own_lexical_ranges_remain_compatible():
    parent = message(1, "сука", content_hash="seed")
    owner = message(2, "термин", reply_to=1)
    old = list(EpisodeBuilder(Words(), lookup=lambda chat, mid: parent).build([owner], 1, "space"))[
        0
    ]
    assert "сука" in old.text
    new = list(
        EpisodeBuilder(
            Words(), compile_policy(DEFAULT_POLICY), lookup=lambda chat, mid: parent
        ).build([owner], 1, "space")
    )[0]
    assert "сука" not in new.text and new.dependencies == ((1, "seed"),)
    messages = [message(1, "Содержательная фраза"), message(2, "бял"), message(3, "бял в фразе")]
    builder = EpisodeBuilder(Words(), compile_policy(DEFAULT_POLICY, omit=("бял",)))
    chunk = list(builder.build(messages, 1, "space"))[0]
    assert chunk.text.count("бял") == 1 and chunk.lexical_text.count("бял") == 2
    assert [part.message_id for part in chunk.source_parts] == [1, 2, 3]
    assert [part.message_id for part in chunk.parts] == [1, 3]
    assert builder.source_messages == 3 and builder.skipped_messages == 1


def test_cyclic_reply_protection_is_bounded_without_dropping_a_known_question():
    owner = message(3, "почему?", reply_to=2, content_hash="owner")
    parent = message(2, "бял", reply_to=3, content_hash="parent")
    calls = []

    def lookup(chat, mid):
        calls.append(mid)
        return {2: parent, 3: owner}.get(mid)

    chunks = list(
        EpisodeBuilder(Words(), compile_policy(DEFAULT_POLICY, omit=("бял",)), lookup=lookup).build(
            [owner], 1, "space"
        )
    )
    assert len(chunks) == 1 and "бял" in chunks[0].text and "почему?" in chunks[0].text
    assert len(calls) <= 3
