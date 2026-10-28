import json
import threading
import time
from types import SimpleNamespace

import pytest
from conftest import export, load, message
from test_noise_analysis import start_ready

from telegram_search.indexing.noise_analysis import NoiseAnalysisService
from telegram_search.indexing.noise_comparisons import NoiseComparisons
from telegram_search.indexing.noise_dry_run import NoiseDryRun, TokenContract, TokenizerUnavailable
from telegram_search.indexing.noise_policy import NoisePolicyService
from telegram_search.search.chunking.noise.contracts import DryRunOptions
from telegram_search.shared.errors import NoiseDraftConflict, UserError
from telegram_search.storage.noise import NoiseReport


@pytest.fixture
def services(db, importer):
    analyses = NoiseAnalysisService(db)
    policies = NoisePolicyService(db, analyses, importer.lifecycle_lock)
    comparisons = NoiseComparisons(policies)
    try:
        yield analyses, policies, comparisons
    finally:
        comparisons.close()
        analyses.close()


def prepare(db, importer, tmp_path, services, monkeypatch, number=100):
    analyses, policies, _ = services
    chat = load(
        importer,
        export(
            tmp_path / str(number),
            [
                message(1, "Содержательная фраза"),
                message(2, "бял"),
                message(3, "сука"),
            ],
            chat_id=number,
        ),
    )["chat_id"]
    with db.connect() as conn:
        conn.execute("INSERT OR IGNORE INTO embedding_spaces VALUES('synthetic','small','{}',4,0)")
        conn.execute("UPDATE semantic_state SET active_space_id='synthetic',enabled=1,paused=1")
        conn.execute("UPDATE chats SET text_batch=3,text_paused=1 WHERE id=?", (chat,))
    job = start_ready(analyses, chat)["id"]
    policies.save_overrides(chat, job, 0, {"бял": "omit"})
    contract = TokenContract(
        SimpleNamespace(count=lambda text: len(text.split()) + 2),
        "synthetic",
        "synthetic/test",
        "passage: ",
        480,
        3,
        "synthetic",
    )
    monkeypatch.setattr(
        "telegram_search.indexing.noise_dry_run.local_token_contract", lambda *a: contract
    )
    return chat, job


def wait(service, chat, parent, job, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with service.lock:
            owned = job in service.jobs
        if not owned or service.persistence_error:
            return service.status(chat, parent, job)
        time.sleep(0.01)
    pytest.fail("Synthetic comparison did not finish")


def test_real_background_comparison_is_durable_scoped_and_does_not_change_index(
    db, importer, tmp_path, services, monkeypatch
):
    chat, parent = prepare(db, importer, tmp_path, services, monkeypatch)
    analyses, _, service = services
    report = NoiseReport(db, parent, json.loads(analyses.store.get(chat, parent)["fence_json"]))
    digest = report.digest()
    with db.connect() as conn:
        before = {
            table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table}")]
            for table in ("messages", "chats", "index_work", "index_segments", "noise_analyses")
        }
    assert service.latest(chat, parent) == {"comparison": None}
    assert service.thread is None
    created = service.create(chat, parent, 1)
    result = wait(service, chat, parent, created["id"])
    assert result["state"] == "ready" and not result["active"]
    assert result["summary"]["delta"]["removed_own_messages"] == 1
    assert result["summary"]["complete"] and report.digest() == digest
    assert service.latest(chat, parent)["comparison"] == result
    with db.connect() as conn:
        assert all(
            rows == [tuple(row) for row in conn.execute(f"SELECT * FROM {table}")]
            for table, rows in before.items()
        )
    with pytest.raises(KeyError):
        service.status("other", parent, result["id"])
    with pytest.raises(KeyError):
        service.status(chat, "0" * 32, result["id"])
    assert "бял" not in json.dumps(result, ensure_ascii=False)
    service.close()
    reopened = NoiseComparisons(services[1])
    try:
        assert reopened.latest(chat, parent)["comparison"] == result
    finally:
        reopened.close()


def test_latest_replacement_is_bounded_and_restart_recovers_unowned_work(
    db, importer, tmp_path, services, monkeypatch
):
    chat, parent = prepare(db, importer, tmp_path, services, monkeypatch)
    service = services[2]
    first = service.create(chat, parent, 1)["id"]
    assert wait(service, chat, parent, first)["state"] == "ready"
    second = service.create(chat, parent, 1)["id"]
    assert wait(service, chat, parent, second)["state"] == "ready"
    assert first != second
    with pytest.raises(KeyError):
        service.status(chat, parent, first)
    service.close()
    crashed = service.store.create(chat, parent, 1, DryRunOptions())["id"]
    recovered = NoiseComparisons(services[1])
    try:
        status = recovered.status(chat, parent, crashed)
        assert status["state"] == "interrupted" and status["summary"] is None
        assert recovered.thread is None
        with db.connect() as conn:
            assert conn.execute("SELECT count(*) FROM noise_dry_runs").fetchone()[0] == 1
    finally:
        recovered.close()


def test_active_comparison_guards_parent_deletion_and_cancellation_clears_result(
    db, importer, tmp_path, services, monkeypatch
):
    chat, parent = prepare(db, importer, tmp_path, services, monkeypatch)
    entered = threading.Event()
    original = NoiseDryRun.compare

    def held(self, *args, **kwargs):
        entered.set()
        while not kwargs["cancelled"]():
            time.sleep(0.005)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(NoiseDryRun, "compare", held)
    service = services[2]
    created = service.create(chat, parent, 1)
    assert entered.wait(5)
    with pytest.raises(UserError, match="уже выполняется"):
        service.create(chat, parent, 1)
    with pytest.raises(UserError, match="остановите сравнение"):
        services[0].delete(chat, parent)
    service.cancel(chat, parent, created["id"])
    result = wait(service, chat, parent, created["id"])
    assert result["state"] == "cancelled" and result["summary"] is None
    assert service.thread.is_alive()
    services[0].delete(chat, parent)
    with db.connect() as conn:
        assert not conn.execute("SELECT 1 FROM noise_dry_runs").fetchone()


@pytest.mark.parametrize("change", ["source", "draft", "batch", "model"])
def test_changes_between_admission_and_model_loading_are_stale(
    db, importer, tmp_path, services, monkeypatch, change
):
    chat, parent = prepare(db, importer, tmp_path, services, monkeypatch)
    original = NoiseDryRun.compare

    def changed(self, *args, **kwargs):
        with db.connect() as conn:
            if change == "source":
                conn.execute("UPDATE chats SET revision=revision+1 WHERE id=?", (chat,))
            elif change == "draft":
                conn.execute(
                    "UPDATE noise_analyses SET draft_revision=draft_revision+1 WHERE id=?",
                    (parent,),
                )
            elif change == "batch":
                conn.execute("UPDATE chats SET text_batch=8 WHERE id=?", (chat,))
            else:
                conn.execute("UPDATE semantic_state SET active_space_id=NULL")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(NoiseDryRun, "compare", changed)
    service = services[2]
    created = service.create(chat, parent, 1)
    result = wait(service, chat, parent, created["id"])
    assert result["state"] == "stale" and result["summary"] is None
    assert not result["active"] and service.thread.is_alive()


def test_unavailable_tokenizer_is_a_safe_retryable_failure(
    db, importer, tmp_path, services, monkeypatch
):
    chat, parent = prepare(db, importer, tmp_path, services, monkeypatch)

    def absent(*args):
        raise TokenizerUnavailable("private path must not escape")

    monkeypatch.setattr("telegram_search.indexing.noise_dry_run.local_token_contract", absent)
    service = services[2]
    created = service.create(chat, parent, 1)
    result = wait(service, chat, parent, created["id"])
    assert result["error"] == "tokenizer_unavailable" and result["state"] == "failed"
    assert result["summary"] is None and "private" not in json.dumps(result)
    assert service.thread.is_alive()


def test_terminal_publication_rechecks_source_and_discards_late_stale_result(
    db, importer, tmp_path, services, monkeypatch
):
    chat, parent = prepare(db, importer, tmp_path, services, monkeypatch)
    original = NoiseDryRun.compare

    def changed_after_compute(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        with db.connect() as conn:
            conn.execute("UPDATE chats SET revision=revision+1 WHERE id=?", (chat,))
        return result

    monkeypatch.setattr(NoiseDryRun, "compare", changed_after_compute)
    service = services[2]
    created = service.create(chat, parent, 1)
    result = wait(service, chat, parent, created["id"])
    assert result["state"] == "stale" and result["summary"] is None


def test_busy_terminal_publication_can_be_cancelled_without_killing_worker(
    db, importer, tmp_path, services, monkeypatch
):
    chat, parent = prepare(db, importer, tmp_path, services, monkeypatch)
    computed, publish = threading.Event(), threading.Event()
    original = NoiseDryRun.compare

    def held(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        computed.set()
        assert publish.wait(5)
        return result

    monkeypatch.setattr(NoiseDryRun, "compare", held)
    service = services[2]
    created = service.create(chat, parent, 1)
    assert computed.wait(5)
    with db.connect() as writer:
        writer.execute("BEGIN IMMEDIATE")
        publish.set()
        time.sleep(0.25)
        waiting = service.status(chat, parent, created["id"])
        assert waiting["active"] and waiting["summary"] is None and waiting["state"] == "running"
        service.cancel(chat, parent, created["id"])
    result = wait(service, chat, parent, created["id"])
    assert result["state"] == "cancelled" and result["summary"] is None
    assert service.thread.is_alive()


def test_permanent_publication_failure_is_bounded_and_stops_admission(
    db, importer, tmp_path, services, monkeypatch
):
    chat, parent = prepare(db, importer, tmp_path, services, monkeypatch)
    service = services[2]
    with db.connect() as conn:
        conn.execute(
            "CREATE TRIGGER private_failure BEFORE UPDATE OF state ON noise_dry_runs "
            "WHEN NEW.state IN ('ready','limited','failed') "
            "BEGIN SELECT RAISE(ABORT,'private path'); END"
        )
    attempts = []
    original = service.store.publish

    def counted(conn, job):
        attempts.append(job.state)
        original(conn, job)

    monkeypatch.setattr(service.store, "publish", counted)
    created = service.create(chat, parent, 1)
    result = wait(service, chat, parent, created["id"])
    assert attempts == ["ready", "failed"]
    assert result["state"] == "failed" and result["error"] == "result_save_failed"
    assert result["summary"] is None and not result["active"]
    with pytest.raises(UserError, match="Перезапустите"):
        service.create(chat, parent, 1)
    with pytest.raises(UserError, match="Перезапустите"):
        services[0].delete(chat, parent)


def test_oversized_author_id_is_bounded_before_python_and_marks_source_incomplete(
    db, importer, tmp_path, services, monkeypatch
):
    chat = load(importer, export(tmp_path / "long-id", [message(1, "бял", author="x" * 60000)]))[
        "chat_id"
    ]
    analyses = services[0]
    pages = []
    original = analyses._source_page

    def checked(*args):
        rows = original(*args)
        for row in rows:
            assert len(row["author_id"] or "") <= 1025
            pages.append(row["metadata_limited"])
        return rows

    monkeypatch.setattr(analyses, "_source_page", checked)
    state = start_ready(analyses, chat)
    assert state["state"] == "ready" and not state["summary"]["source_complete"]
    assert state["summary"]["scan_complete"]
    assert state["summary"]["limited_messages"] == 1 and pages == [1]
    with db.connect() as conn:
        assert (
            len(
                conn.execute("SELECT author_id FROM messages WHERE chat_id=?", (chat,)).fetchone()[
                    0
                ]
            )
            == 60000
        )


def test_creation_checks_revision_and_no_model_or_partial_reports_do_not_create_work(
    db, importer, tmp_path, services, monkeypatch
):
    chat, parent = prepare(db, importer, tmp_path, services, monkeypatch)
    service = services[2]
    with pytest.raises(NoiseDraftConflict):
        service.create(chat, parent, 0)
    with pytest.raises(UserError):
        service.create(chat, parent, True)
    with db.connect() as conn:
        conn.execute("UPDATE semantic_state SET active_space_id=NULL")
    with pytest.raises(UserError, match="токенизатор"):
        service.create(chat, parent, 1)
    assert service.thread is None
    with db.connect() as conn:
        assert not conn.execute("SELECT 1 FROM noise_dry_runs").fetchone()


def test_parent_disappearance_cannot_grow_queue_or_unlink_owned_report(
    db, importer, tmp_path, services, monkeypatch
):
    from telegram_search.storage.noise import report_path

    chat, first_parent = prepare(db, importer, tmp_path, services, monkeypatch)
    analyses, policies, service = services
    parents = [first_parent]
    for _ in range(7):
        parent = start_ready(analyses, chat)["id"]
        policies.save_overrides(chat, parent, 0, {"бял": "omit"})
        parents.append(parent)
    entered, release = threading.Event(), threading.Event()
    original = NoiseDryRun.compare

    def held(self, *args, **kwargs):
        entered.set()
        while not release.wait(0.005):
            if kwargs["cancelled"]():
                break
        return original(self, *args, **kwargs)

    monkeypatch.setattr(NoiseDryRun, "compare", held)
    try:
        service.create(chat, parents[0], 1)
        assert entered.wait(5)
        for parent in parents[1:]:
            service.create(chat, parent, 1)
        assert len(service.jobs) == 8
        # Simulate removal by a canonical FK cascade, which bypasses the
        # report-delete API guard. A still-owned report must survive pruning.
        with db.connect() as conn:
            conn.execute("DELETE FROM noise_analyses WHERE id=?", (parents[1],))
        replacement = start_ready(analyses, chat)["id"]
        assert report_path(db.workspace, parents[1]).is_file()
        policies.save_overrides(chat, replacement, 0, {"бял": "omit"})
        with pytest.raises(UserError, match="8 сравнений"):
            service.create(chat, replacement, 1)
        assert len(service.jobs) == 8 and len(service.queue) <= 7
    finally:
        release.set()
