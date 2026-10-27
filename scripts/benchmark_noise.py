"""Reproducible CPU-only statistics benchmark using generated synthetic messages."""

import argparse
import json
import os
import platform
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from telegram_search.evaluation.files import check_output, write_report
from telegram_search.indexing.noise_analysis import NoiseAnalysisService
from telegram_search.ingestion.importer import ImportService
from telegram_search.search.chunking.classification import whole_word
from telegram_search.search.chunking.noise.candidates import CandidateGenerator
from telegram_search.search.chunking.noise.contracts import AnalysisOptions, CandidateLimits
from telegram_search.search.chunking.noise.lexicon import load_lexicon
from telegram_search.search.chunking.policy import APPROVED_FILLER
from telegram_search.storage.database import Database
from telegram_search.storage.noise import NoiseReport

VERSION = "noise-cpu-synthetic-v1"
ROOT = Path(__file__).resolve().parents[1]
PUBLIC_OUTPUT = ROOT / "docs/benchmarks/noise-synthetic-cpu.json"
CASES = ((10000, 32), (100000, 32), (100000, 10000), (100000, 50000))
BASE_FORMS = (
    "бля",
    "бял",
    "блля",
    "пиздец",
    "пздец",
    "пизднц",
    "сука",
    "сук",
    "сууука",
    "хаха",
    "хаах",
    "ахаха",
    "ахах",
    "лол",
    "lol",
    "lmao",
    "rofl",
    "кек",
    "kek",
    "да",
    "нет",
    "ок",
    "ага",
    "угу",
    "для",
    "пол",
    "рука",
    "суда",
    "мама",
    "папа",
    "класс",
    "hello",
)


def synthetic_form(number):
    if number < len(BASE_FORMS):
        return BASE_FORMS[number]
    letters = []
    while number:
        number, remainder = divmod(number, 26)
        letters.append(chr(97 + remainder))
    return "noisebench" + "".join(reversed(letters))


def dense_probe():
    forms = []
    for value in range(160, 8192):
        form = "benchmark" + chr(value)
        if whole_word(form) == form:
            forms.append(form)
            if len(forms) == 512:
                break
    if len(forms) != 512:
        raise RuntimeError("Synthetic dense lexicon setup failed")
    generator = CandidateGenerator(useful_words=forms)
    began = time.perf_counter()
    reports = [generator.analyze("benchmark") for _ in range(1000)]
    return {
        "dataset": "512 generated forms sharing one deletion signature",
        "calls": 1000,
        "wall_seconds": round(time.perf_counter() - began, 6),
        "maximum_candidates_examined_per_call": max(row.comparisons for row in reports),
        "incomplete_calls": sum(not row.complete for row in reports),
        "candidate_pool_cap_per_index": generator.limits.max_candidates,
    }


def write_source(path, messages, forms):
    if (
        type(messages) is not int
        or type(forms) is not int
        or not 1 <= forms <= min(messages, 50000)
        or messages > 1000000
    ):
        raise ValueError("Invalid synthetic benchmark size")
    with path.open("x", encoding="utf-8") as stream:
        stream.write(
            '{"id":100,"name":"Synthetic noise benchmark","type":"personal_chat","messages":['
        )
        for number in range(messages):
            if number:
                stream.write(",")
            # The first U rows cover every form. Later phrase rows cannot erase
            # a deterministic residue class from the observed vocabulary.
            text = synthetic_form(number % forms)
            if number >= forms and number % 8 == 0:
                text = "Synthetic substantive phrase with preserved context."
            row = {
                "id": number + 1,
                "type": "message",
                "date_unixtime": str(1750000000 + number * 3),
                "from": "Synthetic author",
                "from_id": "synthetic" + str(number % 8),
                "text": text,
            }
            if number % 97 == 0:
                row["text_entities"] = [{"type": "code", "text": text}]
            json.dump(row, stream, ensure_ascii=False, separators=(",", ":"))
        stream.write("]}")


def analysis_run(service, chat, messages):
    began = time.perf_counter()
    created = service.create(chat, AnalysisOptions(max_messages=messages, time_limit_seconds=300))
    deadline = time.monotonic() + 330
    while True:
        with service.lock:
            active = created["id"] in service.jobs
        if not active or service.persistence_error:
            break
        if time.monotonic() > deadline or not service.thread.is_alive():
            raise RuntimeError("Synthetic benchmark worker stopped or timed out")
        time.sleep(0.02)
    status = service.status(chat, created["id"])
    if status["state"] not in {"ready", "limited"} or not isinstance(status["summary"], dict):
        raise RuntimeError("Synthetic benchmark analysis failed")
    row = service.store.get(chat, created["id"])
    report = NoiseReport(service.db, created["id"], json.loads(row["fence_json"]))
    with report.connect() as conn:
        hypotheses = conn.execute(
            "SELECT coalesce(sum(json_array_length(payload,'$.noise')),0),"
            "coalesce(sum(json_array_length(payload,'$.useful')),0),count(*) FROM forms"
        ).fetchone()
    summary = status["summary"]
    return {
        "wall_seconds": round(time.perf_counter() - began, 3),
        "thread_cpu_seconds": summary["cpu_seconds"],
        "sampled_process_peak_rss_mib": summary["sampled_process_peak_rss_mib"],
        "process_rss_mib_at_completion": summary["process_rss_mib"],
        "messages_scanned": summary["scanned_messages"],
        "whole_messages": summary["whole_messages"],
        "unique_forms": summary["forms"],
        "examined_candidates": summary["candidate_comparisons"],
        "index_postings_read": summary["index_postings_read"],
        "stored_hypotheses": dict(zip(("noise", "useful", "unchanged"), hypotheses, strict=True)),
        "source_complete": summary["source_complete"],
        "evidence_complete": summary["evidence_complete"],
        "limits": summary["limits"],
        "decisions": summary["decisions"],
        "report_bytes": report.path.stat().st_size,
        "analysis_version": summary["version"],
        "score_version": summary["score_version"],
    }


def worker(root, messages, forms):
    if root.is_symlink() or not root.is_dir() or next(root.iterdir(), None) is not None:
        raise ValueError("Benchmark requires its own empty temporary directory")
    source = root / "result.json"
    write_source(source, messages, forms)
    db = Database(root / "workspace")
    db.initialize()
    importer = ImportService(db)
    service = None
    try:
        imported = importer.run(importer.prepare(str(source)))
        if imported["state"] != "completed":
            raise RuntimeError("Synthetic benchmark import failed")
        service = NoiseAnalysisService(db)
        first = analysis_run(service, imported["chat_id"], messages)
        warm = analysis_run(service, imported["chat_id"], messages)
        deterministic = (
            "messages_scanned",
            "whole_messages",
            "unique_forms",
            "examined_candidates",
            "index_postings_read",
            "stored_hypotheses",
            "decisions",
            "limits",
        )
        if any(first[key] != warm[key] for key in deterministic):
            raise RuntimeError("Synthetic first/warm counters differ")
        lexicon = load_lexicon()
        generator = CandidateGenerator(useful_words=lexicon.forms)
        adversarial = []
        for label, text in (
            ("million_character_word", "я" * 1000000),
            ("long_repeated_block", "ах" * 100000),
            ("technical_url", "https://example.invalid/" + "a" * 10000),
        ):
            began = time.perf_counter()
            reports = [generator.analyze(text) for _ in range(1000)]
            adversarial.append(
                {
                    "kind": label,
                    "input_characters": len(text),
                    "calls": len(reports),
                    "wall_seconds": round(time.perf_counter() - began, 6),
                    "candidate_comparisons": sum(item.comparisons for item in reports),
                    "incomplete_calls": sum(not item.complete for item in reports),
                }
            )
        return {
            "messages": messages,
            "requested_forms": forms,
            "first": first,
            "warm": warm,
            "counters_identical": True,
            "seed_count": len(APPROVED_FILLER),
            "lexicon_records": len(lexicon.forms),
            "lexicon_sha256": lexicon.sha256,
            "seed_index_postings": generator.noise.postings,
            "useful_index_postings": generator.useful.postings,
            "candidate_pool_cap_per_index": CandidateLimits().max_candidates,
            "adversarial_inputs": adversarial,
        }
    finally:
        if service is not None:
            service.close()
        importer.shutdown()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=PUBLIC_OUTPUT)
    parser.add_argument("--worker-root", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--messages", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--forms", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        if args.worker_root is not None:
            result = worker(args.worker_root, args.messages, args.forms)
        else:
            output = check_output(args.output, ROOT, "version", VERSION, (PUBLIC_OUTPUT,))
            rows = []
            for messages, forms in CASES:
                with tempfile.TemporaryDirectory(prefix="bts-noise-bench-") as temporary:
                    child = subprocess.run(
                        [
                            sys.executable,
                            str(Path(__file__).resolve()),
                            "--worker-root",
                            temporary,
                            "--messages",
                            str(messages),
                            "--forms",
                            str(forms),
                        ],
                        capture_output=True,
                        text=True,
                        timeout=900,
                        check=False,
                    )
                    if child.returncode:
                        raise RuntimeError("Synthetic benchmark subprocess failed")
                    rows.append(json.loads(child.stdout))
            result = {
                "version": VERSION,
                "dataset": "generated_synthetic_only",
                "python": platform.python_version(),
                "os": platform.system(),
                "architecture": platform.machine(),
                "logical_cpus": os.cpu_count(),
                "sqlite": sqlite3.sqlite_version,
                "cases": rows,
                "dense_candidate_bucket": dense_probe(),
                "cold_definition": (
                    "First analysis in a fresh subprocess after import. "
                    "OS caches are not cleared; indexes are rebuilt in both runs."
                ),
                "limitations": [
                    "No private data, models, GPU or embedding inference.",
                    "Synthetic workload and sampled whole-process RSS; "
                    "not an isolated allocator peak.",
                    "This measures analysis cost, not retrieval quality or GPU/indexing speedup.",
                    "New automatic omissions remain disabled; this is not gate validation.",
                ],
            }
            write_report(
                output,
                result,
                root=ROOT,
                owner_key="version",
                owner_value=VERSION,
                public_paths=(PUBLIC_OUTPUT,),
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
        parser.exit(1, "Synthetic noise benchmark failed.\n")


if __name__ == "__main__":
    main()
