"""Run each phase in its own runtime/source tree, using synthetic data only.

legacy: PYTHONPATH=<v0.2.0 source>/src .venv/bin/python scripts/validate_resume.py legacy
resume: workspace/gpu-env/bin/python scripts/validate_resume.py resume
No user database is opened: only pinned public model bundles are reused.
"""

import argparse
import hashlib
import json
import os
import shutil
from dataclasses import replace
from pathlib import Path

import numpy as np
from PIL import Image

from telegram_search import __version__
from telegram_search.config.model_registry import media_registry, registry
from telegram_search.indexing.media import MediaService
from telegram_search.indexing.service import SemanticService
from telegram_search.indexing.worker import SegmentWorker
from telegram_search.inference.bundles import BundleStore
from telegram_search.inference.clip import ClipEncoder
from telegram_search.inference.e5 import E5Encoder
from telegram_search.ingestion.importer import ImportService
from telegram_search.search.hybrid import HybridSearch
from telegram_search.search.lexical import Filters
from telegram_search.search.media import MediaSearch
from telegram_search.storage.database import Database


def vector_rows(service):
    table = service._vector_store().table(service.encoder.space_id, service.encoder.spec.dimension)
    return {
        row["id"]: hashlib.sha256(np.asarray(row["vector"], dtype=np.float32).tobytes()).hexdigest()
        for row in table.to_arrow().to_pylist()
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=["legacy", "resume"])
    parser.add_argument("--case", type=Path, default=Path("workspace/resume-validation"))
    parser.add_argument("--models", type=Path, default=Path("workspace"))
    args = parser.parse_args()
    root = args.case.resolve()
    if args.phase == "legacy":
        if __version__ != "0.2.0" or root.exists():
            raise RuntimeError("Legacy phase requires 0.2.0 and a new synthetic case folder")
        root.mkdir(parents=True)
        model_store = BundleStore(args.models)
        for spec in [registry()["small"], *media_registry().values()]:
            source = model_store.verify(spec)
            target = root / "workspace/models/bundles" / spec.identity
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source, target, copy_function=os.link)
        source = root / "export"
        source.mkdir()
        for color in ("red", "blue"):
            Image.new("RGB", (320, 240), color).save(source / (color + ".png"))
        messages = [
            {
                "id": i + 1,
                "type": "message",
                "date_unixtime": str(1750000000 + i * 10),
                "from": "Public synthetic author",
                "from_id": "user1",
                "text": ("Schedule travel meeting budget release. " * 16) + str(i),
                **({"photo": "red.png"} if i == 0 else {"photo": "blue.png"} if i == 10 else {}),
            }
            for i in range(120)
        ]
        (source / "result.json").write_text(
            json.dumps(
                {
                    "id": 100,
                    "name": "Synthetic upgrade case",
                    "type": "personal_chat",
                    "messages": messages,
                }
            )
        )
    elif __version__ != "0.3.0" or not (root / "legacy.json").is_file():
        raise RuntimeError("Resume phase requires 0.3.0 and a legacy synthetic case")
    db = Database(root / "workspace")
    db.initialize()
    if args.phase == "resume":
        db.settings = replace(db.settings, device="gpu", search_device="cpu")
        db.settings.save(db.workspace)
    importer = ImportService(db)
    service = media = None
    try:
        service = SemanticService(db, importer.lifecycle_lock, start_background=False)
        media = MediaService(db, importer.lifecycle_lock, service, start_background=False)
        if args.phase == "legacy":
            job = importer.run(importer.prepare(str(root / "export/result.json")))
            assert job["state"] == "completed"
            spec = registry()["small"]
            encoder = E5Encoder(spec, BundleStore(db.workspace).verify(spec))
            service.activate(encoder)
            media.clip = ClipEncoder(db.workspace)
            with db.connect() as conn:
                conn.execute("UPDATE media_state SET images_enabled=1 WHERE id=1")
                work = conn.execute("SELECT id FROM index_work WHERE state='pending'").fetchone()[0]
            original = encoder.encode_text
            calls = 0

            def encode(texts, purpose, **kwargs):
                nonlocal calls
                calls += 1
                result = original(texts, purpose, **kwargs)
                if calls == 3:
                    service.control("pause")
                return result

            encoder.encode_text = encode
            partial = SegmentWorker(
                db, encoder, service._vector_store(), importer.lifecycle_lock
            ).run(work)
            assert 0 < partial["chunks_done"] < partial["chunks_total"]
            assert media._image_batch()
            media.control("pause")
            with db.connect() as conn:
                ids = [row[0] for row in conn.execute("SELECT id FROM chunks ORDER BY id")]
                generation = conn.execute(
                    "SELECT target_generation FROM index_segments"
                ).fetchone()[0]
            report = {
                "synthetic_only": True,
                "legacy_version": __version__,
                "work": work,
                "chunks_done": partial["chunks_done"],
                "chunks_total": partial["chunks_total"],
                "space": encoder.space_id,
                "ids": ids,
                "generation": generation,
                "vectors": vector_rows(service),
                "clip_space": media.clip.space_id,
                "images_ready": media.status()["images_ready"],
            }
            (root / "legacy.json").write_text(json.dumps(report))
            print(
                json.dumps(
                    {
                        "legacy_version": __version__,
                        "chunks_done": partial["chunks_done"],
                        "chunks_total": partial["chunks_total"],
                        "synthetic_only": True,
                    }
                )
            )
        else:
            from telegram_search.inference import providers

            before = json.loads((root / "legacy.json").read_text())
            assert before["synthetic_only"] is True
            assert service.encoder.space_id == before["space"]
            assert media.clip.space_id == before["clip_space"]
            assert service.status()["paused"] and media.status()["paused"]
            assert vector_rows(service) == before["vectors"]
            with db.connect() as conn:
                assert [
                    row[0] for row in conn.execute("SELECT id FROM chunks ORDER BY id")
                ] == before["ids"]
                assert (
                    conn.execute("SELECT target_generation FROM index_segments").fetchone()[0]
                    == before["generation"]
                )
                assert (
                    conn.execute(
                        "SELECT chunks_done FROM index_work WHERE id=?", (before["work"],)
                    ).fetchone()[0]
                    == before["chunks_done"]
                )
            original_preload = providers.preload_cuda
            preloads = []

            def preload():
                preloads.append(True)
                original_preload()

            providers.preload_cuda = preload
            service.encode_query("Public schedule and travel query")
            assert MediaSearch(db, media, service, importer.lifecycle_lock).search(
                "A red square", Filters(), kind="images"
            )[0]
            assert not preloads and service.encoder.session is None
            assert service.encoder.query_session.get_providers() == [providers.CPU]
            assert "image" not in media.clip.sessions
            encoded = []
            original = service.encoder.encode_text

            def encode(texts, purpose, **kwargs):
                if purpose == "passage":
                    encoded.extend(texts)
                return original(texts, purpose, **kwargs)

            service.encoder.encode_text = encode
            service.control("resume")
            result = service._worker(service.encoder).run(before["work"])
            assert result["state"] == "done"
            assert len(encoded) == before["chunks_total"] - before["chunks_done"]
            after = vector_rows(service)
            assert all(after[key] == value for key, value in before["vectors"].items())
            media.control("resume")
            assert media._image_batch()
            assert media.status()["images_ready"] == before["images_ready"] + 1
            service.encoder.unload_index()
            media.clip.unload_index()
            count = len(preloads)
            assert HybridSearch(db, service, importer.lifecycle_lock).search(
                "travel", mode="meaning"
            )["results"]
            assert MediaSearch(db, media, service, importer.lifecycle_lock).search(
                "A blue square", Filters(), kind="images"
            )[0]
            assert len(preloads) == count
            report = {
                "synthetic_only": True,
                "legacy_version": "0.2.0",
                "new_version": __version__,
                "space_id_preserved": True,
                "chunk_ids_preserved": True,
                "cpu_vectors_unchanged": True,
                "cpu_chunks_preserved": before["chunks_done"],
                "gpu_chunks_encoded": len(encoded),
                "total_chunks": result["chunks_total"],
                "legacy_clip_space_preserved": True,
                "new_gpu_images": 1,
                "cpu_queries_without_gpu_session_or_preload": True,
            }
            (root / "result.json").write_text(json.dumps(report, indent=2))
            print(json.dumps(report))
    finally:
        if media:
            media.shutdown()
        if service:
            service.shutdown()
        importer.shutdown()


if __name__ == "__main__":
    main()
