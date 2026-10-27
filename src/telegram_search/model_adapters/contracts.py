"""Bounded JSON wire contracts shared by all custom-model roles.

No optional scientific runtime is imported into the application by this module.
"""

import base64
import binascii
import math
import struct
from array import array

from telegram_search.shared.errors import UserError

MAX_WIRE_BYTES = 64 * 1024**2
MAX_TEXT = 1024 * 1024
OPERATIONS = {
    "text_embedding": {"count_tokens", "encode_text"},
    "image_embedding": {"encode_images", "encode_image_queries"},
    "reranker": {"rerank"},
    "ocr": {"recognize"},
    "asr": {"transcribe"},
}


def invalid():
    raise UserError("Custom model input or output does not match its declared contract.")


def number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def string(value, maximum=MAX_TEXT, *, empty=True):
    return isinstance(value, str) and (empty or bool(value)) and len(value) <= maximum


def identifier(value):
    return string(value, 256, empty=False) and "\x00" not in value


def entries(items, maximum):
    if not isinstance(items, list) or not 1 <= len(items) <= maximum:
        invalid()
    if not all(isinstance(item, dict) and identifier(item.get("id")) for item in items):
        invalid()
    ids = [item["id"] for item in items]
    if len(ids) != len(set(ids)):
        invalid()
    return ids


def binary(value, size):
    if not isinstance(value, str) or len(value) != 4 * ((size + 2) // 3):
        invalid()
    try:
        result = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise UserError("Invalid binary input for custom model.") from exc
    if len(result) != size:
        invalid()
    return result


def image_pixels(image):
    if not isinstance(image, dict):
        invalid()
    w, h = image.get("width"), image.get("height")
    if type(w) is not int or type(h) is not int or not 1 <= w <= 16384 or not 1 <= h <= 16384:
        invalid()
    if w * h > 16 * 1024**2 or image.get("mode") != "RGB":
        invalid()
    return binary(image.get("pixels"), w * h * 3)


def audio_samples(audio):
    if not isinstance(audio, dict) or type(audio.get("sample_rate")) is not int:
        invalid()
    rate, samples = audio["sample_rate"], audio.get("samples")
    if not 8000 <= rate <= 48000 or type(samples) is not int or not 1 <= samples <= rate * 300:
        invalid()
    raw = binary(audio.get("pcm_f32le"), samples * 4)
    values = array("f")
    values.frombytes(raw)
    import sys

    if sys.byteorder != "little":
        values.byteswap()
    if any(not math.isfinite(value) or abs(value) > 1 for value in values):
        invalid()
    return values


def validate_request(manifest, operation, payload):
    definition = manifest.data
    if operation not in OPERATIONS[manifest.role] or not isinstance(payload, dict):
        invalid()
    maximum = definition["input_contract"]["max_batch"]
    if operation in {"encode_text", "encode_image_queries", "count_tokens"}:
        if operation == "count_tokens":
            texts = payload.get("texts")
            if (
                not isinstance(texts, list)
                or not 1 <= len(texts) <= maximum
                or not all(string(t) for t in texts)
            ):
                invalid()
        else:
            entries(payload.get("items"), maximum)
            if not all(string(item.get("text")) for item in payload["items"]):
                invalid()
        if operation == "encode_text" and payload.get("operation") not in {"query", "document"}:
            invalid()
    elif operation == "rerank":
        if not string(payload.get("query")):
            invalid()
        candidates = payload.get("candidates")
        if not isinstance(candidates, list) or not 1 <= len(candidates) <= maximum:
            invalid()
        if not all(
            isinstance(c, dict) and identifier(c.get("candidate_id")) and string(c.get("text"))
            for c in candidates
        ):
            invalid()
        if len({c["candidate_id"] for c in candidates}) != len(candidates):
            invalid()
    elif operation in {"encode_images", "recognize"}:
        entries(payload.get("images"), maximum)
        for image in payload["images"]:
            image_pixels(image)
    elif operation == "transcribe":
        audio = payload.get("audio")
        if not isinstance(audio, dict) or not identifier(audio.get("id")):
            invalid()
        audio_samples(audio)
        if audio["sample_rate"] != definition["input_contract"]["sample_rate"]:
            invalid()
        if payload.get("language") not in {"auto", *definition["supported_languages"]}:
            invalid()


def vectors(manifest, payload, result, key):
    if not isinstance(result, dict) or result.get("dtype") != "float32":
        invalid()
    ids = [item["id"] for item in payload[key]]
    if result.get("ids") != ids:
        invalid()
    rows = result.get("vectors")
    dimension = manifest.data["output_contract"]["dimension"]
    if not isinstance(rows, list) or len(rows) != len(ids):
        invalid()
    converted = []
    for row in rows:
        if not isinstance(row, list) or len(row) != dimension or not all(number(v) for v in row):
            invalid()
        try:
            # JSON numbers have no dtype: make the float32 boundary explicit.
            packed = struct.pack(f"<{dimension}f", *row)
            values = list(struct.unpack(f"<{dimension}f", packed))
        except (OverflowError, struct.error) as exc:
            raise UserError("Custom embedding cannot be represented as float32.") from exc
        norm = math.hypot(*values)
        if (
            not all(math.isfinite(v) for v in values)
            or not math.isfinite(norm)
            or abs(norm - 1) > 1e-3
        ):
            invalid()
        converted.append(values)
    return {"ids": ids, "dtype": "float32", "vectors": converted}


def confidence(item):
    if "confidence" in item and (
        not number(item["confidence"]) or not 0 <= item["confidence"] <= 1
    ):
        invalid()


def validate_result(manifest, operation, payload, result):
    if operation == "count_tokens":
        if (
            not isinstance(result, list)
            or len(result) != len(payload["texts"])
            or not all(type(v) is int and 0 <= v <= 10 * MAX_TEXT for v in result)
        ):
            invalid()
        return result
    if operation in {"encode_text", "encode_image_queries", "encode_images"}:
        return vectors(
            manifest, payload, result, "images" if operation == "encode_images" else "items"
        )
    if operation == "rerank":
        if not isinstance(result, list) or len(result) != len(payload["candidates"]):
            invalid()
        ids = [c["candidate_id"] for c in payload["candidates"]]
        if not all(
            isinstance(r, dict) and identifier(r.get("candidate_id")) and number(r.get("score"))
            for r in result
        ):
            invalid()
        if len({r["candidate_id"] for r in result}) != len(ids) or {
            r["candidate_id"] for r in result
        } != set(ids):
            invalid()
        by_id = {r["candidate_id"]: r["score"] for r in result}
        # Stable input order is the tie breaker, independent of adapter output order.
        return sorted(
            [{"candidate_id": cid, "score": by_id[cid]} for cid in ids], key=lambda r: -r["score"]
        )
    if operation == "recognize":
        if not isinstance(result, list) or len(result) != len(payload["images"]):
            invalid()
        for image, item in zip(payload["images"], result, strict=True):
            if (
                not isinstance(item, dict)
                or item.get("id") != image["id"]
                or not string(item.get("text"))
            ):
                invalid()
            confidence(item)
            regions = item.get("regions", [])
            if not isinstance(regions, list) or len(regions) > 10000:
                invalid()
            for region in regions:
                if not isinstance(region, dict) or not string(region.get("text")):
                    invalid()
                bbox = region.get("bbox")
                if not isinstance(bbox, list) or len(bbox) != 4 or not all(number(v) for v in bbox):
                    invalid()
                x1, y1, x2, y2 = bbox
                if not 0 <= x1 < x2 <= image["width"] or not 0 <= y1 < y2 <= image["height"]:
                    invalid()
                confidence(region)
        return result
    if operation == "transcribe":
        if (
            not isinstance(result, dict)
            or result.get("id") != payload["audio"]["id"]
            or not string(result.get("text"))
            or result.get("language") not in {"und", *manifest.data["supported_languages"]}
        ):
            invalid()
        confidence(result)
        segments = result.get("segments")
        if not isinstance(segments, list) or len(segments) > 10000:
            invalid()
        duration = payload["audio"]["samples"] / payload["audio"]["sample_rate"]
        previous = 0
        for segment in segments:
            if not isinstance(segment, dict) or not string(segment.get("text")):
                invalid()
            start, end = segment.get("start"), segment.get("end")
            if (
                not number(start)
                or not number(end)
                or not previous <= start <= end <= duration + 1e-6
            ):
                invalid()
            previous = end
            confidence(segment)
        return result
    invalid()
