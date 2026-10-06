"""Compare pinned upstream ONNX against PyTorch in a separate developer environment."""

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import onnx
import torch
from transformers import AutoModel, AutoTokenizer

from telegram_search.config.model_registry import model_spec
from telegram_search.inference.bundles import BundleStore

# Declared before inference: numerical compatibility, padding invariance and ranking.
MAX_ABSOLUTE_ERROR = 1e-4
MIN_COSINE_AGREEMENT = 0.99999
MIN_TOPK_OVERLAP = 0.99

parser = argparse.ArgumentParser()
parser.add_argument("--workspace", type=Path, default=Path("../workspace"))
parser.add_argument("--profile", choices=["small", "base"], default="small")
parser.add_argument("--output", type=Path, required=True)
parser.add_argument("--offline", action="store_true")
args = parser.parse_args()
workspace = args.workspace.resolve()
spec = model_spec(args.profile)
bundle = BundleStore(workspace).verify(spec)
project = Path(__file__).resolve().parents[1]
runtime_python = (
    project / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
)
torch.set_num_threads(4)
cases = {
    "query": [
        "Где мы собирались встретиться перед поездкой?",
        "Что почитать о путешествиях?",
        "Когда заканчивается срок оплаты?",
        "What should I bring to the lake?",
        "Кто починит колесо велосипеда?",
        "ё Ё е Е Unicode 👩🏽‍💻",
        "длинная русская реплика о путешествии и планах " * 32,
    ],
    "passage": [
        "Алиса: Встретимся на вокзале в субботу утром.",
        "Борис: Возьми велосипед и бутылку воды, поедем к озеру.",
        "Вера: Я читаю новую книгу о путешествиях на поезде.",
        "Глеб: Оплатить счёт нужно до конца недели.",
        "Дина: Завтра отнесу колесо в мастерскую.",
        "Alice: Please bring water and sunscreen for our trip to the lake.",
        "Автор: ё Ё е Е Unicode 👩🏽‍💻",
        "Автор: длинная русская реплика о путешествии и планах " * 27,
    ],
}
with tempfile.TemporaryDirectory(prefix="validation-", dir=workspace) as temporary:
    temporary = Path(temporary)
    cases_path = temporary / "cases.json"
    cases_path.write_text(json.dumps(cases, ensure_ascii=False), encoding="utf-8")
    actual_path = temporary / "actual.npz"
    subprocess.run(
        [
            str(runtime_python),
            str(project / "validation/onnx_outputs.py"),
            "--workspace",
            str(workspace),
            "--profile",
            args.profile,
            "--cases",
            str(cases_path),
            "--output",
            str(actual_path),
        ],
        check=True,
    )
    actual = np.load(actual_path)
    runtime_info = json.loads(Path(str(actual_path) + ".json").read_text())
    # Read graph metadata once in this separate environment; not a runtime dependency.
    graph = onnx.load(bundle / "onnx/model.onnx", load_external_data=False)
    opsets = {item.domain or "ai.onnx": item.version for item in graph.opset_import}
    del graph
    tokenizer = AutoTokenizer.from_pretrained(
        spec.model_id,
        revision=spec.revision,
        use_fast=True,
        trust_remote_code=False,
        cache_dir=workspace / "models/reference",
        local_files_only=args.offline,
    )
    reference = (
        AutoModel.from_pretrained(
            spec.model_id,
            revision=spec.revision,
            use_safetensors=True,
            dtype=torch.float32,
            trust_remote_code=False,
            cache_dir=workspace / "models/reference",
            local_files_only=args.offline,
        )
        .eval()
        .to("cpu")
    )
    reference_outputs = {}
    for purpose, texts in cases.items():
        inputs = tokenizer(
            [spec.manifest[f"{purpose}_prefix"] + text for text in texts],
            padding=True,
            truncation=False,
            return_tensors="pt",
        )
        with torch.inference_mode():
            hidden = reference(**inputs).last_hidden_state
            mask = inputs["attention_mask"].unsqueeze(-1)
            mean = (hidden * mask).sum(1) / mask.sum(1).clamp(min=1)
            reference_outputs[purpose] = torch.nn.functional.normalize(mean, dim=1).numpy()
    errors, cosines, padding_errors = [], [], []
    for purpose in cases:
        expected = reference_outputs[purpose]
        for batch_size in (1, 2, 4):
            values = actual[f"{purpose}_{batch_size}"]
            errors.append(float(np.abs(values - expected).max()))
            cosines.append(float((values * expected).sum(axis=1).min()))
            padding_errors.append(float(np.abs(values - actual[f"{purpose}_1"]).max()))
    actual_ranks = np.argsort(-(actual["query_4"] @ actual["passage_4"].T), axis=1)
    expected_ranks = np.argsort(
        -(reference_outputs["query"] @ reference_outputs["passage"].T), axis=1
    )
    k = min(5, len(cases["passage"]))
    overlap = float(
        np.mean(
            [
                len(set(left[:k]) & set(right[:k])) / k
                for left, right in zip(actual_ranks, expected_ranks, strict=True)
            ]
        )
    )
    report = {
        "profile": args.profile,
        "model_id": spec.model_id,
        "revision": spec.revision,
        "runtime": runtime_info,
        "reference_torch": torch.__version__,
        "opsets": opsets,
        "query_cases": len(cases["query"]),
        "passage_cases": len(cases["passage"]),
        "batch_sizes": [1, 2, 4],
        "max_absolute_error": max(errors),
        "min_cosine_agreement": min(cosines),
        "max_padding_error": max(padding_errors),
        "topk_overlap": overlap,
        "ranking_k": k,
        "thresholds": {
            "max_absolute_error": MAX_ABSOLUTE_ERROR,
            "min_cosine_agreement": MIN_COSINE_AGREEMENT,
            "min_topk_overlap": MIN_TOPK_OVERLAP,
        },
    }
    report["passed"] = (
        max(errors) <= MAX_ABSOLUTE_ERROR
        and min(cosines) >= MIN_COSINE_AGREEMENT
        and max(padding_errors) <= MAX_ABSOLUTE_ERROR
        and overlap >= MIN_TOPK_OVERLAP
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["passed"]:
        sys.exit(1)
