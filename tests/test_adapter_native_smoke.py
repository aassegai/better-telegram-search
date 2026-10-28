"""The frozen-smoke binding path also runs on public fixtures in source CI."""

import sys

import pytest


def test_native_smoke_exercises_all_current_role_bindings(tmp_path):
    pytest.importorskip("numpy")
    pytest.importorskip("onnxruntime")
    from telegram_search.distribution.smoke import _custom_adapter_check

    checks = _custom_adapter_check(tmp_path / "workspace", tmp_path, sys.executable)
    assert checks == {
        "custom_text_binding": True,
        "custom_image_binding": True,
        "custom_ocr_binding": True,
        "custom_reranker_binding": True,
    }
