import json
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("lancedb")

from conftest import export, load, message  # noqa: E402

from telegram_search.indexing.service import SemanticService  # noqa: E402
from telegram_search.indexing.worker import SegmentWorker  # noqa: E402
from telegram_search.search.hybrid import HybridSearch  # noqa: E402
from telegram_search.search.lexical import Filters  # noqa: E402
from telegram_search.search.vectors import VectorStore  # noqa: E402
from telegram_search.shared.errors import UserError  # noqa: E402
from telegram_search.storage.generations import invalidate_segments  # noqa: E402


class TestEncoder:
    __test__ = False

    def __init__(self, space="synthetic", on_encode=None):
        self.space_id = space
        self.spec = SimpleNamespace(profile="small", dimension=4)
        self.space_manifest = {"test": space}
        self.tokenizer = SimpleNamespace(count=lambda text: len(text.split()) + 2)
        self.on_encode = on_encode
        self.calls = []
        self.purposes = []
        self.suspended = False
        self.execution = SimpleNamespace(
            info=lambda: {"device": "cpu", "provider": "synthetic-CPU"}
        )
        self.query_execution = self.execution

    def encode_text(self, texts, purpose, **kwargs):
        self.calls.append(len(texts))
        self.purposes.append(purpose)
        if self.on_encode:
            self.on_encode(texts)
        values = np.array(
            [[1, "поезд" in text, "ремонт" in text, "встреча" in text] for text in texts],
            dtype=np.float32,
        )
        return values / np.linalg.norm(values, axis=1, keepdims=True)

    def unload(self):
        pass

    def unload_index(self):
        pass

    def unload_idle(self, **kwargs):
        return False

    def suspend(self):
        self.suspended = True

    def resume(self):
        self.suspended = False

    def backend_info(self):
        return {"runtime": "synthetic-test", "device": "cpu"}

    def adopt_space(self, manifest_json):
        assert json.loads(manifest_json) == self.space_manifest


def setup_index(db, importer, tmp_path, texts=None):
    texts = texts or [f"поезд синтетический текст {i}" for i in range(40)]
    result = load(
        importer,
        export(
            tmp_path / "export",
            [
                message(
                    i + 1, text, date=1750000000 + i * 10, author="user1" if i % 2 == 0 else "user2"
                )
                for i, text in enumerate(texts)
            ],
        ),
    )
    service = SemanticService(db, importer.lifecycle_lock, start_background=False)
    encoder = TestEncoder()
    service.activate(encoder)
    vectors = VectorStore(db.workspace)
    service.vectors = vectors
    with db.connect() as conn:
        work = conn.execute("SELECT id FROM index_work WHERE state='pending'").fetchone()[0]
    worker = SegmentWorker(db, encoder, vectors, importer.lifecycle_lock)
    return result["chat_id"], service, worker, work


def test_idle_service_keeps_encoder_used_by_ocr_without_search_queries(db, importer, monkeypatch):
    from telegram_search.inference.e5 import E5Encoder

    service = SemanticService(db, importer.lifecycle_lock, start_background=False)
    encoder = E5Encoder.__new__(E5Encoder)
    encoder.condition = threading.Condition()
    encoder.encoding = False
    encoder.last_index_used = time.monotonic()
    encoder.session = object()
    encoder.query_session = object()
    service.encoder = encoder
    service.last_used = time.monotonic() - db.settings.idle_unload_seconds - 10
    service.query_cache["synthetic query"] = [1]
    monkeypatch.setattr(service.wake, "wait", lambda seconds: service.stop.set())
    service._loop()
    assert encoder.session is not None and encoder.query_session is not None
    assert service.query_cache
    with db.connect() as conn:
        assert conn.execute("SELECT error FROM semantic_state WHERE id=1").fetchone()[0] is None


def test_pause_resume_reuses_staged_chunks(db, importer, tmp_path):
    _, service, worker, work = setup_index(db, importer, tmp_path)
    worker.encoder.on_encode = lambda texts: service.control("pause")
    paused = worker.run(work)
    assert paused["state"] == "pending" and paused["chunks_done"] == 0
    assert paused["stage"] == "embedding" and paused["chunks_total"] > 4
    with db.connect() as conn:
        ids = [row[0] for row in conn.execute("SELECT id FROM chunks ORDER BY id")]
    worker.encoder.on_encode = None
    service.control("resume")
    done = worker.run(work)
    assert done["state"] == "done" and done["chunks_done"] == done["chunks_total"]
    with db.connect() as conn:
        assert ids == [row[0] for row in conn.execute("SELECT id FROM chunks ORDER BY id")]
    assert service.status()["pending_segments"] == 0


def test_partial_cpu_index_resumes_with_same_chunk_ids_after_gpu_settings_and_restart(
    db, importer, tmp_path, monkeypatch
):
    from telegram_search.indexing.media import MediaService
    from telegram_search.inference import providers
    from telegram_search.sources.service import WorkspaceService

    _, service, worker, work = setup_index(db, importer, tmp_path)

    def pause_after_one_saved_batch(texts):
        if len(worker.encoder.calls) == 2:
            service.control("pause")

    worker.encoder.on_encode = pause_after_one_saved_batch
    partial = worker.run(work)
    assert 0 < partial["chunks_done"] < partial["chunks_total"]
    with db.connect() as conn:
        old_ids = [row[0] for row in conn.execute("SELECT id FROM chunks ORDER BY id")]
        generation = conn.execute("SELECT target_generation FROM index_segments").fetchone()[0]
    count_before = worker.vectors.table("synthetic", 4).count_rows()
    monkeypatch.setattr(
        providers, "Execution", lambda *args, **kw: SimpleNamespace(info=lambda: {})
    )
    monkeypatch.setattr(SemanticService, "_new_encoder", lambda *args: TestEncoder())
    media = MediaService(db, importer.lifecycle_lock, service, start_background=False)
    media.control("pause")
    WorkspaceService(db, importer, service, media).change_device("gpu", search_device="cpu")
    assert service.status()["paused"] == 1
    assert db.settings.device == "gpu" and db.settings.search_device == "cpu"
    with db.connect() as conn:
        unchanged = dict(conn.execute("SELECT * FROM index_work WHERE id=?", (work,)).fetchone())
        assert unchanged["chunks_done"] == partial["chunks_done"]
        assert (
            conn.execute("SELECT target_generation FROM index_segments").fetchone()[0] == generation
        )
    service.shutdown()
    resumed = SemanticService(db, importer.lifecycle_lock, start_background=False)
    assert resumed.status()["paused"] == 1 and resumed.encoder.space_id == "synthetic"
    resumed.control("resume")
    complete = resumed._worker(resumed.encoder).run(work)
    assert complete["state"] == "done" and complete["chunks_done"] == partial["chunks_total"]
    with db.connect() as conn:
        assert old_ids == [row[0] for row in conn.execute("SELECT id FROM chunks ORDER BY id")]
    assert count_before == partial["chunks_done"]
    assert sum(resumed.encoder.calls) == partial["chunks_total"] - partial["chunks_done"]
    resumed.shutdown()
    media.shutdown()


def test_worker_captured_before_same_space_device_change_cannot_use_old_encoder(
    db, importer, tmp_path
):
    _, service, _, work = setup_index(db, importer, tmp_path)
    old = service.encoder
    captured = service._worker(old)
    old.suspend()
    service.activate(TestEncoder())
    result = captured.run(work)
    assert result["state"] == "paused" and not old.calls
    assert service._worker(service.encoder).run(work)["state"] == "done"


def test_oom_reduces_batch_without_losing_rows(db, importer, tmp_path):
    _, _, worker, work = setup_index(db, importer, tmp_path)

    def limited(texts):
        if len(texts) > 1:
            raise MemoryError("synthetic out of memory")

    worker.encoder.on_encode = limited
    done = worker.run(work)
    assert done["state"] == "done"
    assert worker.batch_size == 1 and worker.encoder.calls[:3] == [4, 2, 1]
    assert worker.vectors.table("synthetic", 4).count_rows() == done["chunks_total"]


def test_crash_after_vector_write_replays_idempotently(db, importer, tmp_path, monkeypatch):
    _, _, worker, work = setup_index(db, importer, tmp_path)
    original = worker.vectors.upsert

    def crash(*args):
        original(*args)
        raise SystemExit("simulated process loss")

    monkeypatch.setattr(worker.vectors, "upsert", crash)
    with pytest.raises(SystemExit):
        worker.run(work)
    with db.connect() as conn:
        assert (
            conn.execute("SELECT chunks_done FROM index_work WHERE id=?", (work,)).fetchone()[0]
            == 0
        )
    monkeypatch.setattr(worker.vectors, "upsert", original)
    done = worker.run(work)
    assert done["state"] == "done"
    assert worker.vectors.table("synthetic", 4).count_rows() == done["chunks_total"]


def test_filters_use_one_witness_and_context_keeps_original(db, importer, tmp_path):
    chat, service, worker, work = setup_index(
        db, importer, tmp_path, ["поезд " + "🙂" * 2000, "ремонт другой автор"]
    )
    assert worker.run(work)["state"] == "done"
    search = HybridSearch(db, service, importer.lifecycle_lock)
    assert (
        search.search("поезд", Filters(author_ids=["user1"], date_from=1750000010), mode="meaning")[
            "results"
        ]
        == []
    )
    response = search.search("поезд", Filters(author_ids=["user2"]), mode="hybrid")
    assert response["effective_mode"] == "hybrid" and response["results"]
    hit = response["results"][0]
    assert hit["chat_id"] == chat and hit["message_id"] == 2
    assert hit["messages"][0]["text"].endswith("🙂" * 2000)
    assert not hit["messages"][0]["matches_filters"]
    assert hit["messages"][1]["matches_filters"]
    assert set(hit["matched_by"]) == {"words", "meaning"}


def test_model_switch_is_explicit_and_only_current_generation_is_eligible(db, importer, tmp_path):
    chat, service, worker, work = setup_index(db, importer, tmp_path)
    assert worker.run(work)["state"] == "done"
    with pytest.raises(UserError, match="переиндексации"):
        service.activate(TestEncoder("other"))
    service.activate(TestEncoder("other"), reindex=True)
    with db.connect() as conn:
        predicate, params = HybridSearch.eligible_sql(Filters(), "synthetic")
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM chunks c JOIN index_segments s "
                f"ON s.chat_id=c.chat_id AND s.utc_day=c.utc_day WHERE {predicate}",
                params,
            ).fetchone()[0]
            == 0
        )
        pending = conn.execute("SELECT id FROM index_work WHERE state='pending'").fetchone()[0]
    updated = SegmentWorker(db, service.encoder, service.vectors, importer.lifecycle_lock).run(
        pending
    )
    assert updated["state"] == "done"
    assert service.vectors.table("synthetic", 4).count_rows() == 0
    assert service.vectors.table("other", 4).count_rows() == updated["chunks_total"]
    with db.connect() as conn:
        day = conn.execute(
            "SELECT utc_day FROM index_segments WHERE chat_id=?", (chat,)
        ).fetchone()[0]
        invalidate_segments(conn, chat, {day}, "test-edit")
    assert service.status()["pending_segments"] == 1


def test_reimport_drains_delete_before_new_vectors(db, importer, tmp_path):
    chat, service, worker, work = setup_index(db, importer, tmp_path)
    assert worker.run(work)["state"] == "done"
    with importer.lifecycle_lock, db.connect() as conn:
        conn.execute("INSERT INTO vector_deletions VALUES(?,0,NULL)", (chat,))
        conn.execute("DELETE FROM chats WHERE id=?", (chat,))
    result = load(importer, export(tmp_path / "new", [message(1, "ремонт новый")]))
    assert result["chat_id"] == chat
    with db.connect() as conn:
        pending = conn.execute("SELECT id FROM index_work WHERE state='pending'").fetchone()[0]
    done = worker.run(pending)
    assert done["state"] == "done" and done["chunks_total"] == 1
    service.cleanup(compact=True)
    assert worker.vectors.table("synthetic", 4).count_rows() == 1


def test_startup_recovers_running_work_without_auto_download(db, importer, tmp_path, monkeypatch):
    _, service, _, work = setup_index(db, importer, tmp_path)
    with db.connect() as conn:
        conn.execute("UPDATE index_work SET state='running' WHERE id=?", (work,))
        conn.execute("UPDATE semantic_state SET preparation_state='downloading'")
    monkeypatch.setattr(SemanticService, "_load_existing", lambda self: None)
    resumed = SemanticService(db, threading.RLock(), start_background=False)
    assert resumed.status()["preparation_state"] == "interrupted"
    with db.connect() as conn:
        assert (
            conn.execute("SELECT state FROM index_work WHERE id=?", (work,)).fetchone()[0]
            == "pending"
        )
    assert resumed.encoder is None and service.encoder is not None


def test_chunk_card_includes_match_in_eighth_message(db, importer, tmp_path):
    _, service, worker, work = setup_index(
        db, importer, tmp_path, ["обычная реплика"] * 7 + ["уникальный ремонт"]
    )
    assert worker.run(work)["state"] == "done"
    response = HybridSearch(db, service, importer.lifecycle_lock).search("ремонт", mode="hybrid")
    hit = response["results"][0]
    assert [part["message_id"] for part in hit["matched_parts"]] == list(range(1, 9))
    assert any(item["message_id"] == 8 and "ремонт" in item["text"] for item in hit["messages"])
    assert len(hit["messages"]) <= 10


def test_small_chunk_display_keeps_query_evidence_and_filter_witness(db, importer, tmp_path):
    _, service, worker, work = setup_index(
        db, importer, tmp_path, ["обычная реплика"] * 7 + ["уникальный ремонт"]
    )
    assert worker.run(work)["state"] == "done"
    search = HybridSearch(db, service, importer.lifecycle_lock)
    for mode in ("hybrid", "meaning"):
        hit = search.search("ремонт", mode=mode, chunk_size=1)["results"][0]
        assert hit["message_id"] == 8 and [row["message_id"] for row in hit["messages"]] == [8]
        assert [part["message_id"] for part in hit["matched_parts"]] == list(range(1, 9))
        filtered = search.search("ремонт", Filters(author_ids=["user1"]), mode=mode, chunk_size=1)[
            "results"
        ][0]
        assert len(filtered["messages"]) == 1 and filtered["messages"][0]["matches_filters"]


def test_model_activation_preserves_user_pause(db, importer, tmp_path):
    _, service, _, _ = setup_index(db, importer, tmp_path)
    service.control("pause")
    service.activate(TestEncoder("other"), reindex=True)
    assert service.status()["paused"] == 1


def test_model_switch_between_status_and_query_falls_back_with_warning(
    db, importer, tmp_path, monkeypatch
):
    _, service, worker, work = setup_index(db, importer, tmp_path)
    assert worker.run(work)["state"] == "done"
    original = service.status
    switched = False

    def status():
        nonlocal switched
        value = original()
        if not switched:
            switched = True
            service.activate(TestEncoder("other"), reindex=True)
        return value

    monkeypatch.setattr(service, "status", status)
    result = HybridSearch(db, service, importer.lifecycle_lock).search("поезд", mode="hybrid")
    assert result["effective_mode"] == "words" and result["results"] and result["warnings"]


def test_failed_new_model_restores_old_encoder_and_user_pause(db, importer, tmp_path, monkeypatch):
    from telegram_search.inference.bundles import BundleStore

    _, service, _, _ = setup_index(db, importer, tmp_path)
    old = service.encoder
    service.control("pause")
    new = TestEncoder("other", on_encode=lambda texts: (_ for _ in ()).throw(MemoryError()))
    unloaded = []
    new.unload = lambda: unloaded.append(True)
    monkeypatch.setattr(BundleStore, "prepare", lambda *args, **kwargs: tmp_path)
    monkeypatch.setattr(service, "_new_encoder", lambda profile: new)
    service._prepare(SimpleNamespace(profile="base"), True, True, False, None)
    assert service.encoder is old and not old.suspended
    assert not service.preparing_encoder.is_set() and unloaded == [True]
    assert service.status()["preparation_state"] == "failed"
    assert service.status()["paused"] == 1


def test_preparation_checks_indexing_and_query_devices(db, importer, tmp_path, monkeypatch):
    from telegram_search.inference.bundles import BundleStore

    service = SemanticService(db, importer.lifecycle_lock, start_background=False)
    encoder = TestEncoder()
    monkeypatch.setattr(BundleStore, "prepare", lambda *args, **kwargs: tmp_path)
    monkeypatch.setattr(service, "_new_encoder", lambda profile: encoder)
    service._prepare(SimpleNamespace(profile="small"), False, True, False, None)
    assert encoder.purposes == ["passage", "query"]
    assert service.encoder is encoder
    assert service.status()["preparation_state"] == "ready"


def test_failed_queue_has_no_eta_and_exposes_error_until_explicit_retry(db, importer, tmp_path):
    _, service, worker, work = setup_index(db, importer, tmp_path)
    worker.encoder.on_encode = lambda texts: (_ for _ in ()).throw(
        UserError("Синтетическая ошибка GPU.")
    )
    assert worker.run(work)["state"] == "failed"
    service.activate(worker.encoder)
    status = service.status()
    assert status["index_error"] == "Синтетическая ошибка GPU."
    assert status["estimated_remaining_seconds"] is None
    assert status["works"][0]["state"] == "failed"
    service.control("retry")
    assert service.status()["index_error"] is None
    worker.encoder.on_encode = None
    assert worker.run(work)["state"] == "done"


def test_first_failed_batch_pauses_queue_without_failing_other_days(
    db, importer, tmp_path, monkeypatch
):
    path = export(tmp_path / "export", [message(1, date=1750000000), message(2, date=1750086400)])
    load(importer, path)
    service = SemanticService(db, importer.lifecycle_lock, start_background=False)
    encoder = TestEncoder(on_encode=lambda texts: (_ for _ in ()).throw(UserError("Сбой GPU.")))
    service.activate(encoder)
    monkeypatch.setattr(service.wake, "wait", lambda seconds: service.stop.set())
    service._loop()
    status = service.status()
    assert status["paused"] == 1 and status["error"] == "Сбой GPU."
    assert {work["state"]: work["count"] for work in status["works"]} == {"failed": 1, "pending": 1}


def test_model_preparation_interruption_requeues_worker(db, importer, tmp_path):
    _, service, worker, work = setup_index(db, importer, tmp_path)
    worker.should_stop = service.preparing_encoder.is_set

    def interrupted(texts):
        service.preparing_encoder.set()
        raise UserError("Модель временно остановлена")

    worker.encoder.on_encode = interrupted
    result = worker.run(work)
    assert result["state"] == "pending" and result["error"] is None


def test_activation_commit_failure_keeps_original_encoder(db, importer, tmp_path, monkeypatch):
    _, service, _, _ = setup_index(db, importer, tmp_path)
    old = service.encoder
    reference = old.encode_text(["поезд"], "query")
    unloaded = []
    old.unload = lambda: unloaded.append(True)
    original = db.connect

    @contextmanager
    def failing_commit():
        with original() as conn:
            yield conn
            raise RuntimeError("synthetic commit failure")

    monkeypatch.setattr(db, "connect", failing_commit)
    with pytest.raises(RuntimeError, match="commit failure"):
        service.activate(TestEncoder("other"), reindex=True)
    monkeypatch.setattr(db, "connect", original)
    assert service.encoder is old and unloaded == [True]
    np.testing.assert_array_equal(old.encode_text(["поезд"], "query"), reference)
    with db.connect() as conn:
        assert (
            conn.execute("SELECT active_space_id FROM semantic_state").fetchone()[0] == old.space_id
        )
        assert not conn.execute("SELECT 1 FROM embedding_spaces WHERE id='other'").fetchone()


def test_oom_backoff_survives_new_day_worker_and_explicit_batch_resets_it(db, importer, tmp_path):
    from telegram_search.indexing.chats import ChatIndexing
    from telegram_search.indexing.media import MediaService

    path = export(
        tmp_path / "export",
        [message(i, "long synthetic text " * 1000, date=1750000000 + i * 86400) for i in (1, 2)],
    )
    chat = load(importer, path)["chat_id"]
    service = SemanticService(db, importer.lifecycle_lock, start_background=False)

    def limited(texts):
        if len(texts) > 2:
            raise RuntimeError("Available memory of 10 is smaller than requested bytes of 20")

    encoder = TestEncoder(on_encode=limited)
    service.activate(encoder)
    with db.connect() as conn:
        work = [
            row[0]
            for row in conn.execute(
                "SELECT id FROM index_work WHERE state='pending' ORDER BY utc_day"
            )
        ]
    assert service._worker(encoder).run(work[0])["state"] == "done"
    assert encoder.index_batch_limit == 2
    encoder.calls.clear()
    assert service._worker(encoder).run(work[1])["state"] == "done"
    assert max(encoder.calls) <= 2
    media = MediaService(db, importer.lifecycle_lock, service, start_background=False)
    ChatIndexing(db, service, media, importer.lifecycle_lock).settings(chat, {"embedding_batch": 8})
    assert encoder.index_batch_limit is None


@pytest.mark.parametrize("mutation", ["retry", "delete", "reimport"])
def test_stale_failed_result_cannot_pause_changed_queue(
    db, importer, tmp_path, monkeypatch, mutation
):
    path, service, worker, _ = setup_index(db, importer, tmp_path)
    original = worker.run
    worker.encoder.on_encode = lambda texts: (_ for _ in ()).throw(UserError("Старый сбой."))

    def run(work):
        result = original(work)
        assert result["state"] == "failed"
        if mutation == "retry":
            service.control("retry")
        elif mutation == "delete":
            with importer.lifecycle_lock, db.connect() as conn:
                conn.execute("DELETE FROM chats")
        else:
            with db.connect() as conn:
                chat = conn.execute("SELECT id FROM chats LIMIT 1").fetchone()[0]
            with importer.lifecycle_lock, db.connect() as conn:
                invalidate_segments(conn, chat, {result["utc_day"]}, "synthetic_reimport")
        service.stop.set()
        return result

    worker.run = run
    monkeypatch.setattr(service, "_worker", lambda encoder: worker)
    monkeypatch.setattr(service.wake, "wait", lambda seconds: service.stop.set())
    service._loop()
    assert not service.status()["paused"]
    assert service.status()["error"] is None
