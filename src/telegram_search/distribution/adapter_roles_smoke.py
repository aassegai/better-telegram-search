"""Exercise real adapter bindings in the extracted app, using public tiny fixtures."""

import hashlib
import io
import json
from dataclasses import replace

from PIL import Image

from telegram_search.config.settings import Settings
from telegram_search.model_adapters.image_encoder import AdapterImageEncoder
from telegram_search.model_adapters.ocr import AdapterOcrEngine
from telegram_search.model_adapters.registry import custom_spec
from telegram_search.model_adapters.reranker import AdapterReranker
from telegram_search.model_adapters.store import AdapterStore
from telegram_search.model_adapters.text_encoder import AdapterTextEncoder
from telegram_search.model_adapters.validation import validate_adapter

CODE = """
class Base:
    def info(self):
        return {'adapter_api_version':1, 'role':self.role, 'devices':['cpu'],
                'operations':self.operations}
    def validate(self, context):
        if context.device != 'cpu' or not context.offline:
            raise ValueError('This public smoke fixture requires offline CPU.')
    def load(self, context): context.checkpoint()
    def unload(self, context): pass
    def close(self, context): pass
class Visual(Base):
    role = 'image_embedding'
    operations = ['encode_images', 'encode_image_queries']
    def encode_images(self, images, context):
        return {'ids':[i['id'] for i in images], 'dtype':'float32',
                'vectors':[[1,0] if i['pixels'][0]>i['pixels'][2] else [0,1] for i in images]}
    def encode_image_queries(self, items, context):
        return {'ids':[i['id'] for i in items], 'dtype':'float32',
                'vectors':[[1,0] if 'red' in i['text'] else [0,1] for i in items]}
class Ocr(Base):
    role = 'ocr'
    operations = ['recognize']
    def recognize(self, images, context):
        return [{'id':i['id'], 'text':'red 12345' if i['pixels'][0]>i['pixels'][2]
                 else 'blue 67890', 'confidence':0.9} for i in images]
class Ranker(Base):
    role = 'reranker'
    operations = ['rerank']
    def rerank(self, query, candidates, context):
        return [{'candidate_id':i['candidate_id'],
                 'score':len(set(query.split()) & set(i['text'].split()))} for i in candidates]
"""


def _register(workspace, root, python, role):
    source = root.resolve() / ("Bound adapter Тест 中文 " + role)
    source.mkdir()
    raw = CODE.encode("utf-8")
    (source / "adapter.py").write_bytes(raw)
    classes = {"image_embedding": "Visual", "ocr": "Ocr", "reranker": "Ranker"}
    output = (
        {"dimension": 2, "dtype": "float32", "normalization": "l2", "metric": "cosine"}
        if role == "image_embedding"
        else {"confidence_range": [0, 1], "bbox": "pixel-xyxy"}
        if role == "ocr"
        else {"higher_is_better": True}
    )
    manifest = {
        "schema_version": 1,
        "adapter_api_version": 1,
        "id": "native-bound-" + role.replace("_", "-"),
        "role": role,
        "entrypoint": "adapter.py:" + classes[role],
        "revision": "v1",
        "supported_languages": ["ru", "en"],
        "devices": ["cpu"],
        "runtime": {"name": "python", "version": "3"},
        "dependencies": {},
        "input_contract": {"max_batch": 4},
        "output_contract": output,
        "preprocessing": {"version": "v1"},
        "network": False,
        "files": [
            {"path": "adapter.py", "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
        ],
    }
    path = source / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    result = validate_adapter(AdapterStore(workspace), path, python, register=True)
    return custom_spec(workspace, "custom:" + result["identity"], role=role)


def check_bound_roles(workspace, root, python, text_profile):
    """No downloads, GPU or private archives; these are binding checks, not quality tests."""
    import numpy as np

    settings = replace(Settings(), device="cpu", search_device="cpu", cpu_threads=1)
    encoder = AdapterTextEncoder(custom_spec(workspace, text_profile), workspace, settings)
    try:
        if encoder.tokenizer.count("public fixture") != 4:
            raise RuntimeError("Native custom tokenizer binding")
        for purpose in ("query", "passage"):
            np.testing.assert_allclose(
                encoder.encode_text(["public fixture"], purpose), [[0.6, 0.8]]
            )
    finally:
        encoder.unload()

    images = []
    for color in ("red", "blue"):
        stream = io.BytesIO()
        Image.new("RGB", (12, 8), color).save(stream, format="PNG")
        images.append(stream.getvalue())
    visual = AdapterImageEncoder(
        _register(workspace, root, python, "image_embedding"), workspace, settings
    )
    try:
        visual.check_contract()
        np.testing.assert_array_equal(visual.encode_images(images), [[1, 0], [0, 1]])
        np.testing.assert_array_equal(visual.encode_text(["red", "blue"]), [[1, 0], [0, 1]])
        processes = [worker.process for worker in visual.sessions.values()]
    finally:
        visual.unload()
    if any(process.poll() is None for process in processes):
        raise RuntimeError("Native custom image worker cleanup")

    ocr = AdapterOcrEngine(_register(workspace, root, python, "ocr"), workspace, settings)
    try:
        ocr.prepare(offline=True)
        values = ocr.recognize_many(images)
        if [v["text"] for v in values] != ["red 12345", "blue 67890"]:
            raise RuntimeError("Native custom OCR binding")
        if any(v["confidence"] != 90 for v in values):
            raise RuntimeError("Native custom OCR confidence mapping")
        process = ocr.sessions["image"].process
    finally:
        ocr.unload()
    if process.poll() is None:
        raise RuntimeError("Native custom OCR worker cleanup")

    reranker = AdapterReranker(_register(workspace, root, python, "reranker"), workspace, settings)
    try:
        rows = reranker.score_pairs(
            "ticket", [{"candidate_id": "a", "text": "ticket"}, {"candidate_id": "b", "text": "x"}]
        )
        if [(row["candidate_id"], row["score"]) for row in rows] != [("a", 1), ("b", 0)]:
            raise RuntimeError("Native custom reranker binding")
        process = reranker.session.process
    finally:
        reranker.unload()
    if process.poll() is None:
        raise RuntimeError("Native custom reranker worker cleanup")
    return {"custom_" + role + "_binding": True for role in ("text", "image", "ocr", "reranker")}
