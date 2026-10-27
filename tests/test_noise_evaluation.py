import importlib.util
import json
import math
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from telegram_search.evaluation.files import write_report
from telegram_search.evaluation.noise import ablation, evaluate_dataset, retrieval_metrics
from telegram_search.search.chunking.noise.candidates import CandidateGenerator
from telegram_search.search.chunking.noise.lexicon import load_lexicon
from telegram_search.shared.errors import UserError

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def dataset():
    return json.loads(
        (ROOT / "tests/fixtures/synthetic/noise-evaluation.json").read_text(encoding="utf-8")
    )


@pytest.fixture(scope="module")
def generator():
    return CandidateGenerator(useful_words=load_lexicon().forms)


def test_ablation_metrics_preserve_protection_and_never_validate_new_auto_gate(dataset, generator):
    result = evaluate_dataset(dataset, generator=generator)
    assert result["calibration_status"] == "not_validated" and not result["new_auto_omit_enabled"]
    assert all(result["partition"].values())
    for modes in result["decisions"].values():
        for values in modes.values():
            assert values["false_useful_omissions"] == values["novel_automatic_omissions"] == 0
    full = result["decisions"]["full_candidates"]
    assert full["development"]["gold_uncertain"] == 1
    assert full["development"]["protected_occurrences"] == 2
    assert full["test"]["protected_occurrences"] == 3
    assert full["test"]["noise_omission_recall"] == 0.5
    assert full["test"]["novel_noise_candidate_coverage"] == 1
    assert result["decisions"]["exact_seeds"]["test"]["novel_noise_candidate_coverage"] == 0
    assert (
        result["decisions"]["seeds_plus_repetitions"]["test"]["novel_noise_candidate_coverage"] == 1
    )
    assert result["retrieval"]["provenance"] == "supplied_rankings_only_not_reexecuted"
    encoded = json.dumps(result, ensure_ascii=False)
    assert all(
        private not in encoded
        for private in ("бял", "сука", "doc-a", "synthetic-development", "dev-caption")
    )


@pytest.mark.parametrize("form,seed", [("лоол", "лол"), ("суука", "сука")])
def test_single_edit_lengthening_is_present_in_both_candidate_ablations(generator, form, seed):
    evidence = generator.analyze(form)
    assert seed in {item.form for item in ablation(evidence, "seeds_plus_typos").noise}
    assert seed in {item.form for item in ablation(evidence, "seeds_plus_repetitions").noise}


def test_exact_seed_runtime_behavior_is_independent_of_candidate_input_limits(dataset, generator):
    dataset["cases"][0]["text"] = " " * 600 + "сука"
    result = evaluate_dataset(dataset, generator=generator)["decisions"]["full_candidates"][
        "development"
    ]
    assert result["automatic_omissions"] == result["true_omissions"] == 1
    assert result["source_limited_occurrences"] == 1


def test_false_and_uncertain_omissions_have_explicit_denominators(dataset, generator):
    dataset["cases"][0]["label"] = "keep"
    result = evaluate_dataset(dataset, generator=generator)["decisions"]["full_candidates"][
        "development"
    ]
    assert result["false_useful_omissions"] == 1
    assert result["useful_false_omission_rate"] == pytest.approx(1 / 6)
    assert result["omission_precision_over_decisive_labels"] == 0
    dataset["cases"][0]["label"] = "uncertain"
    result = evaluate_dataset(dataset, generator=generator)["decisions"]["full_candidates"][
        "development"
    ]
    assert result["uncertain_omissions"] == 1
    assert result["omission_precision_over_decisive_labels"] is None
    assert result["precision_treating_uncertain_as_errors"] == 0


def test_declared_human_labels_do_not_certify_a_gate(dataset, generator):
    dataset["kind"], dataset["label_source"] = "annotated", "human_reviewed"
    result = evaluate_dataset(dataset, generator=generator)
    assert result["calibration_status"] == "not_validated"
    assert result["label_source_verification"] == "declared_only_not_independently_verified"
    assert result["reference"]["id"] == "provided_generator_not_pinned"


@pytest.mark.parametrize("field", ["family", "scope", "period"])
def test_declared_groups_cannot_cross_partitions(dataset, generator, field):
    dataset["cases"][-1][field] = dataset["cases"][0][field]
    with pytest.raises(UserError):
        evaluate_dataset(dataset, generator=generator)


def test_renaming_families_cannot_hide_canonical_or_derived_seed_leakage(dataset, generator):
    for form in ("бял", "бля"):
        changed = deepcopy(dataset)
        changed["cases"][7]["text"] = form
        changed["scopes"][1]["frequencies"][form] = {"whole": 3, "eligible": 3, "phrase": 0}
        with pytest.raises(UserError):
            evaluate_dataset(changed, generator=generator)
    dataset["cases"][-1]["text"] = "ЭТО  ПОЛЕЗНАЯ \n фраза со словом сука."
    with pytest.raises(UserError):
        evaluate_dataset(dataset, generator=generator)


@pytest.mark.parametrize("name", ["source", "space", "filters"])
def test_retrieval_snapshots_must_match(dataset, generator, name):
    dataset["retrieval"][name + "_after"] = "a" * 64
    with pytest.raises(UserError):
        evaluate_dataset(dataset, generator=generator)


def test_query_groups_cannot_leak_case_or_query_families(dataset, generator):
    dataset["retrieval"]["queries"][1]["family"] = "swear-b"
    with pytest.raises(UserError):
        evaluate_dataset(dataset, generator=generator)


@pytest.mark.parametrize(
    "mutation", ["duplicate_rank", "unjudged_doc", "duplicate_id", "negative_grade", "bool_grade"]
)
def test_invalid_rankings_are_rejected(dataset, generator, mutation):
    query = dataset["retrieval"]["queries"][0]
    if mutation == "duplicate_rank":
        query["before"] = ["doc-a", "doc-a"]
    elif mutation == "unjudged_doc":
        query["after"] = ["unknown-private-document"]
    elif mutation == "duplicate_id":
        dataset["retrieval"]["queries"][1]["id"] = query["id"]
    else:
        query["relevance"]["doc-a"] = -1 if mutation == "negative_grade" else True
    with pytest.raises(UserError) as error:
        evaluate_dataset(dataset, generator=generator)
    assert "unknown-private" not in str(error.value)


def test_graded_ndcg_and_recall_match_independent_hand_calculation(dataset):
    result = retrieval_metrics(dataset["retrieval"], (1, 2, 3))["splits"]["development"]
    assert result["1"]["before"] == {"recall": 0, "ndcg": 0}
    assert result["1"]["after"] == {"recall": 0.5, "ndcg": 1}
    ideal = 7 + 1 / math.log2(3)
    assert result["3"]["before"]["ndcg"] == pytest.approx((7 / math.log2(3) + 0.5) / ideal)
    assert result["3"]["after"]["ndcg"] == 1
    assert result["2"]["before"]["recall"] == 0.5
    assert result["2"]["after"]["recall"] == 1
    for query in dataset["retrieval"]["queries"]:
        query["relevance"] = {key: 0 for key in query["relevance"]}
    result = retrieval_metrics(dataset["retrieval"], (1,))["splits"]["test"]["1"]
    assert result["before"]["ndcg"] is None and result["before"]["recall"] is None
    assert result["queries_without_positive_judgments"] == 1


@pytest.mark.parametrize(
    "path,value",
    [
        (("schema_version",), True),
        (("kind",), []),
        (("label_source",), "model_guess"),
        (("partition_by",), ["family", "family"]),
        (("cases", 0, "text"), "я" * 16385),
        (("cases", 0, "protected"), 1),
        (("cases", 0, "label"), "omit"),
        (("scopes", 0, "frequencies", "сука", "eligible"), 11),
        (("scopes", 0, "frequencies", "сука", "whole"), True),
        (("scopes", 0, "frequencies", "сука", "eligible"), 0),
    ],
    # Pytest exports the node ID in PYTEST_CURRENT_TEST. Keep the 16K input
    # inside the test, rather than exceeding Windows' 32767-character env cap.
    ids=[
        "boolean-schema",
        "invalid-kind",
        "machine-label-source",
        "duplicate-partition",
        "oversized-text",
        "nonboolean-protection",
        "unknown-label",
        "eligible-exceeds-total",
        "boolean-total",
        "zero-eligible",
    ],
)
def test_invalid_annotations_and_inconsistent_corpus_counts(dataset, generator, path, value):
    current = dataset
    for part in path[:-1]:
        current = current[part]
    current[path[-1]] = value
    with pytest.raises(UserError):
        evaluate_dataset(dataset, generator=generator)


def test_evaluation_and_retrieval_are_cancellable(dataset, generator):
    calls = 0

    def cancelled():
        nonlocal calls
        calls += 1
        if calls == 25:
            raise RuntimeError("synthetic cancellation")

    with pytest.raises(RuntimeError, match="synthetic cancellation"):
        evaluate_dataset(dataset, generator=generator, check=cancelled)
    assert calls == 25

    def stop():
        raise UserError("synthetic stop")

    with pytest.raises(UserError):
        retrieval_metrics(dataset["retrieval"], (1,), stop)


@pytest.fixture
def command(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "noise_evaluation_command", ROOT / "scripts/evaluate_noise.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(
        module, "PUBLIC_FIXTURE", ROOT / "tests/fixtures/synthetic/noise-evaluation.json"
    )
    (tmp_path / "workspace").mkdir()
    return module


def test_command_confines_reports_and_refuses_input_or_unowned_overwrite(
    command, tmp_path, dataset
):
    source = tmp_path / "workspace/labels.json"
    source.write_text(json.dumps(dataset))
    output = tmp_path / "workspace/report.json"
    first = command.run(source, output)
    second = command.run(source, output)
    assert first["decisions"] == second["decisions"]
    original = source.read_bytes()
    with pytest.raises(UserError):
        command.run(source, source)
    assert source.read_bytes() == original
    unowned = tmp_path / "workspace/settings.json"
    unowned.write_text('{"synthetic-user-setting":true}')
    with pytest.raises(UserError):
        command.run(source, unowned)
    assert unowned.read_text(encoding="utf-8") == '{"synthetic-user-setting":true}'
    with pytest.raises(UserError):
        command.run(source, tmp_path / "outside.json")
    with pytest.raises(UserError):
        command.run(source, tmp_path / "workspace/not-a-report.sqlite")


def test_final_deadline_prevents_report_publication(command, tmp_path, monkeypatch):
    source = tmp_path / "workspace/input.json"
    source.write_text("{}")
    output = tmp_path / "workspace/result.json"
    clock = [0]
    monkeypatch.setattr(command, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    def delayed(*args, **kwargs):
        clock[0] = 2
        return {"evaluation_version": "noise-diagnostics-v1"}

    monkeypatch.setattr(command, "evaluate_dataset", delayed)
    with pytest.raises(UserError, match="time limit"):
        command.run(source, output, seconds=1)
    assert not output.exists()
    assert not list(output.parent.glob(".noise-evaluation-*"))


def test_writer_cancellation_after_serialization_leaves_no_result(tmp_path):
    calls = 0

    def check():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise UserError("synthetic stop")

    output = tmp_path / "workspace/result.json"
    with pytest.raises(UserError):
        write_report(
            output,
            {"version": "test"},
            root=tmp_path,
            owner_key="version",
            owner_value="test",
            check=check,
        )
    assert not output.exists()
    assert not list(output.parent.glob(".noise-evaluation-*"))


def test_reader_rejects_duplicate_keys_nonfinite_links_and_nonregular_files(command, tmp_path):
    source = tmp_path / "workspace/input.json"
    for encoded in ('{"private-key":1,"private-key":2}', '{"value":NaN}', '{"value":Infinity}'):
        source.write_text(encoded)
        with pytest.raises(UserError) as error:
            command.read_json(source)
        assert "private-key" not in str(error.value)
    target = tmp_path / "outside.json"
    target.write_text("{}")
    link = tmp_path / "workspace/link.json"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("Symlink creation unavailable")
    with pytest.raises(UserError):
        command.confined(link)
    import os

    if hasattr(os, "mkfifo"):
        fifo = tmp_path / "workspace/input-fifo"
        os.mkfifo(fifo)
        with pytest.raises(UserError):
            command.read_json(fifo)
