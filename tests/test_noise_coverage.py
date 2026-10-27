import json
from dataclasses import replace

import pytest
from conftest import export, load, message
from test_noise_analysis import start_ready

from telegram_search.indexing.noise_analysis import NoiseAnalysisService
from telegram_search.indexing.noise_policy import NoisePolicyService
from telegram_search.search.chunking.noise.candidates import CandidateGenerator
from telegram_search.search.chunking.noise.contracts import AnalysisOptions
from telegram_search.search.chunking.noise.coverage import application_ready, coverage
from telegram_search.search.chunking.noise.scoring import evaluate
from telegram_search.shared.errors import UserError
from telegram_search.storage.chunking import chat_policy


@pytest.fixture
def services(db, importer):
    analyses = NoiseAnalysisService(db)
    try:
        yield analyses, NoisePolicyService(db, analyses, importer.lifecycle_lock)
    finally:
        analyses.close()


def summary(limits=()):
    return {
        "complete": not limits,
        "limits": list(limits),
        "new_auto_omit_enabled": False,
        **coverage(limits),
    }


@pytest.mark.parametrize(
    "limits", [(), ("candidate_limit",), ("payload_limit",), ("candidate_limit", "payload_limit")]
)
def test_complete_source_and_advisory_evidence_limits_allow_exact_rules_only(limits):
    value = summary(limits)
    assert application_ready(value)
    assert value["source_complete"] and value["policy_applicable"]
    assert value["evidence_complete"] == (not limits)
    assert value["complete"] == (not limits)


@pytest.mark.parametrize(
    "limit",
    [
        "message_limit",
        "form_limit",
        "phrase_form_limit",
        "text_limit",
        "time_limit",
        "unknown_future_limit",
    ],
)
def test_source_or_unknown_limits_cannot_be_upgraded_to_applicable(limit):
    value = summary((limit, "candidate_limit"))
    assert not value["source_complete"] and not application_ready(value)
    value["source_complete"] = value["policy_applicable"] = True
    assert not application_ready(value)


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_complete", 1),
        ("evidence_complete", 0),
        ("policy_applicable", "true"),
        ("coverage_version", "unknown"),
        ("coverage_version", None),
        ("complete", True),
        ("new_auto_omit_enabled", True),
        ("limits", ["candidate_limit", "candidate_limit"]),
        ("limits", [True]),
        ("limits", None),
    ],
)
def test_inconsistent_or_unversioned_readiness_is_rejected(field, value):
    record = summary(("candidate_limit",))
    record[field] = value
    assert not application_ready(record)


def test_legacy_full_reports_remain_readable_but_old_partial_reports_are_not_upgraded():
    for complete in (False, True):
        record = {
            "complete": complete,
            "limits": [] if complete else ["candidate_limit"],
            "new_auto_omit_enabled": False,
        }
        assert application_ready(record) is complete
    assert not application_ready(None)
    assert not application_ready({"complete": True})


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_complete", False),
        ("evidence_complete", True),
        ("policy_applicable", False),
        ("limited_evidence_forms", 0),
    ],
)
def test_hybrid_coverage_cannot_use_legacy_full_report_fallback(field, value):
    record = {"complete": True, "limits": [], "new_auto_omit_enabled": False, field: value}
    assert not application_ready(record)


def test_missing_noise_matches_after_an_interrupted_candidate_search_remain_review():
    report = CandidateGenerator().analyze("новыйтермин")
    incomplete = replace(report, complete=False)
    result = evaluate(incomplete, {"eligible_count": 1}, lambda _: 0)
    assert result["decision"] == "review" and result["reason"] == "candidate_limit"
    assert not result["complete"] and not result["calibrated"]


def test_short_words_do_not_block_a_full_chat_and_new_ambiguous_forms_stay(
    db, importer, tmp_path, services
):
    analyses, policies = services
    chat = load(
        importer,
        export(
            tmp_path / "synthetic",
            [
                message(1, "да"),
                message(2, "ок"),
                message(3, "бял"),
                message(4, "сука"),
                message(5, "Содержательная фраза"),
            ],
        ),
    )["chat_id"]
    state = start_ready(analyses, chat)
    assert state["state"] == "ready"
    assert state["summary"]["complete"] is False
    assert state["summary"]["source_complete"] is True
    assert state["summary"]["evidence_complete"] is False
    assert state["summary"]["policy_applicable"] is True
    assert state["summary"]["limited_evidence_forms"] >= 2
    assert "candidate_limit" in state["summary"]["limits"]
    assert application_ready(state["summary"])
    rows = {item["form"]: item for item in analyses.report(chat, state["id"])["forms"]}
    for form in ("да", "ок"):
        assert rows[form]["candidate"]["decision"] == "keep"
        assert rows[form]["candidate"]["complete"] is False
    assert rows["бял"]["candidate"]["decision"] == "review"
    baseline = policies.preview(chat, state["id"], 0)
    assert baseline["explicit_omit_forms"] == 0 and not baseline["new_auto_omit_enabled"]
    policies.save_overrides(chat, state["id"], 0, {"бял": "omit", "сука": "keep"})
    applied = policies.apply(chat, state["id"], 1)
    with db.connect() as conn:
        assert chat_policy(conn, chat) == applied["policy_id"]
        assert (
            conn.execute("SELECT count(*) FROM messages WHERE chat_id=?", (chat,)).fetchone()[0]
            == 5
        )
        policy = json.loads(
            conn.execute(
                "SELECT policy_json FROM chunking_policies WHERE id=?", (applied["policy_id"],)
            ).fetchone()[0]
        )
    assert policy["omit"] == ["бял"] and policy["keep"] == ["сука"]


def test_source_limited_analysis_with_short_words_stays_read_only(db, importer, tmp_path, services):
    analyses, policies = services
    chat = load(importer, export(tmp_path / "synthetic", [message(1, "да"), message(2, "бял")]))[
        "chat_id"
    ]
    state = start_ready(analyses, chat, AnalysisOptions(max_messages=1))
    assert state["state"] == "limited" and not state["summary"]["source_complete"]
    assert "message_limit" in state["summary"]["limits"]
    assert not application_ready(state["summary"])
    with pytest.raises(UserError, match="полный актуальный"):
        policies.preview(chat, state["id"], 0)
