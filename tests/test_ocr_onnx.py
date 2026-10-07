import io
from types import SimpleNamespace

import pytest
from PIL import Image

np = pytest.importorskip("numpy")
pytest.importorskip("cv2")
pytest.importorskip("pyclipper")

from telegram_search.inference.ocr_pipeline import (  # noqa: E402
    OnnxOcrPipeline,
    decode_ctc,
    detected_boxes,
)
from telegram_search.inference.providers import CPU, CUDA  # noqa: E402
from telegram_search.shared.errors import UserError  # noqa: E402


def test_ctc_preserves_repeated_letters_separated_by_blank_and_rejects_bad_contract():
    probabilities = np.eye(3, dtype=np.float32)[[0, 1, 1, 0, 1, 2, 2]]
    assert decode_ctc(probabilities, ["", "А", "B"]) == ("ААB", 1.0)
    assert decode_ctc(np.eye(3, dtype=np.float32)[[0, 0]], ["", "А", "B"]) == ("", 0.0)
    with pytest.raises(ValueError):
        decode_ctc(probabilities, ["", "А"])
    probabilities[0, 0] = np.nan
    with pytest.raises(ValueError):
        decode_ctc(probabilities, ["", "А", "B"])


def test_detection_reads_lines_in_order_and_bounds_expanded_boxes():
    scores = np.zeros((100, 200), dtype=np.float32)
    scores[10:20, 10:70] = 1
    scores[10:20, 100:160] = 1
    scores[55:65, 10:100] = 1
    boxes = detected_boxes(scores, 400, 200)
    assert len(boxes) == 3
    assert boxes[0][0, 0] < boxes[1][0, 0]
    assert boxes[1][0, 1] < boxes[2][0, 1]
    assert all(np.all(box >= 0) and np.all(box <= [399, 199]) for box in boxes)
    assert not detected_boxes(np.zeros_like(scores), 400, 200)


def test_recognition_batches_crops_and_retries_oom_without_changing_order():
    pipeline = OnnxOcrPipeline.__new__(OnnxOcrPipeline)
    pipeline.alphabet = ["", "А", "B"]
    batches = []

    class Recognizer:
        def get_inputs(self):
            return [SimpleNamespace(name="x")]

        def run(self, names, inputs):
            tensor = inputs["x"]
            batches.append(len(tensor))
            assert tensor.shape[1:] == (3, 48, 320)
            if len(tensor) > 2:
                raise MemoryError("synthetic CUDA out of memory")
            indices = np.where(tensor[:, 0, 0, 0] < 0, 1, 2)
            return [np.eye(3, dtype=np.float32)[indices][:, None, :]]

    pipeline.recognizer = Recognizer()
    crops = [np.full((48, 100, 3), 0 if i % 2 == 0 else 255, np.uint8) for i in range(8)]
    assert pipeline._recognize_crops(crops) == [("А", 1.0), ("B", 1.0)] * 4
    assert batches == [8, 4, 2, 2, 4, 2, 2]


def test_recognition_does_not_hide_non_memory_failures():
    pipeline = OnnxOcrPipeline.__new__(OnnxOcrPipeline)

    class Recognizer:
        def get_inputs(self):
            return [SimpleNamespace(name="x")]

        def run(self, names, inputs):
            raise ValueError("synthetic invalid graph")

    pipeline.recognizer = Recognizer()
    with pytest.raises(ValueError, match="invalid graph"):
        pipeline._recognize_crops([np.zeros((48, 100, 3), np.uint8)] * 8)


def test_line_padding_is_stable_across_mixed_batches_and_oom():
    pipeline = OnnxOcrPipeline.__new__(OnnxOcrPipeline)
    pipeline.alphabet = ["", "А", "B"]

    class Recognizer:
        limit = 8

        def get_inputs(self):
            return [SimpleNamespace(name="x")]

        def run(self, names, inputs):
            tensor = inputs["x"]
            if len(tensor) > self.limit:
                raise MemoryError("synthetic out of memory")
            # Deliberately depend on tensor width, as a bidirectional model can.
            index = 1 if tensor.shape[-1] == 320 else 2
            return [np.eye(3, dtype=np.float32)[[index] * len(tensor)][:, None, :]]

    pipeline.recognizer = Recognizer()
    crops = [np.zeros((48, width, 3), np.uint8) for width in [100, 400, 310, 510, 90, 490]]
    individually = [pipeline._recognize_crops([crop])[0] for crop in crops]
    assert pipeline._recognize_crops(crops) == individually
    pipeline.recognizer.limit = 1
    assert pipeline._recognize_crops(crops) == individually


@pytest.mark.parametrize("device", ["cpu", "gpu", "auto"])
def test_both_ocr_models_use_selected_provider_and_only_auto_can_fall_back(
    tmp_path,
    monkeypatch,
    device,
):
    dictionary = tmp_path / "languages/eslav/dict.txt"
    dictionary.parent.mkdir(parents=True)
    dictionary.write_text("А\nB\n", encoding="utf-8")
    requested = []
    sessions = []

    class Session:
        def __init__(self, recognizer):
            self.recognizer = recognizer

        def get_inputs(self):
            return [SimpleNamespace(name="x")]

        def run(self, names, inputs):
            shape = (1, 1, 4) if self.recognizer else (1, 1, 32, 32)
            return [np.zeros(shape, dtype=np.float32)]

    class Execution:
        def __init__(self, selected, **kwargs):
            requested.append(selected)
            self.provider = CPU if selected == "cpu" else CUDA

        def session(self, path):
            sessions.append((self.provider, path))
            if self.provider == CUDA:
                raise UserError("synthetic unsupported GPU graph")
            return Session(path.endswith("rec.onnx"))

    monkeypatch.setattr("telegram_search.inference.ocr_pipeline.Execution", Execution)
    kwargs = dict(max_edge=2400, device=device, device_id=1, memory_limit_mib=1024, threads=1)
    if device == "gpu":
        with pytest.raises(UserError):
            OnnxOcrPipeline(tmp_path, **kwargs)
        assert all(provider == CUDA for provider, _ in sessions)
        return
    pipeline = OnnxOcrPipeline(tmp_path, **kwargs)
    assert requested == [device]
    assert sessions[-2][0] == sessions[-1][0] == CPU
    data = io.BytesIO()
    Image.new("RGB", (64, 64), "white").save(data, format="PNG")
    result = pipeline.recognize(data.getvalue())
    assert result == {"text": "", "confidence": 0.0, "provider": CPU}


@pytest.mark.parametrize("device", ["cpu", "gpu", "auto"])
@pytest.mark.parametrize("broken_graph", ["detector", "recognizer"])
def test_both_kernels_are_checked_before_ready(tmp_path, monkeypatch, device, broken_graph):
    dictionary = tmp_path / "languages/eslav/dict.txt"
    dictionary.parent.mkdir(parents=True)
    dictionary.write_text("А\nB\n", encoding="utf-8")
    calls = []

    class Session:
        def __init__(self, provider, recognizer):
            self.provider = provider
            self.graph = "recognizer" if recognizer else "detector"

        def get_inputs(self):
            return [SimpleNamespace(name="x")]

        def run(self, names, inputs):
            calls.append((self.graph, self.provider))
            if self.graph == broken_graph and (self.provider == CUDA or device == "cpu"):
                raise RuntimeError("synthetic unsupported kernel")
            shape = (1, 1, 4) if self.graph == "recognizer" else (1, 1, 32, 32)
            return [np.zeros(shape, dtype=np.float32)]

    class Execution:
        def __init__(self, selected, **kwargs):
            self.provider = CPU if selected == "cpu" else CUDA

        def session(self, path):
            return Session(self.provider, path.endswith("rec.onnx"))

    monkeypatch.setattr("telegram_search.inference.ocr_pipeline.Execution", Execution)
    kwargs = dict(max_edge=2400, device=device, device_id=0, memory_limit_mib=512, threads=1)
    if device == "auto":
        assert OnnxOcrPipeline(tmp_path, **kwargs).execution.provider == CPU
        assert (broken_graph, CUDA) in calls
        assert calls[-2:] == [("detector", CPU), ("recognizer", CPU)]
    else:
        with pytest.raises(RuntimeError, match="unsupported kernel"):
            OnnxOcrPipeline(tmp_path, **kwargs)
        assert calls[-1] == (broken_graph, CPU if device == "cpu" else CUDA)
        assert all(provider == (CPU if device == "cpu" else CUDA) for _, provider in calls)
