"""CPU-only synthetic RU/EN OCR quality/throughput and finished-prefix SQL benchmark.

Run with uv run --locked --extra semantic --extra ocr python scripts/benchmark_ocr.py
--models PATH. Models must be a verified pinned OCR bundle, never an archive folder.
Reports contain numbers/configuration only, no recognized text or input paths.
"""

import argparse
import io
import json
import platform
import sqlite3
import statistics
import time
from importlib import metadata
from pathlib import Path

import psutil
from PIL import Image, ImageDraw, ImageFont

from telegram_search.config.model_registry import ocr_spec
from telegram_search.inference.bundles import checksum
from telegram_search.inference.ocr_pipeline import OnnxOcrPipeline


def dataset():
    font = ImageFont.truetype("DejaVuSans.ttf", 25)
    examples = [
        ("Архив сообщений 12345", 640, 100, 0),
        ("Meeting schedule 67890", 640, 100, 0),
        ("Отчёт о встрече 12345\nПроверка русского текста\nRelease schedule 67890", 680, 180, 0),
        ("Document number 12345\nДата встречи 2026\nTelegram archive search", 680, 200, 3),
        ("Мелкий текст 67890", 640, 100, 0),
        ("", 640, 100, 0),
        ("\n".join(f"Строка {i:02d} Document number 12345" for i in range(1, 26)), 700, 1150, 0),
        ("Архив сообщений 12345", 640, 100, 90),
    ]
    data, truth = [], []
    for index, (text, width, height, angle) in enumerate(examples):
        image = Image.new("RGB", (width, height), "white")
        ImageDraw.Draw(image).multiline_text((20, 20), text, font=font, fill="black", spacing=12)
        if index == 4:
            image = image.resize((320, 50))
        if angle:
            image = image.rotate(angle, expand=True, fillcolor="white")
        stream = io.BytesIO()
        image.save(stream, format="PNG")
        data.append(stream.getvalue())
        truth.append(text)
    return data, truth


def distance(left, right):
    previous = list(range(len(right) + 1))
    for i, x in enumerate(left, 1):
        current = [i]
        for j, y in enumerate(right, 1):
            current.append(min(current[-1] + 1, previous[j] + 1, previous[j - 1] + (x != y)))
        previous = current
    return previous[-1]


def quality(values, truth):
    chars = words = char_errors = word_errors = found = controls = 0
    for value, target in zip(values, truth, strict=True):
        text = " ".join(value.get("text", "").lower().split())
        target = " ".join(target.lower().split())
        chars += len(target)
        words += len(target.split())
        char_errors += distance(target, text)
        word_errors += distance(target.split(), text.split())
        for token in ("12345", "67890", "2026"):
            if token in target:
                controls += 1
                found += token in text
    return {
        "cer": round(char_errors / max(1, chars), 4),
        "wer": round(word_errors / max(1, words), 4),
        "control_tokens_found": found,
        "control_tokens_total": controls,
    }


def queue_benchmark(count=40060, completed=40000, trials=30):
    with sqlite3.connect(":memory:") as conn:
        conn.executescript("""
            CREATE TABLE chats(id INTEGER PRIMARY KEY,ocr_paused INTEGER);
            INSERT INTO chats VALUES(1,0);
            CREATE TABLE media_refs(sha256 TEXT,chat_id INTEGER,kind TEXT,status TEXT);
            CREATE INDEX media_by_blob ON media_refs(sha256,kind,status);
            CREATE TABLE ocr_cache(sha256 TEXT,version TEXT,UNIQUE(sha256,version));
            CREATE TABLE ocr_work(sha256 TEXT,version TEXT,state TEXT,PRIMARY KEY(version,sha256));
            CREATE INDEX ocr_work_pending ON ocr_work(version,state,sha256);
        """)
        keys = [f"{i:064x}" for i in range(count)]
        conn.executemany(
            "INSERT INTO media_refs VALUES(?,1,'photo','ready')", [(key,) for key in keys]
        )
        conn.executemany(
            "INSERT INTO ocr_cache VALUES(?,'v1')", [(key,) for key in keys[:completed]]
        )
        conn.executemany(
            "INSERT INTO ocr_work VALUES(?,'v1',?)",
            [(key, "done" if i < completed else "pending") for i, key in enumerate(keys)],
        )
        queries = {
            "previous": "SELECT r.sha256 FROM media_refs r JOIN chats c ON c.id=r.chat_id "
            "WHERE c.ocr_paused=0 AND r.kind='photo' AND r.status='ready' "
            "AND NOT EXISTS (SELECT 1 FROM ocr_cache o WHERE o.sha256=r.sha256 "
            "AND o.version='v1') LIMIT 1",
            "pending": "SELECT w.sha256 FROM ocr_work w WHERE w.version='v1' AND w.state='pending' "
            "AND EXISTS (SELECT 1 FROM media_refs r JOIN chats c ON c.id=r.chat_id "
            "WHERE r.sha256=w.sha256 AND r.status='ready' AND r.kind='photo' AND c.ocr_paused=0) "
            "ORDER BY w.sha256 LIMIT 4",
        }
        report = {"images": count, "completed_prefix": completed, "trials": trials}
        for name, query in queries.items():
            timings = []
            for _ in range(trials):
                start = time.perf_counter()
                assert conn.execute(query).fetchall()
                timings.append((time.perf_counter() - start) * 1000)
            report[name] = {
                "p50_ms": round(statistics.median(timings), 4),
                "p95_ms": round(sorted(timings)[int(trials * 0.95) - 1], 4),
            }
    return report


def benchmark(models, threads, trials):
    spec = ocr_spec()
    for item in spec.manifest["files"]:
        path = models / item["name"]
        if (
            not path.is_file()
            or path.stat().st_size != item["bytes"]
            or checksum(path) != item["sha256"]
        ):
            raise ValueError("Pinned model bundle verification failed")
    data, truth = dataset()
    report = {
        "device": "cpu",
        "runtime": {
            "onnxruntime": metadata.version("onnxruntime"),
            "python": platform.python_version(),
        },
        "threads": threads,
        "trials": trials,
        "synthetic_unique_images": len(data),
        "queue": queue_benchmark(),
        "modes": {},
    }
    reference = None
    for batch, regions in ((1, 8), (2, 8), (4, 8), (4, 16)):
        start = time.perf_counter()
        pipeline = OnnxOcrPipeline(
            models, max_edge=2400, device="cpu", device_id=0, memory_limit_mib=4096, threads=threads
        )
        cold = time.perf_counter() - start
        timings, values, phases = [], [], []
        for _ in range(trials):
            start = time.perf_counter()
            values = []
            for offset in range(0, len(data), batch):
                if batch == 1:
                    value = pipeline.recognize(data[offset])
                    values.append(value)
                    phases.append(dict(pipeline.last_timings))
                else:
                    chunk = pipeline.recognize_many(
                        data[offset : offset + batch], region_batch=regions
                    )
                    values.extend(chunk)
                    phases.extend(value.get("timings", {}) for value in chunk)
            timings.append(time.perf_counter() - start)
        if reference is None:
            reference = values
        report["modes"][f"images={batch},regions={regions}"] = {
            "cold_load_seconds": round(cold, 4),
            "warm_images_per_minute": round(len(data) * 60 / statistics.median(timings), 2),
            "warm_dataset_p50_seconds": round(statistics.median(timings), 4),
            "same_transcriptions": [value["text"] for value in values]
            == [value["text"] for value in reference],
            "rss_bytes": psutil.Process().memory_info().rss,
            "phase_means": {
                key: round(statistics.mean(p.get(key, 0) for p in phases), 6)
                for key in (
                    "preprocess_seconds",
                    "detection_seconds",
                    "boxes_seconds",
                    "recognition_seconds",
                    "regions",
                )
            },
            **quality(values, truth),
        }
        invalid = pipeline.recognize_many([data[0], b"corrupt", data[1]])
        assert invalid[1] == {"error": True} and invalid[0]["text"] and invalid[2]["text"]
        del pipeline
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", type=Path)
    parser.add_argument("--threads", type=int, default=4, choices=range(1, 33))
    parser.add_argument("--trials", type=int, default=5, choices=range(1, 31))
    args = parser.parse_args()
    print(
        json.dumps(
            benchmark(args.models, args.threads, args.trials) if args.models else queue_benchmark(),
            indent=2,
        )
    )
