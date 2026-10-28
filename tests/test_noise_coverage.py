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
        "report_size_limit",
        "unknown_future_limit",
    ],
)
def test_known_source_limits_allow_exact_rules_without_claiming_full_coverage(limit):
    value = summary((limit, "candidate_limit"))
    assert not value["source_complete"]
    assert application_ready(value) is (limit != "unknown_future_limit")
    value["source_complete"] = True
    assert not application_ready(value)


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_complete", 1),
        ("evidence_complete", 0),
        ("policy_applicable", "true"),
        ("coverage_version", "unknown"),
        ("coverage_version", None),
        ("coverage_version", []),
        ("coverage_version", {}),
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


def test_source_limited_analysis_allows_explicit_exact_rules(db, importer, tmp_path, services):
    analyses, policies = services
    chat = load(importer, export(tmp_path / "synthetic", [message(1, "да"), message(2, "бял")]))[
        "chat_id"
    ]
    state = start_ready(analyses, chat, AnalysisOptions(max_messages=1))
    assert state["state"] == "limited" and not state["summary"]["source_complete"]
    assert "message_limit" in state["summary"]["limits"]
    assert application_ready(state["summary"])
    assert policies.preview(chat, state["id"], 0)["new_auto_omit_enabled"] is False
    policies.save_overrides(chat, state["id"], 0, {"да": "keep"})
    policies.apply(chat, state["id"], 1)


def test_v072_source_limited_reports_remain_partial_but_can_compile_exact_rules():
    value = {
        "complete": False,
        "limits": ["candidate_limit", "text_limit"],
        "new_auto_omit_enabled": False,
        **coverage(["candidate_limit", "text_limit"], legacy=True),
    }
    assert not value["source_complete"] and not value["policy_applicable"]
    assert application_ready(value)


def test_long_messages_are_skipped_and_do_not_block_rules(db, importer, tmp_path, services):
    analyses, policies = services
    chat = load(
        importer,
        export(
            tmp_path / "short-scope",
            [
                message(1, "раз два три четыре пять " * 400),
                message(2, "бял"),
                message(3, "ах" * 100),
            ],
        ),
    )["chat_id"]
    state = start_ready(analyses, chat)
    assert state["summary"]["skipped_long_messages"] == 1
    assert state["summary"]["limited_messages"] == 1
    assert state["summary"]["statistics_scope"] == "short-messages-under-five-unique-v1"
    assert application_ready(state["summary"])
    assert {row["form"] for row in analyses.report(chat, state["id"])["forms"]} == {"бял"}
    policies.save_overrides(chat, state["id"], 0, {"бял": "omit"})
    applied = policies.apply(chat, state["id"], 1)
    with db.connect() as conn:
        assert chat_policy(conn, chat) == applied["policy_id"]
        assert (
            conn.execute("SELECT COUNT(*) FROM messages WHERE chat_id=?", (chat,)).fetchone()[0]
            == 3
        )


def test_time_limit_keeps_committed_unscored_forms_reviewable_and_applicable(
    db, importer, tmp_path, services, monkeypatch
):
    import time

    analyses, policies = services
    chat = load(
        importer, export(tmp_path / "time-partial", [message(1, "бял"), message(2, "сука")])
    )["chat_id"]
    original = analyses._progress

    def expire_after_committed_scan(job, **values):
        original(job, **values)
        if values.get("scanned_messages"):
            job.deadline = time.monotonic() - 1

    monkeypatch.setattr(analyses, "_progress", expire_after_committed_scan)
    state = start_ready(analyses, chat)
    assert state["state"] == "limited" and state["error"] == "time_limit"
    assert state["summary"]["scanned_messages"] == 2
    assert state["summary"]["decisions"] == {"keep": 0, "review": 0, "omit": 0}
    forms = analyses.report(chat, state["id"], decision="review")["forms"]
    assert {row["form"] for row in forms} == {"бял", "сука"}
    assert all(row["candidate"] is None for row in forms)
    policies.save_overrides(chat, state["id"], 0, {"бял": "omit"})
    policies.preview(chat, state["id"], 1)
    applied = policies.apply(chat, state["id"], 1)
    with db.connect() as conn:
        assert chat_policy(conn, chat) == applied["policy_id"]


def test_existing_v072_partial_report_can_be_applied_without_reanalysis(
    db, importer, tmp_path, services
):
    analyses, policies = services
    chat = load(
        importer, export(tmp_path / "old-partial", [message(1, "бял"), message(2, "ах" * 100)])
    )["chat_id"]
    state = start_ready(analyses, chat)
    old_summary = {**state["summary"], **coverage(state["summary"]["limits"], legacy=True)}
    assert old_summary["policy_applicable"] is False and not old_summary["source_complete"]
    with db.connect() as conn:
        conn.execute(
            "UPDATE noise_analyses SET summary_json=? WHERE id=?",
            (json.dumps(old_summary), state["id"]),
        )
    policies.save_overrides(chat, state["id"], 0, {"бял": "keep"})
    policies.preview(chat, state["id"], 1)
    policies.apply(chat, state["id"], 1)
    with db.connect() as conn:
        stored = json.loads(
            conn.execute(
                "SELECT summary_json FROM noise_analyses WHERE id=?", (state["id"],)
            ).fetchone()[0]
        )
        assert stored == old_summary


def test_report_size_limit_keeps_prior_batches_applicable(
    db, importer, tmp_path, services, monkeypatch
):
    import sqlite3

    from telegram_search.storage.noise import NoiseReport

    analyses, policies = services
    chat = load(
        importer, export(tmp_path / "size-partial", [message(i, "бял") for i in range(1, 260)])
    )["chat_id"]
    original = NoiseReport.add_batch
    calls = 0

    def full_on_second_batch(report, batch, maximum):
        nonlocal calls
        calls += 1
        if calls == 2:
            exc = sqlite3.OperationalError("synthetic report size cap")
            exc.sqlite_errorcode = sqlite3.SQLITE_FULL
            raise exc
        return original(report, batch, maximum)

    monkeypatch.setattr(NoiseReport, "add_batch", full_on_second_batch)
    state = start_ready(analyses, chat)
    assert state["state"] == "limited" and state["error"] == "report_size_limit"
    assert state["summary"]["scanned_messages"] == 128
    assert application_ready(state["summary"])
    assert analyses.report(chat, state["id"], decision="review")["forms"][0]["whole_count"] == 128
    policies.save_overrides(chat, state["id"], 0, {"бял": "keep"})
    policies.apply(chat, state["id"], 1)


def test_report_size_rollback_does_not_publish_phantom_forms(
    db, importer, tmp_path, services, monkeypatch
):
    import sqlite3
    from contextlib import contextmanager

    from telegram_search.storage.noise import NoiseReport

    analyses, policies = services
    rows = [message(i, "бял" if i <= 128 else "блля") for i in range(1, 260)]
    chat = load(importer, export(tmp_path / "rollback-partial", rows))["chat_id"]
    original = NoiseReport.connect
    raised = []

    @contextmanager
    def full_after_form_insert(report, *, write=False):
        with original(report, write=write) as conn:

            class Proxy:
                def __getattr__(self, name):
                    return getattr(conn, name)

                def execute(self, sql, parameters=()):
                    if (
                        write
                        and sql.startswith("INSERT OR IGNORE INTO days")
                        and parameters[0] == "блля"
                        and not raised
                    ):
                        raised.append(report.forms_count)
                        exc = sqlite3.OperationalError("synthetic report rollback")
                        exc.sqlite_errorcode = sqlite3.SQLITE_FULL
                        raise exc
                    return conn.execute(sql, parameters)

            yield Proxy()

    monkeypatch.setattr(NoiseReport, "connect", full_after_form_insert)
    state = start_ready(analyses, chat)
    assert raised == [2]
    assert state["summary"]["forms"] == 1
    assert state["summary"]["scanned_messages"] == 128
    assert [row["form"] for row in analyses.report(chat, state["id"])["forms"]] == ["бял"]
    policies.preview(chat, state["id"], 0)
