import importlib.metadata
import importlib.util
import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

repo = Path(SPECPATH).parent
assets = Path(os.environ["BTS_BUNDLE_ASSETS"])
datas = [
    (str(repo / "frontend" / "dist"), "frontend"),
    (str(assets / "tessdata"), "tessdata"),
    (str(repo / "bundle" / "ocr-smoke.png"), "smoke"),
    (str(assets / "licenses"), "licenses"),
]
datas += collect_data_files("telegram_search", includes=["**/*.json", "**/*.sql"])
binaries = []
for package in ("onnxruntime", "pyarrow", "lancedb", "tesserocr"):
    binaries += collect_dynamic_libs(package)
for distribution in importlib.metadata.distributions():
    for file in distribution.files or []:
        if file.name == "METADATA" and file.parent.name.endswith(".dist-info"):
            datas.append((str(distribution.locate_file(file)), file.parent.name))
            break
hiddenimports = ["uvicorn.logging", "uvicorn.loops.asyncio", "uvicorn.protocols.http.h11_impl",
                 "uvicorn.lifespan.on", "tesserocr", "tokenizers", "safetensors.numpy"]
hiddenimports += collect_submodules("lancedb")
hiddenimports += collect_submodules("tesserocr")
datas += collect_data_files("tesserocr", include_py_files=True, includes=["cysignals/*-helper.py"])
hiddenimports += collect_submodules("pyarrow", filter=lambda name: ".tests" not in name)
if importlib.util.find_spec("cysignals") is not None:
    hiddenimports += collect_submodules("cysignals")
    datas += collect_data_files("cysignals", include_py_files=True, includes=["*-helper.py"])
a = Analysis(
    [str(repo / "bundle" / "entry.py")],
    # Package directories must not be import roots: Windows tesserocr contains
    # a same-named .pyd that otherwise shadows the package and loses its DLL path.
    pathex=[str(repo / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["torch", "torchvision", "torchaudio", "transformers", "sentence_transformers",
              "pytest", "IPython", "matplotlib", "tkinter"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True, name="telegram-search",
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
    console=True, argv_emulation=False,
    manifest=str(repo / "bundle/windows.manifest") if sys.platform == "win32" else None,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="BetterTelegramSearch")
if sys.platform == "darwin":
    app = BUNDLE(coll, name="Better Telegram Search.app",
                 bundle_identifier="local.bettertelegramsearch.app",
                 info_plist={"CFBundleShortVersionString": "0.1.0", "NSHighResolutionCapable": True})
