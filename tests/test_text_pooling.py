"""Pooling contracts preserve legacy mean semantics and support explicit CLS."""

from dataclasses import replace

import pytest

np = pytest.importorskip("numpy")

from telegram_search.config.model_registry import model_spec  # noqa: E402
from telegram_search.inference.e5 import (  # noqa: E402
    E5Encoder,
    cls_normalize,
    masked_mean_normalize,
)
from telegram_search.shared.errors import UserError  # noqa: E402


def test_cls_is_first_masked_valid_token_not_a_mean_or_nonzero_id():
    hidden = np.array([[[3, 4], [100, -100], [0, 0]]], dtype=np.float16)
    mask = np.array([[1, 1, 0]], dtype=np.int64)
    result = cls_normalize(hidden, mask)
    np.testing.assert_allclose(result, [[0.6, 0.8]], atol=1e-7)
    assert result.dtype == np.float32 and result.flags.c_contiguous
    assert not np.allclose(result, masked_mean_normalize(hidden, mask))


def test_bos_zero_is_valid_while_padding_zero_is_masked(tmp_path):
    import json

    tk = pytest.importorskip("tokenizers")
    from telegram_search.inference.tokenization import ModelTokenizer

    tokenizer = tk.Tokenizer(
        tk.models.WordLevel({"<s>": 0, "a": 1, "b": 2, "<unk>": 3}, unk_token="<unk>")
    )
    tokenizer.pre_tokenizer = tk.pre_tokenizers.Whitespace()
    tokenizer.post_processor = tk.processors.TemplateProcessing(
        single="<s> $A", special_tokens=[("<s>", 0)]
    )
    tokenizer.save(str(tmp_path / "tokenizer.json"))
    (tmp_path / "tokenizer_config.json").write_text(json.dumps({"pad_token": "<s>"}))
    inputs = ModelTokenizer(tmp_path).batch(["a", "a b"])
    assert inputs["input_ids"].tolist() == [[0, 1, 0], [0, 1, 2]]
    assert inputs["attention_mask"].tolist() == [[1, 1, 0], [1, 1, 1]]
    hidden = np.zeros((2, 3, 2), dtype=np.float32)
    hidden[:, 0] = [3, 4]
    np.testing.assert_allclose(cls_normalize(hidden, inputs["attention_mask"]), [[0.6, 0.8]] * 2)


@pytest.mark.parametrize(
    "hidden,mask",
    [
        (np.zeros((1, 0, 2)), np.zeros((1, 0))),
        (np.ones((1, 2)), np.ones((1, 2))),
        (np.ones((1, 2, 2)), np.zeros((1, 2))),
        (np.ones((1, 2, 2)), np.ones((1, 3))),
        (np.zeros((1, 2, 2)), np.ones((1, 2))),
        (np.array([[[float("nan"), 1]]]), np.ones((1, 1))),
        (np.array([[[1e38, 1e38]]], dtype=np.float32), np.ones((1, 1))),
    ],
)
def test_cls_invalid_shapes_masks_and_values_reject(hidden, mask):
    with np.errstate(over="ignore"), pytest.raises(UserError):
        cls_normalize(hidden, mask)


def test_dispatch_is_manifest_driven_and_legacy_output_unchanged():
    model = E5Encoder.__new__(E5Encoder)
    spec = model_spec("berta")
    hidden = np.array([[[1.2, 3.4], [9.1, -4.3]]], dtype=np.float32)
    mask = np.ones((1, 2), dtype=np.int64)
    model.spec = spec
    np.testing.assert_array_equal(model._pool(hidden, mask), masked_mean_normalize(hidden, mask))
    model.spec = replace(spec, manifest={**spec.manifest, "pooling": "cls"})
    np.testing.assert_array_equal(model._pool(hidden, mask), cls_normalize(hidden, mask))
    model.spec = replace(spec, manifest={**spec.manifest, "pooling": "unknown"})
    with pytest.raises(UserError):
        model._pool(hidden, mask)
