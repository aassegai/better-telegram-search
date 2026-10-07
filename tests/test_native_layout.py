import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "native_layout", Path(__file__).resolve().parents[1] / "bundle/native_layout.py"
)
layout = importlib.util.module_from_spec(spec)
spec.loader.exec_module(layout)


def test_cuda_dlls_are_collected_once_without_changing_other_binaries():
    source = r"C:\env\nvidia\cublas\bin\cublas64_12.dll"
    entries = [
        ("cublas64_12.dll", source, "BINARY"),
        (r"nvidia\cublas\bin\cublas64_12.dll", source.upper(), "BINARY"),
        ("numpy.libs/other.dll", "numpy/other.dll", "BINARY"),
    ]
    assert layout.windows_nvidia_binaries(entries, {"cublas64_12.dll"}) == [entries[0], entries[2]]
    assert layout.windows_nvidia_binaries(entries, set()) == entries


def test_namespace_only_cuda_library_moves_to_runtime_loader_directory():
    entries = [(r"nvidia\cudnn\bin\cudnn64_9.dll", r"C:\env\cudnn64_9.dll", "BINARY")]
    assert layout.windows_nvidia_binaries(entries, {"cudnn64_9.dll"}) == [
        ("cudnn64_9.dll", entries[0][1], "BINARY")
    ]


def test_image_only_bundle_keeps_cv2_and_drops_only_optional_windows_video_plugin():
    entries = [
        (r"cv2\cv2.pyd", "cv2.pyd", "EXTENSION"),
        (r"cv2\opencv_videoio_ffmpeg4140_64.dll", "video.dll", "BINARY"),
        ("other/ffmpeg.dll", "ffmpeg.dll", "BINARY"),
        ("cv2/LICENSE-ffmpeg.txt", "license.txt", "DATA"),
    ]
    assert layout.windows_image_binaries(entries) == [entries[0], entries[2], entries[3]]


def test_conflicting_cuda_library_sources_fail_instead_of_shadowing_each_other():
    with pytest.raises(RuntimeError, match="Conflicting NVIDIA DLL"):
        layout.windows_nvidia_binaries(
            [
                ("cublas64_12.dll", r"C:\first\cublas64_12.dll", "BINARY"),
                ("nvidia/cublas64_12.dll", r"C:\second\cublas64_12.dll", "BINARY"),
            ],
            {"cublas64_12.dll"},
        )
