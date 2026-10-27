import json
import sqlite3
import threading
import time
from contextlib import closing

import pytest
from conftest import export, load, message

from telegram_search.indexing.exclusions import update_authors
from telegram_search.indexing.noise_analysis import NoiseAnalysisService
from telegram_search.search.chunking.noise.contracts import AnalysisOptions
from telegram_search.shared.errors import UserError
from telegram_search.storage.noise import NoiseReport, NoiseStore, report_path


def wait_terminal(service, chat, job_id, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with service.lock:
            active = job_id in service.jobs
        if not active:
            return service.status(chat, job_id)
        time.sleep(0.01)
    pytest.fail("Synthetic noise analysis did not finish")


@pytest.fixture
def analysis(db):
    service = NoiseAnalysisService(db)
    try:
        yield service
    finally:
        service.close()


def chat(importer, tmp_path, messages=None, chat_id=100):
    return load(
        importer, export(tmp_path / str(chat_id), messages or [message(text="бял")], chat_id)
    )["chat_id"]


def start_ready(service, chat_id, options=None):
    job = service.create(chat_id, options)
    result = wait_terminal(service, chat_id, job["id"])
    assert result["state"] in {"ready", "limited"}, result
    return result


def test_cpu_analysis_aggregates_rare_variants_and_preserves_source_and_index_state(
    db, importer, tmp_path, analysis
):
    chat_id = chat(
        importer,
        tmp_path,
        [
            message(1, "сука"),
            message(2, "сука", author="user2", date=1750100000),
            message(3, "бял"),
            message(4, "он был рядом"),
            message(5, "блля"),
            message(6, "сука", media_type="document"),
        ],
    )
    tables = (
        "messages",
        "index_segments",
        "index_work",
        "semantic_state",
        "media_state",
        "chats",
        "chat_chunking",
    )

    def snapshot():
        with db.connect() as conn:
            return {
                name: [tuple(r) for r in conn.execute(f"SELECT * FROM {name}")] for name in tables
            }

    before = snapshot()
    status = start_ready(analysis, chat_id)
    assert snapshot() == before
    assert status["summary"]["complete"] and not status["new_auto_omit_enabled"]
    assert status["summary"]["dry_run"]["state"] == "not_available"
    first = analysis.report(chat_id, status["id"], limit=1)
    assert first["forms"][0]["form"] == "сука" and first["next_offset"] == 1
    row = first["forms"][0]
    assert (row["whole_count"], row["eligible_count"], row["days"], row["authors"]) == (3, 2, 2, 2)
    assert row["protections"] == {"attachment": 1}
    rows = analysis.report(chat_id, status["id"])["forms"]
    assert {r["form"] for r in rows} == {"сука", "бял", "блля"}
    typo = next(r for r in rows if r["form"] == "бял")
    assert typo["candidate"]["decision"] != "omit"
    assert analysis.report(chat_id, status["id"], kind="families")["families"]
    with pytest.raises(KeyError):
        analysis.report("other-chat", status["id"])
    # Ordinary lexical access still sees the original approved filler.
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM messages WHERE text='сука'").fetchone()[0] == 3


def test_excluded_messages_and_excluded_parent_do_not_influence_statistics(
    db, importer, tmp_path, analysis
):
    chat_id = chat(
        importer,
        tmp_path,
        [
            message(1, "исключенный?", author="bot"),
            message(2, "бял", reply_to_message_id=1),
            message(3, "ботслово", author="bot"),
        ],
    )
    with db.connect() as conn:
        update_authors(conn, chat_id, {"bot"})
    status = start_ready(analysis, chat_id)
    rows = analysis.report(chat_id, status["id"])["forms"]
    assert len(rows) == 1 and rows[0]["form"] == "бял"
    assert rows[0]["eligible_count"] == 1 and not rows[0]["protections"]
    assert status["summary"]["scanned_messages"] == 1
    with db.connect() as conn:
        update_authors(conn, chat_id, set())
    assert analysis.status(chat_id, status["id"])["stale"]
    with pytest.raises(UserError, match="изменились"):
        analysis.report(chat_id, status["id"])


def test_cpu_collection_holds_no_main_writer_or_lifecycle_lock_and_cancel_is_reusable(
    db, importer, tmp_path, analysis, monkeypatch
):
    import telegram_search.indexing.noise_analysis as module

    chat_id = chat(importer, tmp_path)
    entered, release = threading.Event(), threading.Event()
    original = module.collect_batch

    def held(rows, salt, check):
        entered.set()
        assert release.wait(5)
        check()
        return original(rows, salt, check)

    monkeypatch.setattr(module, "collect_batch", held)
    try:
        job = analysis.create(chat_id)
        assert entered.wait(5)
        assert importer.lifecycle_lock.acquire(timeout=0.1)
        importer.lifecycle_lock.release()
        with db.connection_timeout(0.1), db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE semantic_state SET error=error WHERE id=1")
        assert analysis.cancel(chat_id, job["id"])["state"] == "cancelling"
    finally:
        release.set()
    assert wait_terminal(analysis, chat_id, job["id"])["state"] == "cancelled"
    assert analysis.thread.is_alive()
    monkeypatch.setattr(module, "collect_batch", original)
    assert start_ready(analysis, chat_id)["state"] == "ready"


def test_source_mutation_during_cpu_work_stales_report(
    db, importer, tmp_path, analysis, monkeypatch
):
    import telegram_search.indexing.noise_analysis as module

    chat_id = chat(importer, tmp_path)
    original = module.collect_batch

    def mutate(*args):
        result = original(*args)
        with db.connect() as conn:
            conn.execute("UPDATE chats SET revision=revision+1 WHERE id=?", (chat_id,))
        return result

    monkeypatch.setattr(module, "collect_batch", mutate)
    job = analysis.create(chat_id)
    assert wait_terminal(analysis, chat_id, job["id"])["state"] == "stale"
    with pytest.raises(UserError, match="изменились"):
        analysis.report(chat_id, job["id"])


def test_progress_busy_and_terminal_writer_contention_do_not_kill_worker(
    db, importer, tmp_path, analysis, monkeypatch
):
    chat_id = chat(importer, tmp_path)
    original = analysis._analyze
    completed, release = threading.Event(), threading.Event()

    def held(job):
        result = original(job)
        completed.set()
        assert release.wait(5)
        return result

    def busy(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(analysis, "_analyze", held)
    monkeypatch.setattr(analysis.store, "update", busy)
    job = analysis.create(chat_id)
    try:
        assert completed.wait(10)
        with db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            release.set()
            time.sleep(0.3)
            assert analysis.thread.is_alive()
            # Cancellation remains responsive despite terminal DB retry.
            assert analysis.cancel(chat_id, job["id"])["state"] in {"cancelling", "cancelled"}
    finally:
        release.set()
    assert wait_terminal(analysis, chat_id, job["id"])["state"] == "cancelled"
    assert analysis.thread.is_alive()


def test_explicit_caps_partial_report_and_bounded_pagination(importer, tmp_path, analysis):
    chat_id = chat(importer, tmp_path, [message(i, f"term{i}") for i in range(1, 8)])
    status = start_ready(analysis, chat_id, AnalysisOptions(max_messages=4, max_forms=2))
    assert status["state"] == "limited"
    assert set(status["summary"]["limits"]) == {"message_limit", "form_limit"}
    assert len(analysis.report(chat_id, status["id"])["forms"]) == 2
    for kwargs in ({"limit": 101}, {"offset": -1}, {"offset": True}, {"decision": "invalid"}):
        with pytest.raises(UserError):
            analysis.report(chat_id, status["id"], **kwargs)


def test_restart_marks_queued_work_interrupted_without_automatic_scan(db, importer, tmp_path):
    chat_id = chat(importer, tmp_path)
    store = NoiseStore(db)
    job_id, _ = store.create(chat_id, AnalysisOptions())
    service = NoiseAnalysisService(db)
    try:
        assert service.thread is None
        assert service.status(chat_id, job_id)["state"] == "interrupted"
        assert not report_path(db.workspace, job_id).exists()
    finally:
        service.close()


def test_report_identity_payload_and_links_are_validated(db, importer, tmp_path, analysis):
    chat_id = chat(importer, tmp_path)
    status = start_ready(analysis, chat_id)
    job_id = status["id"]
    path = report_path(db.workspace, job_id)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(
            "UPDATE forms SET payload=?",
            (
                json.dumps(
                    {
                        "schema_version": 1,
                        "decision": "omit",
                        "noise": [],
                        "useful": [],
                        "margin": float("nan"),
                    }
                ),
            ),
        )
    with pytest.raises(UserError, match="повреждён"):
        analysis.report(chat_id, job_id)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("UPDATE forms SET payload=?", ("x" * 20000,))
    with pytest.raises(UserError, match="повреждён"):
        analysis.report(chat_id, job_id)
    path.unlink()
    outside = tmp_path / "outside"
    outside.write_bytes(b"synthetic private data")
    try:
        path.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(UserError, match="ссылки"):
        analysis.report(chat_id, job_id)
    assert outside.read_bytes() == b"synthetic private data"


def test_metadata_limits_and_orphan_gc_protect_active_reports(db, importer, tmp_path):
    chat_id = chat(importer, tmp_path)
    store = NoiseStore(db)
    job_id, fence = store.create(chat_id, AnalysisOptions())
    report = NoiseReport(db, job_id, fence, create=True)
    with pytest.raises(UserError, match="уже выполняется"):
        store.create(chat_id, AnalysisOptions())
    with db.connect() as conn:
        conn.execute("DELETE FROM noise_analyses WHERE id=?", (job_id,))
    store.prune_orphans(active_ids={job_id})
    assert report.path.exists()
    store.prune_orphans()
    assert not report.path.exists()


@pytest.mark.parametrize("phase", ["lexicon", "candidates"])
def test_cancel_is_checked_during_lexicon_loading_and_candidate_matching(
    importer, tmp_path, analysis, monkeypatch, phase
):
    import telegram_search.indexing.noise_analysis as module

    chat_id = chat(importer, tmp_path)
    entered, release = threading.Event(), threading.Event()
    target = "load_lexicon" if phase == "lexicon" else "analyze"
    owner = module if phase == "lexicon" else module.CandidateGenerator
    original = getattr(owner, target)

    def held(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(owner, target, held)
    job = analysis.create(chat_id)
    try:
        assert entered.wait(5)
        assert analysis.cancel(chat_id, job["id"])["state"] == "cancelling"
    finally:
        release.set()
    assert wait_terminal(analysis, chat_id, job["id"])["state"] == "cancelled"


def test_deadline_is_explicit_and_does_not_enable_automatic_omission(
    importer, tmp_path, analysis, monkeypatch
):
    original = analysis._checkpoint

    def expired(job):
        if job.started is not None:
            job.deadline = time.monotonic() - 1
        original(job)

    monkeypatch.setattr(analysis, "_checkpoint", expired)
    chat_id = chat(importer, tmp_path)
    job = analysis.create(chat_id)
    status = wait_terminal(analysis, chat_id, job["id"])
    assert status["state"] == "limited" and status["error"] == "time_limit"
    assert status["summary"] is None and not status["new_auto_omit_enabled"]
    with pytest.raises(UserError, match="повреждён"):
        analysis.report(chat_id, job["id"])


def test_report_size_and_sidecar_links_are_rejected(db, importer, tmp_path, analysis):
    chat_id = chat(importer, tmp_path)
    status = start_ready(analysis, chat_id)
    path = report_path(db.workspace, status["id"])
    with path.open("r+b") as stream:
        stream.truncate(128 * 1024**2 + 1)
    with pytest.raises(UserError, match="повреждён"):
        analysis.report(chat_id, status["id"])
    path.unlink()
    path.write_bytes(b"")
    outside = tmp_path / "outside-journal"
    outside.write_bytes(b"private")
    try:
        path.with_name(path.name + "-journal").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(UserError, match="ссылки"):
        report_path(db.workspace, status["id"])
    assert outside.read_bytes() == b"private"


def test_sql_metadata_cap_is_explicit_and_large_parent_conservatively_protects_reply(
    importer, tmp_path, analysis
):
    chat_id = chat(
        importer,
        tmp_path,
        [
            message(1, "бял", synthetic_metadata="x" * 70000),
            message(2, "длинный контекст " * 2000),
            message(3, "сука", reply_to_message_id=2),
        ],
    )
    status = start_ready(analysis, chat_id)
    assert status["state"] == "limited"
    assert status["summary"]["limited_messages"] == 2
    assert status["summary"]["limits"] == ["text_limit"]
    rows = analysis.report(chat_id, status["id"])["forms"]
    assert len(rows) == 1 and rows[0]["form"] == "сука"
    assert rows[0]["candidate"]["decision"] == "keep"
    assert rows[0]["protections"] == {"question_reply": 1}


@pytest.mark.parametrize("permanent", [False, True])
def test_nonbusy_terminal_write_failure_does_not_spin_or_strand_later_jobs(
    db, importer, tmp_path, analysis, monkeypatch, permanent
):
    first = chat(importer, tmp_path, chat_id=100)
    second = chat(importer, tmp_path, chat_id=200)
    original = analysis._analyze
    entered, release = threading.Event(), threading.Event()

    def held(job):
        result = original(job)
        if job.chat_id == first:
            entered.set()
            assert release.wait(5)
        return result

    monkeypatch.setattr(analysis, "_analyze", held)
    job = analysis.create(first)
    try:
        assert entered.wait(5)
        queued = analysis.create(second)
        states = "'ready','failed'" if permanent else "'ready'"
        with db.connect() as conn:
            conn.execute(
                "CREATE TRIGGER synthetic_terminal_failure BEFORE UPDATE OF state "
                f"ON noise_analyses WHEN NEW.state IN ({states}) "
                "BEGIN SELECT RAISE(ABORT,'private trigger detail'); END"
            )
    finally:
        release.set()
    if permanent:
        deadline = time.monotonic() + 10
        while not analysis.persistence_error and time.monotonic() < deadline:
            time.sleep(0.01)
        assert analysis.persistence_error
        analysis.thread.join(1)
        assert not analysis.thread.is_alive()
        for chat_id, job_id in ((first, job["id"]), (second, queued["id"])):
            status = analysis.status(chat_id, job_id)
            assert status["state"] == "failed" and status["error"] == "result_save_failed"
            assert status["summary"] is None
            assert analysis.cancel(chat_id, job_id)["state"] == "failed"
        with pytest.raises(UserError, match="Перезапустите"):
            analysis.create(first)
        with pytest.raises(UserError, match="Перезапустите"):
            analysis.delete(first, job["id"])
        with db.connect() as conn:
            conn.execute("DROP TRIGGER synthetic_terminal_failure")
        analysis.close()
        restored = NoiseAnalysisService(db)
        try:
            assert restored.thread is None
            assert restored.status(first, job["id"])["state"] == "interrupted"
        finally:
            restored.close()
    else:
        for chat_id, job_id in ((first, job["id"]), (second, queued["id"])):
            status = wait_terminal(analysis, chat_id, job_id)
            assert status["state"] == "failed" and status["error"] == "result_save_failed"
            assert status["summary"] is None
        assert analysis.thread.is_alive() and not analysis.persistence_error


@pytest.mark.parametrize(
    ("sql", "kind"),
    [
        ("UPDATE forms SET whole_count=-1", "forms"),
        ("UPDATE forms SET eligible_count=whole_count+1", "forms"),
        ("UPDATE forms SET decision='omit'", "forms"),
        ("DELETE FROM authors", "forms"),
        ("INSERT INTO protections VALUES('бял','attachment',1000)", "forms"),
        ("UPDATE families SET eligible_count=observed_count+1", "families"),
    ],
)
def test_corrupt_report_counts_and_decision_disagreement_are_rejected(
    db, importer, tmp_path, analysis, sql, kind
):
    chat_id = chat(importer, tmp_path)
    job_id = start_ready(analysis, chat_id)["id"]
    with sqlite3.connect(report_path(db.workspace, job_id)) as conn:
        conn.execute(sql)
    with pytest.raises(UserError, match="повреждён"):
        analysis.report(chat_id, job_id, kind=kind)


def test_stale_running_job_remains_cancellable_until_worker_publication(
    db, importer, tmp_path, analysis, monkeypatch
):
    chat_id = chat(importer, tmp_path)
    entered, release = threading.Event(), threading.Event()

    def held(job):
        entered.set()
        assert release.wait(5)
        analysis._checkpoint(job)

    monkeypatch.setattr(analysis, "_analyze", held)
    job_id = analysis.create(chat_id)["id"]
    try:
        assert entered.wait(5)
        with db.connect() as conn:
            conn.execute("UPDATE chats SET revision=revision+1 WHERE id=?", (chat_id,))
        stale = analysis.status(chat_id, job_id)
        assert stale["state"] == "stale" and stale["active"]
        assert analysis.cancel(chat_id, job_id)["active"]
    finally:
        release.set()
    assert not wait_terminal(analysis, chat_id, job_id)["active"]


def test_family_projection_rejects_missing_columns_and_does_not_export_extra_columns(
    db, importer, tmp_path, analysis
):
    chat_id = chat(importer, tmp_path)
    job_id = start_ready(analysis, chat_id)["id"]
    path = report_path(db.workspace, job_id)
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE families ADD COLUMN unexpected TEXT DEFAULT 'private extra'")
    rows = analysis.report(chat_id, job_id, kind="families")["families"]
    assert rows and set(rows[0]) == {
        "seed",
        "observed_count",
        "eligible_count",
        "omit_count",
        "review_count",
        "keep_count",
    }
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE families DROP COLUMN omit_count")
    with pytest.raises(UserError, match="повреждён"):
        analysis.report(chat_id, job_id, kind="families")
