"""Create fresh, public demo packages with correctly pinned adapter source."""

import argparse
import hashlib
import json
from pathlib import Path


def create(output):
    source = Path(__file__).with_name("adapter.py").read_bytes()
    output.mkdir(parents=True, exist_ok=False)
    classes = {
        "text_embedding": "TextEmbedding",
        "image_embedding": "ImageEmbedding",
        "reranker": "Reranker",
        "ocr": "TinyDigitOCR",
        "asr": "SilenceASR",
    }
    for role, factory in classes.items():
        path = output / role
        path.mkdir()
        (path / "adapter.py").write_bytes(source)
        inputs, outputs, preprocessing = {"max_batch": 16}, {}, {"version": "demo-v1"}
        if role in {"text_embedding", "image_embedding"}:
            outputs.update(
                dimension=32 if role == "text_embedding" else 3,
                dtype="float32",
                metric="cosine",
                normalization="l2",
            )
        if role == "text_embedding":
            inputs.update(query_max_tokens=512, document_max_tokens=480)
            preprocessing.update(query_prefix="", document_prefix="")
        if role == "reranker":
            outputs["higher_is_better"] = True
        if role in {"ocr", "asr"}:
            outputs["confidence_range"] = [0, 1]
        if role == "ocr":
            outputs["bbox"] = "pixel-xyxy"
        if role == "asr":
            inputs["sample_rate"] = 16000
        manifest = {
            "schema_version": 1,
            "adapter_api_version": 1,
            "id": "example-" + role.replace("_", "-"),
            "role": role,
            "entrypoint": "adapter.py:" + factory,
            "revision": "demo-v1",
            "description": "Protocol demonstration only, not a pretrained model.",
            "supported_languages": ["ru", "en"],
            "runtime": {"name": "python", "version": "3"},
            "devices": ["cpu"],
            "input_contract": inputs,
            "output_contract": outputs,
            "preprocessing": preprocessing,
            "dependencies": {},
            "network": False,
            "files": [
                {
                    "path": "adapter.py",
                    "bytes": len(source),
                    "sha256": hashlib.sha256(source).hexdigest(),
                }
            ],
        }
        (path / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    create(parser.parse_args().output)
