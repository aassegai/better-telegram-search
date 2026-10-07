"""DB text detection and CTC recognition for the pinned PaddleOCR RU/EN models.

Model provenance and licence are recorded in config/ocr_onnx_model.json.
No training framework, automatic model downloads or external code execution.
"""

import io
import math
import time

import cv2
import numpy as np
import pyclipper
from PIL import Image, ImageOps

from telegram_search.inference.providers import CPU, Execution
from telegram_search.inference.resources import memory_exhausted


def ordered_box(points):
    points = np.asarray(points, dtype=np.float32)
    points = points[np.argsort(points[:, 0])]
    left = points[:2][np.argsort(points[:2, 1])]
    right = points[2:][np.argsort(points[2:, 1])]
    return np.array([left[0], right[0], right[1], left[1]], dtype=np.float32)


def decode_ctc(probabilities, alphabet):
    if probabilities.ndim != 2 or probabilities.shape[1] != len(alphabet):
        raise ValueError("recognition contract")
    if not np.isfinite(probabilities).all():
        raise ValueError("recognition values")
    indices = probabilities.argmax(axis=1)
    scores = probabilities.max(axis=1)
    text, confidence = [], []
    previous = 0
    for index, score in zip(indices, scores, strict=True):
        if index and index != previous:
            text.append(alphabet[index])
            confidence.append(float(score))
        previous = index
    return "".join(text).strip(), float(np.mean(confidence)) if confidence else 0.0


def detected_boxes(scores, width, height):
    if scores.ndim != 2 or not np.isfinite(scores).all():
        raise ValueError("detection contract")
    bitmap = (scores > 0.3).astype(np.uint8)
    contours, _ = cv2.findContours(bitmap, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    boxes = []
    sh, sw = scores.shape
    for contour in contours[:1000]:
        rectangle = cv2.minAreaRect(contour)
        if min(rectangle[1]) < 3:
            continue
        points = ordered_box(cv2.boxPoints(rectangle))
        x0, y0 = np.floor(points.min(axis=0)).astype(int)
        x1, y1 = np.ceil(points.max(axis=0)).astype(int)
        x0, x1 = max(0, x0), min(sw - 1, x1)
        y0, y1 = max(0, y0), min(sh - 1, y1)
        mask = np.zeros((y1 - y0 + 1, x1 - x0 + 1), dtype=np.uint8)
        cv2.fillPoly(mask, [np.round(points - [x0, y0]).astype(np.int32)], 1)
        if cv2.mean(scores[y0 : y1 + 1, x0 : x1 + 1], mask)[0] < 0.6:
            continue
        area = abs(cv2.contourArea(points))
        perimeter = cv2.arcLength(points, True)
        if perimeter <= 0:
            continue
        offset = pyclipper.PyclipperOffset()
        offset.AddPath(
            np.round(points * 1024).astype(np.int64).tolist(),
            pyclipper.JT_ROUND,
            pyclipper.ET_CLOSEDPOLYGON,
        )
        expanded = offset.Execute(area * 1.5 / perimeter * 1024)
        if len(expanded) != 1:
            continue
        rectangle = cv2.minAreaRect(np.asarray(expanded[0], dtype=np.float32) / 1024)
        if min(rectangle[1]) < 5:
            continue
        box = ordered_box(cv2.boxPoints(rectangle))
        box *= [width / sw, height / sh]
        box[:, 0] = box[:, 0].clip(0, width - 1)
        box[:, 1] = box[:, 1].clip(0, height - 1)
        boxes.append(box)
    boxes.sort(key=lambda b: (float(b[0, 1]), float(b[0, 0])))
    # Correct small vertical jitter so words on one line retain left-to-right order.
    for i in range(1, len(boxes)):
        j = i
        while j > 0 and abs(boxes[j][0, 1] - boxes[j - 1][0, 1]) < 10:
            if boxes[j][0, 0] >= boxes[j - 1][0, 0]:
                break
            boxes[j], boxes[j - 1] = boxes[j - 1], boxes[j]
            j -= 1
    return boxes


def crop_dimensions(box):
    width = max(1, round(max(np.linalg.norm(box[0] - box[1]), np.linalg.norm(box[2] - box[3]))))
    height = max(1, round(max(np.linalg.norm(box[0] - box[3]), np.linalg.norm(box[1] - box[2]))))
    return width, height


def recognition_dimensions(width, height):
    resized = max(1, min(2048, math.ceil(48 * width / height)))
    return resized, min(2048, math.ceil(resized / 320) * 320)


def crop_box(image, box):
    width, height = crop_dimensions(box)
    transform = cv2.getPerspectiveTransform(
        box,
        np.array(
            [[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]], dtype=np.float32
        ),
    )
    crop = cv2.warpPerspective(image, transform, (width, height), borderMode=cv2.BORDER_REPLICATE)
    return np.rot90(crop) if height / width >= 1.5 else crop


class OnnxOcrPipeline:
    def __init__(self, root, *, max_edge, device, device_id, memory_limit_mib, threads):
        cv2.setNumThreads(threads)
        self.max_edge = max_edge
        self.execution = Execution(
            device,
            device_id=device_id,
            memory_limit_mib=memory_limit_mib,
            threads=threads,
            probe=False,
        )
        self.recognition_limits = {}
        self.alphabet = (
            [""]
            + (root / "languages/eslav/dict.txt").read_text(encoding="utf-8").splitlines()
            + [" "]
        )
        try:
            self._load(root)
        except Exception:
            if device != "auto":
                raise
            self.detector = self.recognizer = None
            self.execution.provider = CPU
            self._load(root)

    def _load(self, root):
        self.detector = self.execution.session(str(root / "detection/v3/det.onnx"))
        self.recognizer = self.execution.session(str(root / "languages/eslav/rec.onnx"))
        self._detect(np.zeros((1, 3, 32, 32), dtype=np.float32))
        # Empty images exercise detection only. Warm recognition as well so a
        # device cannot be marked ready with an unusable recognition graph.
        self._recognize_crops([np.full((48, 320, 3), 255, dtype=np.uint8)])

    def _detect(self, tensor):
        scores = self.detector.run(None, {self.detector.get_inputs()[0].name: tensor})[0]
        if scores.ndim != 4 or scores.shape[:2] != (1, 1) or not np.isfinite(scores).all():
            raise ValueError("detection output")
        return scores[0, 0]

    def recognize(self, data):
        began = time.perf_counter()
        with Image.open(io.BytesIO(data)) as original:
            if original.width * original.height > Image.MAX_IMAGE_PIXELS:
                raise ValueError("pixel budget")
            image = ImageOps.exif_transpose(original).convert("RGB")
            image.thumbnail((self.max_edge, self.max_edge), Image.Resampling.LANCZOS)
            image = np.asarray(image)[:, :, ::-1].copy()
        h, w = image.shape[:2]
        ratio = min(1.0, 960 / max(h, w))
        dh, dw = max(32, round(h * ratio / 32) * 32), max(32, round(w * ratio / 32) * 32)
        tensor = cv2.resize(image, (dw, dh)).astype(np.float32) / 255
        tensor = (tensor - np.array([0.485, 0.456, 0.406], dtype=np.float32)) / np.array(
            [0.229, 0.224, 0.225], dtype=np.float32
        )
        tensor = tensor.transpose(2, 0, 1)[None].copy()
        prepared = time.perf_counter()
        scores = self._detect(tensor)
        detected = time.perf_counter()
        boxes = detected_boxes(scores, w, h)
        postprocessed = time.perf_counter()
        texts, confidences = [], []
        text_size = 0
        for text, confidence in self._recognize_boxes(image, boxes):
            if text and confidence >= 0.5:
                texts.append(text)
                confidences.append(confidence)
                text_size += len(text) + 1
            if text_size > 65536:
                raise ValueError("text budget")
        self.last_timings = {
            "preprocess_seconds": prepared - began,
            "detection_seconds": detected - prepared,
            "boxes_seconds": postprocessed - detected,
            "recognition_seconds": time.perf_counter() - postprocessed,
            "regions": len(boxes),
        }
        return {
            "text": "\n".join(texts),
            "confidence": float(np.mean(confidences)) * 100 if confidences else 0.0,
            "provider": self.execution.provider,
        }

    def _recognize_boxes(self, image, boxes):
        if self.execution.provider == CPU:
            # Keep the smaller original CPU batches; larger homogeneous batches
            # help CUDA but regress this recognizer's CPU throughput.
            results = []
            for start in range(0, len(boxes), 8):
                crops = [crop_box(image, box) for box in boxes[start : start + 8]]
                results.extend(self._recognize_crops(crops))
            return results
        # Group the entire image before batching: neighbouring lines often have
        # different widths and split a batch of eight into several tiny GPU runs.
        # Keep fixed padding and restore detector order, preserving cache identity.
        groups = {}
        pixels = {}
        for index, box in enumerate(boxes):
            width, height = crop_dimensions(box)
            pixels[index] = width * height
            if height / width >= 1.5:
                width, height = height, width
            _, bucket = recognition_dimensions(width, height)
            groups.setdefault(bucket, []).append(index)
        results = [None] * len(boxes)
        limit = 32
        for bucket, indices in groups.items():
            start = 0
            while start < len(indices):
                batch_limit = min(limit, self.recognition_limits.get(bucket, limit))
                end, crop_pixels = start, 0
                while end < len(indices) and end - start < batch_limit:
                    next_pixels = pixels[indices[end]]
                    if end > start and crop_pixels + next_pixels > 8_000_000:
                        break
                    crop_pixels += next_pixels
                    end += 1
                selected = indices[start:end]
                crops = [crop_box(image, boxes[index]) for index in selected]
                values = self._recognize_crops(crops)
                for index, value in zip(selected, values, strict=True):
                    results[index] = value
                start = end
        return results

    def _recognize_crops(self, crops):
        dimensions = [recognition_dimensions(crop.shape[1], crop.shape[0]) for crop in crops]
        widths = [size[0] for size in dimensions]
        # A line's padding must not depend on its neighbours or on OOM splitting:
        # the bidirectional recognizer can otherwise return a different transcription.
        buckets = [size[1] for size in dimensions]
        if len(set(buckets)) > 1:
            results = [None] * len(crops)
            for bucket in sorted(set(buckets)):
                indices = [i for i, width in enumerate(buckets) if width == bucket]
                values = self._recognize_crops([crops[i] for i in indices])
                for index, value in zip(indices, values, strict=True):
                    results[index] = value
            return results
        padded = np.zeros((len(crops), 3, 48, buckets[0]), dtype=np.float32)
        for index, (crop, width) in enumerate(zip(crops, widths, strict=True)):
            tensor = cv2.resize(crop, (width, 48)).astype(np.float32) / 127.5 - 1
            padded[index, :, :, :width] = tensor.transpose(2, 0, 1)
        retry = False
        try:
            predictions = self.recognizer.run(None, {self.recognizer.get_inputs()[0].name: padded})[
                0
            ]
        except Exception as exc:
            if len(crops) <= 1 or not memory_exhausted(exc):
                raise
            limits = getattr(self, "recognition_limits", {})
            limits[buckets[0]] = min(limits.get(buckets[0], len(crops)), len(crops) // 2)
            self.recognition_limits = limits
            retry = True
        if retry:
            del padded
            middle = len(crops) // 2
            return self._recognize_crops(crops[:middle]) + self._recognize_crops(crops[middle:])
        if predictions.ndim != 3 or predictions.shape[0] != len(crops):
            raise ValueError("recognition output")
        return [decode_ctc(prediction, self.alphabet) for prediction in predictions]
