"""Synthetic mixed tables: clear one partition, retain sources and resume safely."""

import threading
from types import SimpleNamespace

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("lancedb")

from conftest import load  # noqa: E402
from test_media import fake_ocr, photo_export  # noqa: E402
from test_semantic import setup_index  # noqa: E402

from telegram_search.indexing.chats import ChatIndexing  # noqa: E402
from telegram_search.indexing.media import MediaService  # noqa: E402
from telegram_search.inference.clip import ClipEncoder  # noqa: E402
from telegram_search.search.cache import SearchCache  # noqa: E402
from telegram_search.search.lexical import Filters, SearchService  # noqa: E402
from telegram_search.security.paths import owned_vector_path  # noqa: E402
from telegram_search.shared.errors import UserError  # noqa: E402
from telegram_search.sources.service import WorkspaceService  # noqa: E402
from telegram_search.storage.vector_maintenance import VectorMaintenance, disk_bytes  # noqa: E402


def wait_finished(maintenance):
    maintenance.worker.join(timeout=10)
    assert not maintenance.worker.is_alive()
    assert maintenance.status()["state"] == "done", maintenance.status()


@pytest.fixture
def mixed(db, importer, tmp_path):
    chat, semantic, worker, work = setup_index(db, importer, tmp_path, ["поезд синтетический"])
    assert worker.run(work)["state"] == "done"
    photo_chat = load(importer, photo_export(tmp_path / "photos", chat_id=200))["chat_id"]
    media = MediaService(db, importer.lifecycle_lock, semantic, start_background=False)
    calls = fake_ocr(media, "Поезд и расписание 12345")
    assert media._ocr_one() and media._ocr_embeddings()
    media.clip = SimpleNamespace(
        space_id="visual", execution=SimpleNamespace(info=lambda: {"device": "cpu"})
    )
    with db.connect() as conn:
        sha = conn.execute("SELECT sha256 FROM media_blobs").fetchone()[0]
    media._publish("visual", 4, "image", [sha], np.array([[1.0, 0.0, 0.0, 0.0]], dtype=np.float32))
    maintenance = VectorMaintenance(db, importer.lifecycle_lock, semantic, media)
    yield chat, photo_chat, semantic, media, calls, maintenance
    maintenance.close()


@pytest.mark.parametrize("kind", ["text", "images", "ocr"])
def test_clear_one_vector_kind_preserves_other_partitions_and_can_resume(db, importer, mixed, kind):
    chat, photo_chat, semantic, media, calls, maintenance = mixed
    store = semantic._vector_store()
    before = maintenance.inventory()["indexes"]
    assert all(value["vectors"] > 0 for value in before.values())
    with db.connect() as conn:
        messages = [tuple(r) for r in conn.execute("SELECT * FROM messages")]
        ocr = [tuple(r) for r in conn.execute("SELECT * FROM ocr_cache")]
        chunks = [tuple(r) for r in conn.execute("SELECT * FROM chunks")]
    cache_revision = SearchCache.revision(db)
    maintenance.start(kind)
    wait_finished(maintenance)
    after = maintenance.inventory()["indexes"]
    assert after[kind]["vectors"] == 0
    for retained in {"text", "images", "ocr"} - {kind}:
        assert after[retained]["vectors"] == before[retained]["vectors"]
    assert SearchCache.revision(db) != cache_revision
    with db.connect() as conn:
        assert messages == [tuple(r) for r in conn.execute("SELECT * FROM messages")]
        assert ocr == [tuple(r) for r in conn.execute("SELECT * FROM ocr_cache")]
        assert chunks == [tuple(r) for r in conn.execute("SELECT * FROM chunks")]
        assert conn.execute("SELECT COUNT(*) FROM ocr_cache WHERE state='ready'").fetchone()[0] == 1
        assert conn.execute("SELECT ocr_paused FROM media_state").fetchone()[0] == 0
        if kind == "text":
            assert (
                ChatIndexing(db, semantic, media, importer.lifecycle_lock).status(chat)["chunking"][
                    "profile"
                ]
                == "episodes"
            )
            assert semantic.status()["ready_segments"] == 0
            assert not conn.execute("SELECT 1 FROM chats WHERE text_paused=0").fetchone()
        if kind == "images":
            assert store.table("visual", 4) is None
            assert disk_bytes(store.table_path("visual")) == 0
            assert maintenance.status()["freed_bytes"] > 0
    # Both lexical sources remain usable without regenerating any embeddings.
    assert SearchService(db).search("поезд", Filters())["results"]
    assert len(calls) == 1
    if kind == "text":
        ChatIndexing(db, semantic, media, importer.lifecycle_lock).control(chat, "text", "resume")
        with db.connect() as conn:
            work = conn.execute(
                "SELECT id FROM index_work WHERE chat_id=? AND state='pending'", (chat,)
            ).fetchone()[0]
        assert semantic._worker(semantic.encoder).run(work)["state"] == "done"
        assert maintenance.inventory()["indexes"]["ocr"]["vectors"] == before["ocr"]["vectors"]
    elif kind == "ocr":
        with db.connect() as conn:
            conn.execute("UPDATE media_state SET ocr_dense_paused=0")
            conn.execute("UPDATE chats SET ocr_dense_paused=0 WHERE id=?", (photo_chat,))
        assert media._ocr_embeddings()
        assert len(calls) == 1
        assert maintenance.inventory()["indexes"]["ocr"]["vectors"] == before["ocr"]["vectors"]


def test_clear_waits_for_running_stage_and_keeps_status_responsive(db, mixed):
    *_, media, _, maintenance = mixed
    media._set_active("clip", True)
    maintenance.start("images")
    try:
        assert maintenance.status()["state"] == "waiting"
        with pytest.raises(UserError, match="Дождитесь"):
            maintenance.start("text")
        assert maintenance.inventory()["indexes"]["images"]["vectors"] == 1
        media._set_active("clip", False)
        wait_finished(maintenance)
    finally:
        media._set_active("clip", False)


def test_clear_failure_invalidates_ready_data_but_preserves_other_vectors(db, mixed, monkeypatch):
    _, _, semantic, _, _, maintenance = mixed

    def fail(*args, **kwargs):
        raise OSError("synthetic disk failure with private path")

    monkeypatch.setattr(semantic._vector_store(), "clear", fail)
    maintenance.start("text")
    maintenance.worker.join(timeout=5)
    assert maintenance.status()["state"] == "failed"
    assert "private path" not in maintenance.status()["error"]
    assert semantic.status()["ready_segments"] == 0
    assert semantic.status()["paused"] == 1
    assert maintenance.inventory()["indexes"]["ocr"]["vectors"] > 0


def test_clear_rejects_symlink_table_and_keeps_external_data(mixed, tmp_path):
    _, _, semantic, _, _, maintenance = mixed
    store = semantic._vector_store()
    target = store.table_path("visual")
    original = target.with_name("visual-original.lance")
    target.rename(original)
    try:
        target.symlink_to(original, target_is_directory=True)
    except OSError:
        original.rename(target)
        pytest.skip("symlinks unavailable")
    try:
        maintenance.start("images")
        maintenance.worker.join(timeout=5)
        assert maintenance.status()["state"] == "failed"
        assert disk_bytes(original) > 0
    finally:
        target.unlink()
        original.rename(target)


def test_vector_paths_reject_windows_reparse_points_before_native_io(tmp_path, monkeypatch):
    import stat
    from pathlib import Path

    root = tmp_path.resolve()
    junction = root / "data" / "vectors" / "text_synthetic.lance"
    actual = Path.lstat

    def attributes(path, *args, **kwargs):
        if path == junction:
            return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
        return actual(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", attributes)
    with pytest.raises(UserError, match="junction"):
        owned_vector_path(root, junction.relative_to(root))


def test_clip_memory_limit_and_session_reset_are_atomic_for_queries(db, importer, monkeypatch):
    from test_media import services

    semantic, media = services(db, importer)
    clip = ClipEncoder.__new__(ClipEncoder)
    attempted = threading.Event()
    contended = []

    class ObservedLock:
        def __init__(self):
            self.raw = threading.RLock()

        def __enter__(self):
            if threading.current_thread().name == "synthetic-query":
                acquired = self.raw.acquire(blocking=False)
                contended.append(not acquired)
                attempted.set()
                if not acquired:
                    self.raw.acquire()
            else:
                self.raw.acquire()
            return self

        def __exit__(self, *args):
            self.raw.release()

    clip.lock = ObservedLock()
    clip.threads = 4
    clip.execution = SimpleNamespace(memory_limit_mib=4096, threads=4)
    clip.query_execution = SimpleNamespace(memory_limit_mib=4096, threads=4)
    clip.sessions = {"text": SimpleNamespace(cap=4096)}
    clip.tokenizer = SimpleNamespace(
        batch=lambda *args, **kwargs: {
            "input_ids": np.ones((1, 2), dtype=np.int64),
            "attention_mask": np.ones((1, 2), dtype=np.int64),
        }
    )
    clip.projection = np.ones((512, 2), dtype=np.float32)

    def session(kind):
        if kind not in clip.sessions:
            clip.sessions[kind] = SimpleNamespace(
                cap=clip.query_execution.memory_limit_mib,
                run=lambda *args: [np.ones((1, 2, 2), dtype=np.float32)],
            )
        return clip.sessions[kind]

    clip._session = session
    media.clip = clip
    result = []
    query = threading.Thread(
        target=lambda: result.append(clip.encode_text(["synthetic query"])), name="synthetic-query"
    )

    def collect():
        query.start()
        assert attempted.wait(3)

    monkeypatch.setattr("telegram_search.inference.clip.gc.collect", collect)
    WorkspaceService(db, importer, semantic, media).update_settings({"gpu_memory_limit_mib": 6144})
    query.join(timeout=3)
    assert not query.is_alive() and len(result) == 1
    assert contended == [True]
    assert clip.sessions["text"].cap == 6144
