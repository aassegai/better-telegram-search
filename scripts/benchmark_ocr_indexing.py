"""CPU-only OCR end-to-end queue/IPC/SQL/FTS and worker lifecycle benchmark.

Uses generated RU/EN images and a fresh temporary workspace, never user exports.
--models must point to the pinned public ONNX OCR bundle. Emits no paths or text.
"""

import argparse
import json
import os
import platform
import shutil
import statistics
import tempfile
import threading
import time
from dataclasses import replace
from importlib import metadata
from importlib.resources import files
from pathlib import Path

from benchmark_ocr import dataset, quality

from telegram_search.config.model_registry import ocr_spec
from telegram_search.indexing.media import MediaService
from telegram_search.indexing.service import SemanticService
from telegram_search.inference.bundles import BundleStore
from telegram_search.inference.ocr import OcrEngine
from telegram_search.inference.resources import compute_gate
from telegram_search.ingestion.importer import ImportService
from telegram_search.search.lexical import Filters
from telegram_search.search.media import MediaSearch
from telegram_search.storage.database import Database


def copy_bundle(models, workspace):
    spec = ocr_spec()
    BundleStore(workspace).verify(spec, models)
    destination = BundleStore(workspace).path(spec)
    for name in ["manifest.json", *(item["name"] for item in spec.manifest["files"])]:
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(models / name, target)
        except OSError:
            shutil.copyfile(models / name, target)


def samples(values):
    ordered = sorted(values)
    return (
        {
            "p50_seconds": round(statistics.median(ordered), 6),
            "p95_seconds": round(ordered[max(0, int(len(ordered) * 0.95) - 1)], 6),
        }
        if ordered
        else {}
    )


def copy_tesseract(models, workspace):
    manifest = json.loads(files("telegram_search.config").joinpath("ocr_model.json").read_text())
    destination = workspace / "models" / "ocr"
    destination.mkdir(parents=True, exist_ok=True)
    for item in manifest["files"]:
        shutil.copyfile(models / item["name"], destination / item["name"])
    OcrEngine(workspace).verify()


def indexing(models, workers, total_threads, engine="paddle"):
    data, truth = dataset()
    with tempfile.TemporaryDirectory(prefix="bts-ocr-synthetic-") as temporary:
        root = Path(temporary)
        workspace = root / "workspace"
        db = Database(workspace)
        db.initialize()
        db.settings = replace(
            db.settings, ocr_engine=engine, cpu_threads=total_threads, ocr_cpu_workers=workers
        )
        (copy_bundle if engine == "paddle" else copy_tesseract)(models, workspace)
        source = root / "source"
        source.mkdir()
        messages = []
        targets = []
        for repeat in range(3):
            for index, image in enumerate(data):
                filename = f"{repeat * len(data) + index:03d}.png"
                # PNG permits trailing bytes; distinct blobs with identical pixels/text.
                (source / filename).write_bytes(image + f"synthetic-{repeat}".encode())
                messages.append(
                    {
                        "id": len(messages) + 1,
                        "type": "message",
                        "date_unixtime": "1750000000",
                        "text": "",
                        "photo": filename,
                    }
                )
                targets.append(truth[index])
        (source / "bad.png").write_bytes(data[0] + b"synthetic-changed-file")
        for filename in ["000.png", "000.png", "001.png", "bad.png"]:
            messages.append(
                {
                    "id": len(messages) + 1,
                    "type": "message",
                    "date_unixtime": "1750000000",
                    "text": "",
                    "photo": filename,
                }
            )
        export = source / "result.json"
        export.write_text(
            json.dumps(
                {"id": 1, "type": "personal_chat", "name": "Synthetic", "messages": messages}
            ),
            encoding="utf-8",
        )
        importer = ImportService(db)
        semantic = SemanticService(db, importer.lifecycle_lock, start_background=False)
        media = MediaService(db, importer.lifecycle_lock, semantic, start_background=False)
        query_times, wait_times, memory_samples = [], [], []
        finished = threading.Event()

        def search():
            engine = MediaSearch(db, media, semantic, importer.lifecycle_lock)
            while not finished.wait(0.01):
                memory_samples.append(media.resources.snapshot()["rss_bytes"])
                started = time.perf_counter()
                engine.search("12345", Filters(), kind="ocr", exact=True)
                query_times.append(time.perf_counter() - started)
                started = time.perf_counter()
                with compute_gate(None).slot(interactive=True):
                    wait_times.append(time.perf_counter() - started)

        thread = None
        try:
            result = importer.run(importer.prepare(str(export)))
            assert result["state"] == "completed"
            # A file becomes unreadable after import; its failure must not block neighbors.
            (source / "bad.png").write_bytes(b"synthetic corrupt image")
            media.ocr = media._new_ocr(db.settings)
            thread = threading.Thread(target=search)
            thread.start()
            started = time.perf_counter()
            while media._ocr_run():
                pass
            elapsed = time.perf_counter() - started
            finished.set()
            thread.join()
            status = media.status()
            assert (status["ocr_ready"], status["ocr_failed"], status["total_photos"]) == (
                24,
                1,
                25,
            ), (status["ocr_ready"], status["ocr_failed"], status["total_photos"])
            with db.connect() as conn:
                values = [
                    dict(row)
                    for row in conn.execute(
                        "SELECT DISTINCT r.relative_path,o.text FROM media_refs r "
                        "JOIN ocr_cache o ON o.sha256=r.sha256 "
                        "WHERE o.state='ready' ORDER BY r.relative_path"
                    )
                ]
                assert (
                    conn.execute("SELECT COUNT(*) FROM ocr_work WHERE state='running'").fetchone()[
                        0
                    ]
                    == 0
                )
            return {
                "engine": engine,
                "workers": workers,
                "total_threads": total_threads,
                "attachments": len(messages),
                "unique_images": 25,
                "ready": 24,
                "failed": 1,
                "elapsed_seconds": round(elapsed, 4),
                "completed_images_per_minute": round(25 * 60 / elapsed, 2),
                "resources": status["resource_usage"],
                "sampled_peak_rss_bytes": max(memory_samples, default=0),
                "queue": status["queues"]["ocr"],
                "lexical_search": samples(query_times),
                "interactive_device_wait": samples(wait_times),
                **quality(values, targets),
            }
        finally:
            finished.set()
            if thread:
                thread.join()
            media.shutdown()
            semantic.shutdown()
            importer.shutdown()


def lifecycle(models, recycle_after, total_threads):
    data, _ = dataset()
    with tempfile.TemporaryDirectory(prefix="bts-ocr-lifecycle-") as temporary:
        db = Database(Path(temporary))
        db.initialize()
        copy_bundle(models, db.workspace)
        db.settings = replace(db.settings, ocr_engine="paddle", cpu_threads=total_threads)
        semantic = SemanticService(db, threading.RLock(), start_background=False)
        media = MediaService(db, semantic.lock, semantic, start_background=False)
        engine = media._new_ocr(db.settings)
        engine.worker.recycle_after = recycle_after
        rss, cold, durations = [], [], []
        started = time.perf_counter()
        try:
            for index in range(130):
                began = time.perf_counter()
                engine.recognize(data[index % len(data)])
                durations.append(time.perf_counter() - began)
                stats = engine.worker.last_stats
                rss.append(stats["child_rss_bytes"])
                if stats["cold_start"]:
                    cold.append(stats["cold_start_seconds"])
            return {
                "images": 130,
                "recycle_after": recycle_after,
                "process_starts": engine.worker.starts,
                "elapsed_seconds": round(time.perf_counter() - started, 4),
                "cold_start_seconds": [round(value, 4) for value in cold],
                "rss_max_bytes": max(rss),
                "rss_last_bytes": rss[-1],
                **samples(durations),
            }
        finally:
            engine.unload()
            media.shutdown()
            semantic.shutdown()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", type=Path, required=True)
    parser.add_argument("--threads", type=int, default=4, choices=range(4, 33))
    parser.add_argument("--section", choices=("all", "indexing", "lifecycle"), default="all")
    parser.add_argument("--engine", choices=("paddle", "tesseract"), default="paddle")
    args = parser.parse_args()
    if args.engine == "tesseract" and args.section != "indexing":
        parser.error("Tesseract comparison uses --section indexing and a dictionary directory")
    report = {
        "device": "cpu",
        "runtime": {
            "onnxruntime": metadata.version("onnxruntime"),
            "tesserocr": metadata.version("tesserocr"),
            "python": platform.python_version(),
        },
    }
    if args.section in {"all", "indexing"}:
        report["indexing"] = [
            indexing(args.models, count, args.threads, args.engine) for count in (1, 2, 4)
        ]
    if args.section in {"all", "lifecycle"}:
        report["lifecycle"] = [
            lifecycle(args.models, interval, args.threads) for interval in (128, 1024)
        ]
    print(json.dumps(report, indent=2))
