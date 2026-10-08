"""Bounded CUDA load and mixed indexing/search on generated RU/EN data only.

Run in a uv GPU environment without Torch. Public pinned models are read from
--models-workspace and --ocr-models; user databases and exports are never opened.
The mixed test creates and removes its own temporary workspace.
Use an external process-group watchdog for native inference and shutdown, e.g.
timeout --kill-after=10s 12m python -u scripts/benchmark_gpu_load.py ...
"""

import argparse
import gc
import io
import json
import math
import os
import platform
import shutil
import statistics
import subprocess
import tempfile
import threading
import time
from collections import deque
from dataclasses import replace
from importlib import metadata, util
from pathlib import Path

import psutil
from benchmark_ocr import dataset, quality
from PIL import Image

from telegram_search.config.model_registry import media_registry, ocr_spec, registry
from telegram_search.indexing.chats import ChatIndexing
from telegram_search.indexing.media import MediaService
from telegram_search.indexing.prefetch import BatchPrefetch
from telegram_search.indexing.service import SemanticService
from telegram_search.inference.bundles import BundleStore
from telegram_search.inference.clip import ClipEncoder
from telegram_search.inference.e5 import E5Encoder
from telegram_search.inference.ocr_onnx import OnnxOcrEngine
from telegram_search.inference.providers import CUDA, Execution
from telegram_search.ingestion.importer import ImportService
from telegram_search.search.hybrid import HybridSearch
from telegram_search.search.lexical import Filters
from telegram_search.search.media import MediaSearch, UnifiedSearch
from telegram_search.storage.database import Database


def percentile(values, fraction):
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)] if values else None


def distribution(values):
    return {
        "samples": len(values),
        "mean": round(statistics.mean(values), 4) if values else None,
        "p50": percentile(values, 0.5),
        "p95": percentile(values, 0.95),
        "maximum": max(values, default=None),
    }


class Telemetry:
    def __init__(self):
        self.rows = deque(maxlen=12000)
        self.process = subprocess.Popen(
            [
                "nvidia-smi",
                "--id=0",
                "--query-gpu=utilization.gpu,memory.used,power.draw,temperature.gpu,"
                "clocks.current.sm,clocks_event_reasons.sw_thermal_slowdown,"
                "clocks_event_reasons.sw_power_cap",
                "--format=csv,noheader,nounits",
                "--loop-ms=200",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self):
        for line in self.process.stdout:
            try:
                fields = [value.strip() for value in line.split(",")]
                values = [float(value) for value in fields[:5]]
                if len(fields) != 7 or not all(math.isfinite(value) for value in values):
                    continue
                processes = [psutil.Process(), *psutil.Process().children(recursive=True)]
                rss = sum(
                    process.memory_info().rss for process in processes if process.is_running()
                )
                self.rows.append(
                    (time.monotonic(), *values, fields[5] == "Active", fields[6] == "Active", rss)
                )
            except (ValueError, psutil.Error):
                continue

    def summary(self, start, finish):
        rows = [row for row in list(self.rows) if start <= row[0] <= finish]
        if not rows:
            raise RuntimeError("GPU telemetry unavailable; utilization was not validated")
        return {
            "samples": len(rows),
            "gpu_utilization_percent": distribution([row[1] for row in rows]),
            "low_utilization_sample_fraction": round(
                sum(row[1] < 10 for row in rows) / len(rows), 4
            )
            if rows
            else None,
            "device_memory_peak_mib": max((row[2] for row in rows), default=None),
            "power_peak_watts": max((row[3] for row in rows), default=None),
            "temperature_peak_celsius": max((row[4] for row in rows), default=None),
            "sm_clock_mhz": distribution([row[5] for row in rows]),
            "thermal_throttle_sample_fraction": round(sum(row[6] for row in rows) / len(rows), 4),
            "power_cap_sample_fraction": round(sum(row[7] for row in rows) / len(rows), 4),
            "sampled_rss_peak_bytes": max((row[8] for row in rows), default=None),
        }

    def close(self):
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.thread.join(timeout=5)
        self.process.stdout.close()


def prepared_load(encoder, prepare, infer, size, seconds, telemetry, *, prefetch):
    pipeline = BatchPrefetch()
    durations, units = [], 0
    warmup_start = time.monotonic()
    infer(prepare())
    warmup_seconds = time.monotonic() - warmup_start
    started = time.monotonic()
    try:
        while time.monotonic() - started < seconds:
            began = time.monotonic()
            prepared = pipeline.take("batch", prepare) if prefetch else None
            if prefetch:
                pipeline.submit("batch", prepare)
            infer(prepared)
            units += size
            durations.append(time.monotonic() - began)
        finished = time.monotonic()
        return {
            "units": units,
            "elapsed_seconds": round(finished - started, 4),
            "units_per_minute": round(units * 60 / (finished - started), 2),
            "warmup_seconds": round(warmup_seconds, 4),
            "batch_size": size,
            "prefetch": prefetch,
            "batch_seconds": distribution(durations),
            "telemetry": telemetry.summary(started, finished),
            "last_timings": dict(encoder.last_timings),
        }
    finally:
        pipeline.close()


def copy_model(source, destination, spec):
    BundleStore(destination).verify(spec, source)
    target = BundleStore(destination).path(spec)
    for name in ["manifest.json", *(item["name"] for item in spec.manifest["files"])]:
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(source / name, path)
        except OSError:
            shutil.copyfile(source / name, path)


def mixed_indexing(model_workspace, ocr_models, telemetry, *, deadline_seconds=240):
    images, _ = dataset()
    message_count, image_count = 384, 64
    with tempfile.TemporaryDirectory(prefix="bts-gpu-load-") as temporary:
        root = Path(temporary)
        db = Database(root / "workspace")
        db.initialize()
        db.settings = replace(
            db.settings,
            device="gpu",
            search_device="cpu",
            ocr_engine="paddle",
            ocr_device="gpu",
            embedding_batch=32,
            image_batch=16,
            ocr_batch_size=4,
            ocr_region_batch_size=32,
            memory_limit_mib=8192,
        )
        db.settings.save(db.workspace)
        for spec in [registry()["small"], *media_registry().values()]:
            copy_model(BundleStore(model_workspace).verify(spec), db.workspace, spec)
        copy_model(ocr_models, db.workspace, ocr_spec())
        source = root / "source"
        source.mkdir()
        messages = []
        for index in range(message_count):
            messages.append(
                {
                    "id": index + 1,
                    "type": "message",
                    "date_unixtime": str(1750000000 + index),
                    "from": "Synthetic",
                    "from_id": "user1",
                    "text": f"Meeting schedule Архив сообщений 12345 номер {index}. " * 20,
                }
            )
        for index in range(image_count):
            filename = f"{index:04d}.png"
            (source / filename).write_bytes(images[index % len(images)] + str(index).encode())
            messages.append(
                {
                    "id": len(messages) + 1,
                    "type": "message",
                    "photo": filename,
                    "date_unixtime": str(1750002000 + index),
                    "from": "Synthetic",
                    "from_id": "user1",
                    "text": "Meeting schedule 12345",
                }
            )
        path = source / "result.json"
        path.write_text(
            json.dumps(
                {"id": 1, "name": "Synthetic", "type": "personal_chat", "messages": messages}
            ),
            encoding="utf-8",
        )
        importer = ImportService(db)
        semantic = SemanticService(db, importer.lifecycle_lock, start_background=False)
        media = MediaService(db, importer.lifecycle_lock, semantic, start_background=False)
        stop = threading.Event()
        latencies, errors, match_counts, warning_counts = [], [], [], []
        query_thread = None
        began = time.monotonic()
        paused = False
        before, after, pause_requested, pause_stable = {}, {}, {}, {}

        def search():
            searcher = UnifiedSearch(
                HybridSearch(db, semantic, importer.lifecycle_lock),
                MediaSearch(db, media, semantic, importer.lifecycle_lock),
            )
            index = 0
            while not stop.wait(0.25):
                started = time.monotonic()
                try:
                    result = searcher.search(
                        f"Meeting schedule {12345 + index % 4}",
                        Filters(),
                        False,
                        5,
                        "hybrid",
                        modalities=["text", "images", "ocr"],
                    )
                    match_counts.append(len(result["results"]))
                    warning_counts.append(len(result.get("warnings", [])))
                except Exception as exc:
                    errors.append(type(exc).__name__)
                latencies.append(time.monotonic() - started)
                index += 1

        def progress():
            with db.connect() as conn:
                return {
                    "text_chunks_done": conn.execute(
                        "SELECT COALESCE(SUM(chunks_done),0) FROM index_work"
                    ).fetchone()[0],
                    "text_remaining": conn.execute(
                        "SELECT COUNT(*) FROM index_work WHERE state IN ('pending','running')"
                    ).fetchone()[0],
                    "text_failed": conn.execute(
                        "SELECT COUNT(*) FROM index_work WHERE state='failed'"
                    ).fetchone()[0],
                    "text_running": conn.execute(
                        "SELECT COUNT(*) FROM index_work WHERE state='running'"
                    ).fetchone()[0],
                    "ocr_ready": conn.execute(
                        "SELECT COUNT(*) FROM ocr_cache WHERE state='ready'"
                    ).fetchone()[0],
                    "ocr_failed": conn.execute(
                        "SELECT COUNT(*) FROM ocr_cache WHERE state='failed'"
                    ).fetchone()[0],
                    "images_ready": conn.execute(
                        "SELECT COUNT(*) FROM media_embeddings WHERE kind='image'"
                    ).fetchone()[0],
                    "ocr_dense_ready": conn.execute(
                        "SELECT COUNT(*) FROM media_embeddings WHERE kind='ocr'"
                    ).fetchone()[0],
                    "ocr_nonempty": conn.execute(
                        "SELECT COUNT(*) FROM ocr_cache WHERE state='ready' AND text<>''"
                    ).fetchone()[0],
                    "media_failed": conn.execute("SELECT COUNT(*) FROM media_failures").fetchone()[
                        0
                    ],
                }

        try:
            result = importer.run(importer.prepare(str(path)))
            assert result["state"] == "completed"
            chat = result["chat_id"]
            semantic.activate(semantic._new_encoder("small"))
            media.clip = ClipEncoder(db.workspace, device="gpu", search_device="cpu", threads=4)
            media.ocr = OnnxOcrEngine(db.workspace, db.settings)
            with db.connect() as conn:
                conn.execute(
                    "UPDATE media_state SET images_enabled=1,ocr_enabled=1,"
                    "preparation_state='ready'"
                )
            semantic.start_background()
            media.start_background()
            query_thread = threading.Thread(target=search, daemon=True)
            query_thread.start()
            control = ChatIndexing(db, semantic, media, importer.lifecycle_lock)
            last_print = began
            while time.monotonic() - began < deadline_seconds:
                value = progress()
                if time.monotonic() - last_print > 10:
                    print(json.dumps({"mixed_progress": value}), flush=True)
                    last_print = time.monotonic()
                if value["text_failed"] or value["ocr_failed"] or value["media_failed"] or errors:
                    raise RuntimeError("Mixed indexing/search failed")
                if not paused and value["text_chunks_done"] > 0 and value["ocr_ready"] > 0:
                    pause_requested = value
                    assert value["text_remaining"] > 0 and value["ocr_ready"] < image_count
                    control.control(chat, "text", "pause")
                    control.control(chat, "media", "pause")
                    flush_deadline = min(began + deadline_seconds, time.monotonic() + 45)
                    while True:
                        value = progress()
                        with media.lock:
                            media_active = bool(media.active_stages)
                        if value["text_running"] == 0 and not media_active:
                            break
                        if time.monotonic() > flush_deadline:
                            raise TimeoutError("Paused batches did not flush")
                        stop.wait(0.1)
                    before = progress()
                    stop.wait(2)
                    pause_stable = progress()
                    assert before == pause_stable, "Index writes continued while paused"
                    control.control(chat, "text", "resume")
                    control.control(chat, "media", "resume")
                    paused = True
                if (
                    value["text_remaining"] == 0
                    and value["ocr_ready"] == image_count
                    and value["images_ready"] == image_count
                    and value["ocr_dense_ready"] == value["ocr_nonempty"]
                ):
                    after = value
                    break
                stop.wait(0.25)
            else:
                raise TimeoutError("Mixed indexing exceeded its time budget")
            stop.set()
            query_thread.join(timeout=30)
            if query_thread.is_alive():
                raise TimeoutError("Interactive search did not finish")
            assert not errors and match_counts and max(match_counts) > 0
            assert (
                paused
                and before
                and all(
                    after[key] >= before[key]
                    for key in ("text_chunks_done", "ocr_ready", "images_ready", "ocr_dense_ready")
                )
            )
            final_search = UnifiedSearch(
                HybridSearch(db, semantic, importer.lifecycle_lock),
                MediaSearch(db, media, semantic, importer.lifecycle_lock),
            )
            branches = {}
            for kind in ("text", "images", "ocr"):
                result = final_search.search(
                    "Meeting schedule 12345", Filters(), False, 5, "meaning", modalities=[kind]
                )
                assert not result.get("warnings"), f"Degraded final {kind} search"
                assert result["results"], f"Empty final {kind} search"
                assert result["effective_mode"] == ("images" if kind == "images" else "meaning")
                branches[kind] = {
                    "results": len(result["results"]),
                    "effective_mode": result["effective_mode"],
                    "warnings": 0,
                }
            assert semantic.encoder.query_execution.provider == "CPUExecutionProvider"
            assert media.clip.query_execution.provider == "CPUExecutionProvider"
            return {
                "elapsed_seconds": round(time.monotonic() - began, 4),
                "synthetic_messages": message_count + image_count,
                "synthetic_photos": image_count,
                "progress": after,
                "pause_resume": paused,
                "pause_checkpoint": before,
                "pause_requested_checkpoint": pause_requested,
                "pause_stable_checkpoint": pause_stable,
                "search_errors": len(errors),
                "queries_with_partial_index_warnings": sum(count > 0 for count in warning_counts),
                "final_cpu_search_branches": branches,
                "combined_cpu_query_seconds": distribution(latencies),
                "queries_with_matches": sum(count > 0 for count in match_counts),
                "queues": media.status()["queues"],
                "telemetry": telemetry.summary(began, time.monotonic()),
            }
        finally:
            stop.set()
            media.shutdown()
            semantic.shutdown()
            if query_thread:
                query_thread.join(timeout=5)
            importer.shutdown()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models-workspace", type=Path, required=True)
    parser.add_argument("--ocr-models", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seconds", type=int, default=30, choices=range(5, 121))
    parser.add_argument(
        "--sections",
        nargs="+",
        choices=("encoders", "ocr", "mixed"),
        default=["encoders", "ocr", "mixed"],
    )
    parser.add_argument("--prefetch-first", action="store_true")
    args = parser.parse_args()
    if util.find_spec("torch") is not None:
        raise RuntimeError("GPU validation must not install Torch")
    assert Execution("gpu").provider == CUDA
    hardware = (
        subprocess.check_output(
            [
                "nvidia-smi",
                "--id=0",
                "--query-gpu=name,driver_version,memory.total",
                "--format=csv,noheader,nounits",
            ],
            text=True,
            timeout=10,
        )
        .strip()
        .split(",")
    )
    report = {
        "synthetic_only": True,
        "torch_absent": True,
        "hardware": {
            "gpu": hardware[0].strip(),
            "driver": hardware[1].strip(),
            "vram_mib": float(hardware[2]),
        },
        "runtime": {
            "onnxruntime": metadata.version("onnxruntime-gpu"),
            "python": platform.python_version(),
        },
        "seconds_per_phase": args.seconds,
        "sections": args.sections,
        "prefetch_first": args.prefetch_first,
        "phases": {},
    }
    telemetry = Telemetry()

    def record(name, value):
        report["phases"][name] = value
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"phase": name, "completed": True}), flush=True)

    try:
        if "encoders" in args.sections:
            run_encoders(args, telemetry, record)
        if "ocr" in args.sections:
            run_ocr(args, telemetry, record)
        if "mixed" in args.sections:
            record(
                "mixed-indexing-cpu-search",
                mixed_indexing(args.models_workspace, args.ocr_models, telemetry),
            )
        report["completed"] = True
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    finally:
        telemetry.close()


def run_encoders(args, telemetry, record):
    spec = registry()["small"]
    encoder = E5Encoder(
        spec, BundleStore(args.models_workspace).verify(spec), device="gpu", threads=4
    )
    try:
        texts = [f"Meeting schedule Архив сообщений номер {i}. " * (10 + i % 20) for i in range(32)]
        for prefetch in (True, False) if args.prefetch_first else (False, True):
            record(
                f"e5-small-prefetch-{prefetch}",
                prepared_load(
                    encoder,
                    lambda current=encoder: current.prepare_text(texts, "passage"),
                    lambda value, current=encoder: current.encode_text(
                        texts, "passage", _prepared=value
                    ),
                    len(texts),
                    args.seconds,
                    telemetry,
                    prefetch=prefetch,
                ),
            )
    finally:
        encoder.unload()
    del encoder
    gc.collect()
    encoder = ClipEncoder(args.models_workspace, device="gpu", threads=4)
    try:
        images = []
        for index in range(32):
            stream = io.BytesIO()
            Image.new("RGB", (640 + index * 8, 480), (index * 7, 100, 150)).save(
                stream, format="PNG"
            )
            images.append(stream.getvalue())
        for prefetch in (True, False) if args.prefetch_first else (False, True):
            record(
                f"clip-prefetch-{prefetch}",
                prepared_load(
                    encoder,
                    lambda current=encoder: current.prepare_images(images),
                    lambda value, current=encoder: current.encode_images(images, _prepared=value),
                    len(images),
                    args.seconds,
                    telemetry,
                    prefetch=prefetch,
                ),
            )
    finally:
        encoder.unload()
    del encoder
    gc.collect()


def run_ocr(args, telemetry, record):
    data, truth = dataset()
    with tempfile.TemporaryDirectory(prefix="bts-ocr-cuda-") as temporary:
        db = Database(Path(temporary))
        db.initialize()
        db.settings = replace(
            db.settings, ocr_engine="paddle", ocr_device="gpu", ocr_region_batch_size=32
        )
        copy_model(args.ocr_models, db.workspace, ocr_spec())
        engine = OnnxOcrEngine(db.workspace, db.settings)
        try:
            reference = [engine.recognize(image) for image in data]
            for size in (1, 4):
                start, count, durations = time.monotonic(), 0, []
                while time.monotonic() - start < args.seconds:
                    offset = count % len(data)
                    batch = data[offset : offset + size]
                    began = time.monotonic()
                    values = engine.recognize_many(batch)
                    assert [value["text"] for value in values] == [
                        value["text"] for value in reference[offset : offset + size]
                    ]
                    count += len(batch)
                    durations.append(time.monotonic() - began)
                finish = time.monotonic()
                record(
                    f"ocr-images-{size}",
                    {
                        "images": count,
                        "batch_size": size,
                        "elapsed_seconds": round(finish - start, 4),
                        "images_per_minute": round(count * 60 / (finish - start), 2),
                        "same_transcriptions": True,
                        "batch_seconds": distribution(durations),
                        "telemetry": telemetry.summary(start, finish),
                        "worker_starts": engine.worker.starts,
                    },
                )
        finally:
            engine.unload()
        cpu = OnnxOcrEngine(db.workspace, replace(db.settings, ocr_device="cpu"))
        try:
            cpu_values = [
                value
                for offset in range(0, len(data), 4)
                for value in cpu.recognize_many(data[offset : offset + 4])
            ]
            mismatches = sum(
                left["text"] != right["text"]
                for left, right in zip(reference, cpu_values, strict=True)
            )
            assert mismatches == 0, "CPU/GPU OCR transcriptions differ"
            record(
                "ocr-cpu-gpu-quality",
                {
                    "synthetic_images": len(data),
                    "transcription_mismatches": mismatches,
                    "cpu": quality(cpu_values, truth),
                    "gpu": quality(reference, truth),
                },
            )
        finally:
            cpu.unload()


if __name__ == "__main__":
    main()
