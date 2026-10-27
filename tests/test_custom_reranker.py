"""Actual external Python reranking, cache/history identity and failure recovery."""

import hashlib
import json
import sys
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest
from conftest import export, load, message
from fastapi.testclient import TestClient
from test_custom_adapters import package

pytest.importorskip("numpy")

from telegram_search.backend.api import create_app  # noqa: E402
from telegram_search.config.model_registry import rerank_registry, rerank_spec  # noqa: E402
from telegram_search.indexing.rerank import RerankService  # noqa: E402
from telegram_search.model_adapters.jobs import AdapterJobs  # noqa: E402
from telegram_search.model_adapters.store import AdapterStore  # noqa: E402
from telegram_search.model_adapters.validation import validate_adapter  # noqa: E402
from telegram_search.search.cache import SearchCache  # noqa: E402
from telegram_search.search.execution import (  # noqa: E402
    SearchCancelled,
    SearchExecution,
    execution_context,
)
from telegram_search.search.history import SearchHistory  # noqa: E402
from telegram_search.search.lexical import Filters  # noqa: E402
from telegram_search.search.rerank import SearchReranker  # noqa: E402
from telegram_search.shared.errors import UserError  # noqa: E402
from telegram_search.sources.service import WorkspaceService  # noqa: E402


def register(db, tmp_path, *, batch=1):
    path, data = package(tmp_path, "reranker")
    code_path = path.parent / "adapter.py"
    code = (
        code_path.read_text()
        .replace(
            "    def rerank(self, query, candidates, context):\n",
            "    def rerank(self, query, candidates, context):\n"
            "        context.progress(0, len(candidates))\n"
            "        if query == 'HANG': time.sleep(60)\n"
            "        if query == 'ERROR': raise RuntimeError('PRIVATE_INPUT_AND_PATH')\n",
        )
        .replace("'score':1.0", "'score':3.0 if 'relevant' in i['text'] else 1.0")
    )
    code_path.write_text(code)
    raw = code_path.read_bytes()
    data["files"][0].update(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
    data["input_contract"].update(
        max_batch=batch, query_max_characters=32, document_max_characters=64
    )
    path.write_text(json.dumps(data))
    report = validate_adapter(AdapterStore(db.workspace), path, sys.executable, register=True)
    return "custom:" + report["identity"], path, data


def prepare(service):
    service.prepare(offline=True)
    service.preparation.join(timeout=20)
    assert not service.preparation.is_alive()
    assert service.status()["ready"], service.status()


def workspace_service(db, rerank):
    return WorkspaceService(
        db, SimpleNamespace(lifecycle_lock=threading.RLock()), None, None, rerank
    )


def test_registered_reranker_real_scores_cache_scope_history_and_restart(db, importer, tmp_path):
    profile, _, _ = register(db, tmp_path)
    assert profile in rerank_registry(db.workspace) and profile not in rerank_registry()
    service = RerankService(db)
    try:
        ws = workspace_service(db, service)
        result = load(
            importer,
            export(tmp_path / "source", [message(1, "other"), message(2, "relevant")]),
        )
        before = SearchCache.revision(db)
        with db.connect() as conn:
            segments = [tuple(r) for r in conn.execute("SELECT * FROM index_segments")]
            semantic = tuple(conn.execute("SELECT * FROM semantic_state").fetchone())
        ws.update_settings({"rerank_profile": profile})
        assert SearchCache.revision(db) != before
        prepare(service)
        assert service.status()["source"] == "custom"
        assert service.status()["path"] == str(
            AdapterStore(db.workspace).root / profile.removeprefix("custom:")
        )
        ws.update_settings({"giga_rerank_enabled": True})
        search = SearchReranker(db, service)
        hits = [{"chat_id": result["chat_id"], "message_id": i} for i in (1, 2)]
        ranked, metadata = search.apply("query", hits, "text", Filters())
        assert [h["message_id"] for h in ranked] == [2, 1]
        assert [h["rerank_score"] for h in ranked] == [3, 1]
        assert metadata["applied"] and metadata["profile"] == profile
        encoder = service.encoder
        assert encoder.max_batch == 1 and encoder.backend_info()["provider"] == "custom:cpu"
        original = encoder.score_pairs
        encoder.score_pairs = lambda *_: pytest.fail("cached scoring executed again")
        assert search.apply("query", hits, "text", Filters())[0] == ranked
        with pytest.raises(UserError):
            search.apply("query", hits, "text", Filters(author_ids=["absent"]))
        with db.connect() as conn:
            captured = SearchHistory._models(conn, db, {"rerank_branches": [metadata]})
            assert captured["rerank"]["manifest"]["adapter_identity"] == profile[7:]
            assert segments == [tuple(r) for r in conn.execute("SELECT * FROM index_segments")]
            assert semantic == tuple(conn.execute("SELECT * FROM semantic_state").fetchone())
        encoder.score_pairs = original
        rows = encoder.score_pairs("q" * 40, [{"candidate_id": "x", "text": "t" * 80}])
        assert rows[0]["truncated"] and rows[0]["query_truncated"]
        fallback, reason = search.apply("ERROR", hits, "text", Filters())
        assert fallback == hits and not reason["applied"]
        assert "PRIVATE" not in reason["reason"]
        assert search.apply("another", hits, "text", Filters())[1]["applied"]
    finally:
        service.shutdown()
    restarted = RerankService(db)
    try:
        assert restarted.status()["ready"]
        assert restarted.encoder is None
        assert (
            restarted.get_encoder().score_pairs(
                "query", [{"candidate_id": "x", "text": "relevant"}]
            )[0]["score"]
            == 3
        )
    finally:
        restarted.shutdown()


def test_cancel_native_rerank_terminates_worker_and_no_partial_cache(db, importer, tmp_path):
    profile, _, _ = register(db, tmp_path)
    db.settings = replace(db.settings, rerank_profile=profile, giga_rerank_enabled=True)
    service = RerankService(db)
    outcome = []
    try:
        prepare(service)
        result = load(importer, export(tmp_path / "source", [message()]))
        search = SearchReranker(db, service)
        execution = SearchExecution()

        def run():
            with execution_context(execution):
                try:
                    search.apply(
                        "HANG", [{"chat_id": result["chat_id"], "message_id": 1}], "text", Filters()
                    )
                except SearchCancelled:
                    outcome.append("cancelled")

        thread = threading.Thread(target=run)
        thread.start()
        deadline = time.monotonic() + 10
        while service.encoder is None or service.encoder.session is None:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        process = service.encoder.session.process
        execution.cancelled.set()
        thread.join(timeout=5)
        assert not thread.is_alive() and outcome == ["cancelled"]
        assert process.poll() is not None and not search.cache
        assert (
            service.get_encoder().score_pairs("query", [{"candidate_id": "x", "text": "relevant"}])[
                0
            ]["score"]
            == 3
        )
    finally:
        service.shutdown()


def test_damaged_optional_package_leaves_base_search_and_settings_available(db, tmp_path):
    profile, _, _ = register(db, tmp_path)
    service = RerankService(db)
    try:
        ws = workspace_service(db, service)
        ws.update_settings({"rerank_profile": profile})
        manifest = AdapterStore(db.workspace).root / profile[7:] / "manifest.json"
        manifest.write_text("broken manifest")
        assert rerank_spec(profile, db.workspace).manifest["unavailable"]
        assert SearchCache.revision(db)
        assert not service.status()["ready"]
        assert any(p["profile"] == "giga" for p in service.status()["profiles"])
        with pytest.raises(UserError):
            ws.update_settings({"rerank_profile": profile})
        ws.update_settings({"rerank_profile": "giga"})
        assert db.settings.rerank_profile == "giga"
    finally:
        service.shutdown()


def test_reranker_status_uses_one_settings_snapshot(db):
    old = replace(
        db.settings, rerank_profile="jina-v2", giga_rerank_enabled=True, search_device="gpu"
    )
    new = replace(
        db.settings, rerank_profile="giga", giga_rerank_enabled=False, search_device="cpu"
    )

    class ChangingSettings:
        workspace = db.workspace
        connect = db.connect
        reads = 0

        @property
        def settings(self):
            self.reads += 1
            return old if self.reads == 1 else new

    service = RerankService(db)
    changing = ChangingSettings()
    try:
        service.db = changing
        status = service.status()
        assert status["profile"] == "jina-v2" and status["enabled"]
        assert status["device"] == "gpu" and changing.reads == 1
    finally:
        service.shutdown()


def test_role_mismatch_unregistered_profile_and_catalog_activation(db, tmp_path):
    profile, _, _ = register(db, tmp_path)
    jobs = AdapterJobs(db.workspace)
    try:
        assert jobs.catalog()["models"][0]["activation_supported"]
    finally:
        jobs.shutdown()
    (tmp_path / "text").mkdir()
    path, _ = package(tmp_path / "text", "text_embedding")
    report = validate_adapter(AdapterStore(db.workspace), path, sys.executable, register=True)
    ws = workspace_service(db, None)
    for invalid in ("custom:" + report["identity"], "custom:" + "a" * 64, "remote/model"):
        with pytest.raises(UserError):
            ws.update_settings({"rerank_profile": invalid})
    assert db.settings.rerank_profile == "giga"


def test_api_custom_reranking_search_job_and_saved_snapshot(tmp_path):
    with TestClient(create_app(tmp_path / "workspace"), base_url="http://127.0.0.1") as client:
        db = client.app.state.importer.db
        profile, _, _ = register(db, tmp_path)
        load(
            client.app.state.importer,
            export(
                tmp_path / "source",
                [
                    message(1, "query other", author="alice", date=1750000000),
                    message(2, "query relevant", author="alice", date=1750086400),
                    message(3, "query relevant", author="bot", date=1750172800),
                ],
            ),
        )
        headers = {"X-Session-Token": client.get("/api/session").json()["token"]}
        assert client.patch("/api/settings", json={"rerank_profile": profile}).status_code == 403
        assert (
            client.patch(
                "/api/settings", json={"rerank_profile": profile}, headers=headers
            ).status_code
            == 200
        )
        assert client.post("/api/rerank/prepare", json={}, headers=headers).status_code == 202
        client.app.state.rerank.preparation.join(timeout=20)
        assert client.get("/api/rerank").json()["ready"]
        assert (
            client.patch(
                "/api/settings", json={"giga_rerank_enabled": True}, headers=headers
            ).status_code
            == 200
        )
        job = client.post(
            "/api/search-jobs",
            json={
                "query": "query",
                "mode": "words",
                "modalities": ["text"],
                "author_ids": ["alice"],
            },
            headers=headers,
        ).json()
        client.app.state.search_jobs.jobs[job["job_id"]].future.result(timeout=15)
        result = client.get(f"/api/search-jobs/{job['job_id']}/result")
        assert result.status_code == 200, result.text
        data = result.json()
        assert data["rerank_applied"]
        assert data["results"][0]["message_id"] == 2
        assert {r["message_id"] for r in data["results"]} == {1, 2}
        assert data["rerank_branches"][0]["profile"] == profile
        assert client.get(f"/api/search-jobs/{job['job_id']}").json()["state"] == "completed"
        with db.connect() as conn:
            snapshot = json.loads(
                conn.execute("SELECT models_json FROM search_snapshots").fetchone()[0]
            )
            assert snapshot["rerank"]["manifest"]["adapter_identity"] == profile[7:]
