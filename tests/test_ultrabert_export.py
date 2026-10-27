"""Export graph guards run without Torch/ONNX or model weights."""

import hashlib
import importlib.util
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def exporter():
    path = Path(__file__).resolve().parents[1] / "tools/ultrabert-export/export.py"
    spec = importlib.util.spec_from_file_location("ultrabert_export", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def node(name, kind="Cast", source="input", target="output", to=1, domain=""):
    return SimpleNamespace(
        name=name,
        op_type=kind,
        input=[source],
        output=[target],
        attribute=[SimpleNamespace(name="to", i=to)],
        domain=domain,
    )


def graph(*nodes):
    return SimpleNamespace(graph=SimpleNamespace(node=list(nodes)))


def test_topological_order_is_restored(exporter):
    first = node("cast")
    user = node("consumer", "Identity", "output", "result")
    value = graph(user, first)
    assert exporter.ordered(value).graph.node == [first, user]


def test_cycles_are_rejected_before_saving_graph(exporter):
    value = graph(node("first", source="b", target="a"), node("second", source="a", target="b"))
    with pytest.raises(RuntimeError, match="cycle"):
        exporter.ordered(value)


def test_output_must_be_fresh_before_source_download_or_execution(exporter, tmp_path, monkeypatch):
    existing = tmp_path / "existing"
    existing.mkdir()
    sentinel = existing / "ultrabert-fp32.onnx"
    sentinel.write_bytes(b"unowned")
    monkeypatch.setattr(
        exporter, "verify_sources", lambda path: pytest.fail("Source read before output preflight")
    )
    with pytest.raises(FileExistsError):
        exporter.main(SimpleNamespace(output=existing, source=tmp_path / "source"))
    assert sentinel.read_bytes() == b"unowned"
    link = tmp_path / "link"
    try:
        link.symlink_to(existing, target_is_directory=True)
    except OSError:
        pass  # Windows may require extra privileges; mocked reparse coverage remains.
    else:
        with pytest.raises(RuntimeError, match="links"):
            exporter.prepare_output(link / "new")
    fresh = exporter.prepare_output(tmp_path / "fresh")
    assert fresh.is_dir() and not list(fresh.iterdir())


def test_loader_stage_contains_only_verified_pinned_data(exporter, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    payload = b"pinned data"
    (source / "config.json").write_bytes(payload)
    (source / "reviewed.py").write_text("raise RuntimeError('must never run')")
    (source / "special_tokens_map.json").write_text('{"cls_token":"unapproved"}')
    pins = {
        "files": [
            dict(
                name="config.json", bytes=len(payload), sha256=hashlib.sha256(payload).hexdigest()
            ),
            dict(name="reviewed.py", bytes=1, sha256="unused"),
        ]
    }
    output = exporter.prepare_output(tmp_path / "output")
    stage = exporter.stage_sources(source, output, pins)
    assert [path.name for path in stage.iterdir()] == ["config.json"]
    assert (stage / "config.json").read_bytes() == payload
    assert (source / "special_tokens_map.json").is_file()


def test_stage_rechecks_copied_bytes(exporter, tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    payload = b"pinned data"
    (source / "config.json").write_bytes(payload)
    pins = {
        "files": [
            dict(name="config.json", bytes=len(payload), sha256=hashlib.sha256(payload).hexdigest())
        ]
    }
    monkeypatch.setattr(exporter.shutil, "copyfileobj", lambda source, dest: dest.write(b"changed"))
    with pytest.raises(RuntimeError, match="changed during staging"):
        exporter.stage_sources(source, exporter.prepare_output(tmp_path / "output"), pins)


def test_windows_reparse_attribute_is_rejected_on_any_host(exporter, tmp_path, monkeypatch):
    original = Path.lstat
    junction = tmp_path / "junction"
    monkeypatch.setattr(
        Path,
        "lstat",
        lambda path: (
            SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
            if path == junction
            else original(path)
        ),
    )
    assert exporter.linked(junction)
    with pytest.raises(RuntimeError, match="links"):
        exporter.prepare_output(junction / "new-output")


def test_storage_compression_preserves_float32_execution_and_vector_parameters(exporter):
    onnx = pytest.importorskip("onnx")
    np = pytest.importorskip("numpy")
    from onnx import TensorProto, helper, numpy_helper

    matrix = np.array([[1.001, 2.002], [3.003, 4.004]], dtype=np.float32)
    norm = np.array([1.001, 2.002], dtype=np.float32)
    value = helper.make_model(
        helper.make_graph(
            [helper.make_node("MatMul", ["input", "weights"], ["output"])],
            "test",
            [helper.make_tensor_value_info("input", TensorProto.FLOAT, [1, 2])],
            [helper.make_tensor_value_info("output", TensorProto.FLOAT, [1, 2])],
            initializer=[
                numpy_helper.from_array(matrix, "weights"),
                numpy_helper.from_array(norm, "norm"),
            ],
        ),
        opset_imports=[helper.make_opsetid("", 17)],
    )
    assert exporter.compress_weights(value) == 1
    stored, untouched = value.graph.initializer
    assert stored.data_type == TensorProto.FLOAT16
    np.testing.assert_array_equal(numpy_helper.to_array(stored), matrix.astype(np.float16))
    assert untouched.data_type == TensorProto.FLOAT
    np.testing.assert_array_equal(numpy_helper.to_array(untouched), norm)
    exporter.ordered(value)
    assert value.graph.node[0].op_type == "Cast"
    assert value.graph.node[0].attribute[0].i == TensorProto.FLOAT
    value.graph.node[0].doc_string = "host-specific export source path"
    onnx.helper.strip_doc_string(value)
    assert not value.graph.node[0].doc_string
    onnx.checker.check_model(value)


def test_weight_compression_rejects_overflow_and_name_collisions(exporter):
    pytest.importorskip("onnx")
    np = pytest.importorskip("numpy")
    from onnx import helper, numpy_helper

    for array in (
        np.array([[float("nan")]], dtype=np.float32),
        np.array([[1e20]], dtype=np.float32),
    ):
        value = helper.make_model(
            helper.make_graph(
                [], "test", [], [], initializer=[numpy_helper.from_array(array, "weights")]
            )
        )
        with np.errstate(over="ignore"), pytest.raises(RuntimeError, match="nonfinite"):
            exporter.compress_weights(value)
    value = helper.make_model(
        helper.make_graph(
            [helper.make_node("Identity", ["other"], ["bts_fp16_matrix_0"])],
            "test",
            [],
            [],
            initializer=[numpy_helper.from_array(np.ones((2, 2), dtype=np.float32), "weights")],
        )
    )
    with pytest.raises(RuntimeError, match="name collision"):
        exporter.compress_weights(value)
