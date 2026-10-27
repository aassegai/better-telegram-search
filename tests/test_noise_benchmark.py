import importlib.util
import json
import sys
from pathlib import Path

import pytest

from telegram_search.search.chunking.classification import whole_word

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def benchmark():
    spec = importlib.util.spec_from_file_location(
        "noise_benchmark_command", ROOT / "scripts/benchmark_noise.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generated_source_covers_vocabulary_without_private_input(tmp_path, benchmark):
    source = tmp_path / "result.json"
    benchmark.write_source(source, 1000, 73)
    data = json.loads(source.read_text())
    assert len(data["messages"]) == 1000
    forms = {whole_word(row["text"]) for row in data["messages"]} - {None}
    assert len(forms) == 73 and all(len(form) <= 64 for form in forms)
    assert all(row["from"].startswith("Synthetic") for row in data["messages"])
    with pytest.raises(FileExistsError):
        benchmark.write_source(source, 10, 2)


def test_dense_query_respects_candidate_pool_cap(benchmark):
    result = benchmark.dense_probe()
    assert result["maximum_candidates_examined_per_call"] == 256
    assert result["incomplete_calls"] == result["calls"] == 1000


def test_small_pipeline_has_reproducible_nontiming_counters(tmp_path, benchmark):
    result = benchmark.worker(tmp_path, 128, 32)
    assert result["counters_identical"]
    assert result["first"]["unique_forms"] == result["warm"]["unique_forms"] == 32
    assert result["first"]["source_complete"] and result["warm"]["source_complete"]
    assert result["seed_count"] > 0 and result["lexicon_records"] == 39532
    assert all(row["candidate_comparisons"] == 0 for row in result["adversarial_inputs"])
    with pytest.raises(ValueError):
        benchmark.worker(tmp_path, 128, 32)


def test_benchmark_cannot_overwrite_unowned_or_external_output(tmp_path, benchmark, monkeypatch):
    monkeypatch.setattr(benchmark, "ROOT", tmp_path)
    public = tmp_path / "docs/generated.json"
    monkeypatch.setattr(benchmark, "PUBLIC_OUTPUT", public)
    monkeypatch.setattr(benchmark, "CASES", ())
    monkeypatch.setattr(benchmark, "dense_probe", lambda: {})
    private = tmp_path / "workspace/unowned.json"
    private.parent.mkdir()
    private.write_text('{"synthetic-user-data":true}')
    for output in (private, tmp_path / "external.json"):
        monkeypatch.setattr(sys, "argv", ["benchmark_noise.py", "--output", str(output)])
        with pytest.raises(SystemExit) as result:
            benchmark.main()
        assert result.value.code == 1
    assert private.read_text() == '{"synthetic-user-data":true}'
    monkeypatch.setattr(sys, "argv", ["benchmark_noise.py", "--output", str(public)])
    benchmark.main()
    assert json.loads(public.read_text())["version"] == benchmark.VERSION
