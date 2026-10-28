"""Whole messages and conservative family decisions, with public synthetic text."""

import json
from dataclasses import replace

import pytest
from conftest import export, load, message
from test_noise_analysis import start_ready
from test_noise_statistics import observation

from telegram_search.indexing.noise_analysis import NoiseAnalysisService
from telegram_search.indexing.noise_policy import NoisePolicyService
from telegram_search.search.chunking.classification import classify
from telegram_search.search.chunking.legacy import SourceMessage
from telegram_search.search.chunking.noise.compile import compile_policy
from telegram_search.search.chunking.noise.contracts import validate_evidence
from telegram_search.search.chunking.noise.lexicon import load_lexicon
from telegram_search.search.chunking.noise.message_candidates import (
    MessageCandidates,
    auto_message_reason,
)
from telegram_search.search.chunking.noise.messages import message_form
from telegram_search.search.chunking.noise.statistics import collect_batch
from telegram_search.search.chunking.policy import DEFAULT_POLICY, ChunkPolicy
from telegram_search.storage.chunking import chat_policy, reset_for_rebuild


def counts(**extra):
    return {
        "whole_count": 10,
        "eligible_count": 10,
        "days": 3,
        "eligible_days": 3,
        "authors": 3,
        "bot_commands": 0,
        **extra,
    }


def test_whole_message_statistics_include_phrases_but_do_not_extract_substrings():
    texts = [
        "Зоопарк   xd",
        "покормить жабу",
        "не покормить жабу",
        "доброе утро",
        "один два три четыре пять",
        "да " * 33,
    ]
    batch = collect_batch([observation(text) for text in texts], "salt", include_messages=True)
    assert [row.form for row in batch.observations] == [
        "зоопарк xd",
        "покормить жабу",
        "не покормить жабу",
        "доброе утро",
    ]
    assert batch.skipped_long_messages == batch.limited_messages == 1
    assert batch.single_word_messages == 0
    assert message_form("https://example.org/spam") is None
    assert message_form("/farm") == "/farm"


def test_whole_message_identity_size_skips_are_counted_explicitly():
    batch = collect_batch([observation("покормить " * 26)], "salt", include_messages=True)
    assert not batch.observations
    assert batch.limited_messages == 1 and batch.skipped_long_messages == 0


@pytest.mark.parametrize(
    "text",
    [
        "покорить жабу",
        "покормить бабу",
        "покормить жабы",
        "не покормить жабу",
        "покормить жабу?",
        "покормить жабу сейчас",
    ],
)
def test_meaningful_near_commands_never_become_automatic_typos(text):
    useful = frozenset(load_lexicon().forms)
    assert auto_message_reason(text, counts(), "покормить жабу", useful_words=useful) is None


def test_safe_typo_families_require_eligible_repeat_days_and_local_lexicon():
    useful = frozenset(load_lexicon().forms)
    for text in ("зоопрак xd", "пооокормить жабу"):
        seed = "зоопарк xd" if "xd" in text else "покормить жабу"
        assert (
            auto_message_reason(text, counts(), seed, useful_words=useful)
            == "confirmed_message_family"
        )
        assert auto_message_reason(text, counts(), seed) is None
        assert auto_message_reason(text, counts(eligible_days=1), seed, useful_words=useful) is None
    assert auto_message_reason("зоопарк xd", counts()) == "approved_message_seed"
    assert auto_message_reason("зоопарк xd", counts(eligible_count=0)) is None


def test_frequency_only_cannot_delete_conversation_and_generic_families_are_discovered():
    generator = MessageCandidates(
        [
            {"form": "доброе утро", "whole_count": 100},
            {"form": "новый повторяемый спам", "whole_count": 90},
        ]
    )
    for text in ("доброе утро", "новый повторяемый спам"):
        result = generator.evaluate(text, counts(), lambda _: 100)
        assert result["decision"] == "review"
        assert result["primary_seed"] == text
        validate_evidence(result, text)
    result = generator.evaluate("новый повторяемый спм", counts(), lambda _: 90)
    assert result["decision"] == "review"
    assert result["primary_seed"] == "новый повторяемый спам"


def test_bot_command_metadata_is_required_and_meaningful_arguments_stay():
    assert auto_message_reason("/farm", counts(bot_commands=10)) == "structured_bot_command"
    assert auto_message_reason("/farm", counts(bot_commands=9)) is None
    assert auto_message_reason("/farm", counts(bot_commands=10, eligible_days=1)) is None
    assert auto_message_reason("/ask почему болит голова?", counts(bot_commands=10)) is None
    assert auto_message_reason("/transfer 100 @friend", counts(bot_commands=10)) is None


def test_v4_policy_preserves_legacy_identity_and_uses_exact_whole_messages_only():
    old = compile_policy(DEFAULT_POLICY, omit=("бял",))
    assert old.version == "telegram-episodes-noise-v3"
    assert ChunkPolicy.from_json(old.json).identity == old.identity
    policy = compile_policy(old, omit=("бял", "покормить жабу"))
    assert policy.version == "telegram-episodes-messages-v4"
    assert ChunkPolicy.from_json(policy.json).identity == policy.identity
    raw = json.loads(policy.json)
    raw["version"] = "telegram-episodes-noise-v3"
    with pytest.raises(ValueError):
        ChunkPolicy.from_json(json.dumps(raw))
    base = SourceMessage(
        chat_id="chat",
        message_id=1,
        timestamp=1750000000,
        author="User",
        author_id="user",
        text="покормить жабу",
    )
    assert not classify(base, policy).include
    for extra in ("не покормить жабу", "покормить жабу сейчас", "как покормить жабу?"):
        assert classify(replace(base, text=extra), policy).include
    for protected in (replace(base, has_attachment=True), replace(base, protected=True)):
        assert classify(protected, policy).include
    assert classify(base, policy, replace(base, text="что сделать?", message_id=2)).include
    kept = compile_policy(policy, omit=policy.omit, keep=("покормить жабу",))
    assert classify(base, kept).include


def test_whole_message_analysis_apply_survives_rebuild_and_keeps_original_keyword_source(
    db, importer, tmp_path
):
    records = [
        message(1, "зоопарк xd"),
        message(2, "покормить жабу"),
        message(3, "не покормить жабу"),
        message(4, "покорить жабу"),
    ]
    for index in range(5, 11):
        records.append(message(index, "зоопрак xd", date=1750000000 + (index % 2) * 86400))
    for index in range(11, 25):
        records.append(
            message(
                index,
                "/farm",
                date=1750000000 + (index % 2) * 86400,
                text_entities=[{"type": "bot_command", "text": "/farm"}],
            )
        )
    chat = load(importer, export(tmp_path / "public", records))["chat_id"]
    analyses = NoiseAnalysisService(db)
    policies = NoisePolicyService(db, analyses, importer.lifecycle_lock)
    try:
        job = start_ready(analyses, chat)
        assert job["state"] == "ready" and job["summary"]["scan_complete"]
        rows = {r["form"]: r for r in analyses.report(chat, job["id"])["forms"]}
        for text in ("зоопарк xd", "покормить жабу", "зоопрак xd", "/farm"):
            assert rows[text]["candidate"]["decision"] == "omit", rows[text]
        for text in ("не покормить жабу", "покорить жабу"):
            assert rows[text]["candidate"]["decision"] == "review"
        families = analyses.report(chat, job["id"], kind="families")["families"]
        assert any(r["seed"] == "зоопарк xd" for r in families)
        first = analyses.report(chat, job["id"], family="покормить жабу", limit=1)
        assert first["next_offset"] == 1
        second = analyses.report(chat, job["id"], family="покормить жабу", limit=1, offset=1)
        assert {r["form"] for r in [*first["forms"], *second["forms"]]} == {
            "покормить жабу",
            "покорить жабу",
        }
        assert not analyses.report(chat, job["id"], family="другая команда")["forms"]
        policies.preview(chat, job["id"], 0)
        policies.save_overrides(
            chat, job["id"], 0, {"покорить жабу": "omit", "покормить жабу": "keep"}
        )
        applied = policies.apply(chat, job["id"], 1)
        with db.connect() as conn:
            selected = chat_policy(conn, chat)
            assert selected == applied["policy_id"]
            policy = ChunkPolicy.from_json(
                conn.execute(
                    "SELECT policy_json FROM chunking_policies WHERE id=?", (selected,)
                ).fetchone()[0]
            )
            base = SourceMessage(
                chat_id=chat,
                message_id=1,
                timestamp=1750000000,
                author="User",
                text="покорить жабу",
            )
            assert not classify(base, policy).include
            assert classify(replace(base, text="покормить жабу"), policy).include
            reset_for_rebuild(conn, chat)
            assert chat_policy(conn, chat) == selected
            assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == len(records)
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM message_fts WHERE message_fts MATCH ?", ('"покормить"',)
                ).fetchone()[0]
                >= 2
            )
    finally:
        analyses.close()


def test_protected_second_day_cannot_manufacture_repeat_evidence(db):
    from telegram_search.storage.noise import NoiseReport

    report = NoiseReport(db, "a" * 32, {}, create=True)
    batch = collect_batch(
        [
            *[observation("/farm", bot_command=True) for _ in range(10)],
            observation("/farm", bot_command=True, day="2026-01-02", has_attachment=True),
        ],
        "salt",
        include_messages=True,
    )
    report.add_batch(batch, 100)
    count = report.forms()[0]
    assert count["days"] == 2 and count["eligible_days"] == 1
    assert auto_message_reason("/farm", count) is None
