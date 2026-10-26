import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, timedelta

import pytest
from conftest import export, load, message
from test_noise_analysis import start_ready

from telegram_search.indexing.noise_analysis import NoiseAnalysisService
from telegram_search.indexing.noise_policy import (
    NoiseDraftConflict,
    NoisePolicyService,
    validate_overrides,
)
from telegram_search.search.chunking.policy import ChunkPolicy
from telegram_search.shared.errors import UserError
from telegram_search.storage.chunking import chat_policy
from telegram_search.storage.noise import report_path


@pytest.fixture
def services(db, importer):
    analyses = NoiseAnalysisService(db)
    try:
        yield analyses, NoisePolicyService(db, analyses, importer.lifecycle_lock)
    finally:
        analyses.close()


def prepared(importer, tmp_path, services, *, chat_id=100):
    analyses, policies = services
    chat = load(
        importer,
        export(
            tmp_path / str(chat_id),
            [message(1, "Содержательная фраза"), message(2, "бял"), message(3, "сука")],
            chat_id,
        ),
    )["chat_id"]
    job = start_ready(analyses, chat)
    return chat, job["id"], policies


def test_explicit_override_apply_is_atomic_text_only_and_idempotent(
    db, importer, tmp_path, services
):
    chat, job_id, policy = prepared(importer, tmp_path, services)
    with db.connect() as conn:
        conn.execute(
            "UPDATE chats SET text_paused=1,media_paused=1,ocr_paused=1,text_batch=16 WHERE id=?",
            (chat,),
        )
        before_flags = tuple(
            conn.execute(
                "SELECT text_paused,media_paused,ocr_paused,text_batch FROM chats WHERE id=?",
                (chat,),
            ).fetchone()
        )
        media_before = [tuple(r) for r in conn.execute("SELECT * FROM media_state")]
        source_before = [tuple(r) for r in conn.execute("SELECT * FROM messages")]
        old_generation = conn.execute(
            "SELECT target_generation FROM index_segments WHERE chat_id=?", (chat,)
        ).fetchone()[0]
    original = policy.preview(chat, job_id, 0)
    assert original["explicit_omit_forms"] == 0 and not original["new_auto_omit_enabled"]
    saved = policy.save_overrides(chat, job_id, 0, {"бял": "omit", "сука": "keep"})
    assert saved["revision"] == 1
    preview = policy.preview(chat, job_id, 1)
    assert preview["policy_id"] != original["policy_id"] and preview["explicit_omit_forms"] == 1
    result = policy.apply(chat, job_id, 1)
    assert result == {
        "policy_id": preview["policy_id"],
        "already_applied": False,
        "scheduled_segments": 1,
    }
    with db.connect() as conn:
        assert chat_policy(conn, chat) == result["policy_id"]
        assert (
            tuple(
                conn.execute(
                    "SELECT text_paused,media_paused,ocr_paused,text_batch FROM chats WHERE id=?",
                    (chat,),
                ).fetchone()
            )
            == before_flags
        )
        assert [tuple(r) for r in conn.execute("SELECT * FROM media_state")] == media_before
        assert [tuple(r) for r in conn.execute("SELECT * FROM messages")] == source_before
        work = conn.execute(
            "SELECT * FROM index_work WHERE chat_id=? AND state='pending'", (chat,)
        ).fetchone()
        assert work["generation"] == old_generation + 1 and work["reason"] == "noise_policy"
        assert work["chunking_policy_id"] == preview["policy_id"]
        works = [tuple(r) for r in conn.execute("SELECT * FROM index_work")]
    assert policy.apply(chat, job_id, 1)["already_applied"]
    load(importer, export(tmp_path / "later", [message(4, "Позднее сообщение")]))
    assert policy.apply(chat, job_id, 1)["already_applied"]
    with db.connect() as conn:
        # Idempotence never supersedes the newer import's generation.
        assert all(
            row in [tuple(r) for r in conn.execute("SELECT * FROM index_work")]
            for row in works
            if row[4] == "superseded"
        )
    with pytest.raises(NoiseDraftConflict):
        policy.apply(chat, job_id, 0)


def test_override_revision_and_scope_are_checked(db, importer, tmp_path, services):
    chat, job_id, policy = prepared(importer, tmp_path, services)
    assert policy.save_overrides(chat, job_id, 0, {"бял": "keep"})["revision"] == 1
    with pytest.raises(NoiseDraftConflict):
        policy.save_overrides(chat, job_id, 0, {"бял": "omit"})
    with pytest.raises(KeyError):
        policy.preview("other", job_id, 1)
    with pytest.raises(UserError, match="не найдена"):
        policy.save_overrides(chat, job_id, 1, {"неизвестный": "omit"})
    with db.connect() as conn:
        conn.execute("UPDATE chats SET revision=revision+1 WHERE id=?", (chat,))
    stale = policy.metadata(chat, job_id)
    assert stale["stale"] and not stale["overrides_available"] and not stale["overrides"]
    for action in (lambda: policy.preview(chat, job_id, 1), lambda: policy.apply(chat, job_id, 1)):
        with pytest.raises(NoiseDraftConflict):
            action()


def test_report_tampering_and_old_reports_without_checksum_cannot_apply(
    db, importer, tmp_path, services
):
    chat, job_id, policy = prepared(importer, tmp_path, services)
    original = policy.metadata(chat, job_id)["current_policy_id"]
    path = report_path(db.workspace, job_id)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE forms SET whole_count=whole_count+1")
    with pytest.raises(UserError, match="изменён"):
        policy.apply(chat, job_id, 0)
    with db.connect() as conn:
        assert chat_policy(conn, chat) == original
        summary = json.loads(
            conn.execute(
                "SELECT summary_json FROM noise_analyses WHERE id=?", (job_id,)
            ).fetchone()[0]
        )
        summary.pop("report_sha256")
        conn.execute(
            "UPDATE noise_analyses SET summary_json=? WHERE id=?", (json.dumps(summary), job_id)
        )
    with pytest.raises(UserError, match="полный актуальный"):
        policy.apply(chat, job_id, 0)


def test_final_revision_fence_and_sql_failure_roll_back_all_apply_changes(
    db, importer, tmp_path, services, monkeypatch
):
    chat, job_id, policy = prepared(importer, tmp_path, services)
    original = policy._prepare

    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        with db.connect() as conn:
            conn.execute("UPDATE chats SET revision=revision+1 WHERE id=?", (chat,))
        return result

    monkeypatch.setattr(policy, "_prepare", changed)
    with pytest.raises(NoiseDraftConflict):
        policy.apply(chat, job_id, 0)
    monkeypatch.setattr(policy, "_prepare", original)
    analyses = services[0]
    job_id = start_ready(analyses, chat)["id"]
    with db.connect() as conn:
        before = {
            table: [tuple(r) for r in conn.execute(f"SELECT * FROM {table}")]
            for table in (
                "index_segments",
                "index_work",
                "chat_chunking",
                "chunking_policies",
                "chats",
            )
        }
        conn.execute(
            "CREATE TRIGGER synthetic_apply_failure BEFORE INSERT ON index_work "
            "WHEN NEW.reason='noise_policy' BEGIN SELECT RAISE(ABORT,'synthetic'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        policy.apply(chat, job_id, 0)
    with db.connect() as conn:
        for table, rows in before.items():
            assert rows == [tuple(r) for r in conn.execute(f"SELECT * FROM {table}")]
        assert (
            conn.execute(
                "SELECT applied_policy_id FROM noise_analyses WHERE id=?", (job_id,)
            ).fetchone()[0]
            is None
        )


def test_reanalysis_preserves_previous_manual_rules_unless_explicitly_overridden(
    db, importer, tmp_path, services
):
    chat, job_id, policy = prepared(importer, tmp_path, services)
    policy.save_overrides(chat, job_id, 0, {"бял": "omit", "сука": "keep"})
    first = policy.apply(chat, job_id, 1)
    next_job = start_ready(services[0], chat)["id"]
    next_preview = policy.preview(chat, next_job, 0)
    assert next_preview["policy_id"] == first["policy_id"]
    with db.connect() as conn:
        generations = [tuple(r) for r in conn.execute("SELECT * FROM index_segments")]
        revision = conn.execute("SELECT revision FROM chats WHERE id=?", (chat,)).fetchone()[0]
    assert policy.apply(chat, next_job, 0)["already_applied"]
    with db.connect() as conn:
        assert generations == [tuple(r) for r in conn.execute("SELECT * FROM index_segments")]
        assert (
            conn.execute("SELECT revision FROM chats WHERE id=?", (chat,)).fetchone()[0] == revision
        )
    policy.save_overrides(chat, next_job, 0, {"бял": "keep"})
    result = policy.apply(chat, next_job, 1)
    with db.connect() as conn:
        selected = ChunkPolicy.from_json(
            conn.execute(
                "SELECT policy_json FROM chunking_policies WHERE id=?", (result["policy_id"],)
            ).fetchone()[0]
        )
    assert selected.keep == ("бял", "сука") and not selected.omit


def test_large_day_count_uses_bounded_main_sql_and_no_source_scan_inside_writer(
    db, importer, tmp_path, services, monkeypatch
):
    chat, job_id, policy = prepared(importer, tmp_path, services)
    with db.connect() as conn:
        conn.executemany(
            "INSERT INTO index_segments(chat_id,utc_day,target_generation,lexical_generation) "
            "VALUES(?,?,1,1)",
            [(chat, (date(2020, 1, 1) + timedelta(days=i)).isoformat()) for i in range(1880)],
        )
    statements = []
    connect = db.connect

    @contextmanager
    def traced():
        with connect() as conn:
            conn.set_trace_callback(statements.append)
            yield conn

    monkeypatch.setattr(db, "connect", traced)
    result = policy.apply(chat, job_id, 0)
    assert result["scheduled_segments"] == 1881
    assert len(statements) < 80
    assert not any("FROM messages" in query for query in statements)


def test_applied_policy_is_consumed_by_real_segment_worker_and_words_still_find_source(
    db, importer, tmp_path, services
):
    from test_semantic import setup_index

    from telegram_search.search.hybrid import HybridSearch

    chat, semantic, worker, old_work = setup_index(
        db, importer, tmp_path, ["Содержательная фраза", "бял", "сука"], legacy=False
    )
    try:
        analysis = services[0]
        job_id = start_ready(analysis, chat)["id"]
        policy = NoisePolicyService(db, analysis, importer.lifecycle_lock, semantic)
        policy.save_overrides(chat, job_id, 0, {"бял": "omit", "сука": "keep"})
        applied = policy.apply(chat, job_id, 1)
        with db.connect() as conn:
            work = conn.execute(
                "SELECT id FROM index_work WHERE chat_id=? AND state='pending'", (chat,)
            ).fetchone()[0]
        result = worker.run(work)
        assert result["state"] == "done" and result["skipped_messages"] == 1
        with db.connect() as conn:
            chunk = conn.execute(
                "SELECT text,lexical_text FROM chunks WHERE chunking_policy_id=?",
                (applied["policy_id"],),
            ).fetchone()
            assert "бял" not in chunk["text"] and "сука" in chunk["text"]
            assert "бял" in chunk["lexical_text"]
        assert (
            HybridSearch(db, semantic, importer.lifecycle_lock).search("бял", mode="words")[
                "results"
            ][0]["message_id"]
            == 2
        )
    finally:
        semantic.shutdown()


def test_concurrent_apply_is_idempotent_and_busy_writer_is_safe_to_retry(
    db, importer, tmp_path, services, monkeypatch
):
    chat, job_id, policy = prepared(importer, tmp_path, services)
    with db.connect() as conn:
        selected = chat_policy(conn, chat)
        conn.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            policy.apply(chat, job_id, 0)
        assert chat_policy(conn, chat) == selected
    barrier = threading.Barrier(2)
    original = policy._prepare
    results, errors = [], []

    def held(*args, **kwargs):
        result = original(*args, **kwargs)
        barrier.wait(timeout=5)
        return result

    def apply():
        try:
            results.append(policy.apply(chat, job_id, 0))
        except Exception as exc:
            errors.append(exc)

    monkeypatch.setattr(policy, "_prepare", held)
    threads = [threading.Thread(target=apply) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert not any(thread.is_alive() for thread in threads)
    assert not errors and len(results) == 2
    assert sum(result["scheduled_segments"] for result in results) == 1
    assert sorted(result["already_applied"] for result in results) == [False, True]


@pytest.mark.parametrize(
    "overrides",
    [
        {"Бял": "omit"},
        {"бял": []},
        {"бял": "review"},
        {"a" * 65: "keep"},
        {"a" + str(i): "omit" for i in range(1001)},
    ],
)
def test_invalid_overrides_are_safe_user_errors(overrides):
    with pytest.raises(UserError):
        validate_overrides(overrides)
