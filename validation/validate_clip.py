"""Compare final ONNX CLIP embeddings and RU text/image ranking with pinned CPU references."""

import argparse
import io
import json
import time
from pathlib import Path

import numpy as np
import psutil
import torch
from PIL import Image, ImageDraw
from transformers import AutoModel, CLIPImageProcessor, CLIPVisionModelWithProjection

from telegram_search.inference.clip import ClipEncoder, image_tensor
from telegram_search.inference.tokenization import ModelTokenizer


def images():
    values = []
    for color, shape in (
        ("red", "circle"),
        ("blue", "square"),
        ("green", "triangle"),
        ("yellow", "circle"),
        ("black", "square"),
        ("purple", "triangle"),
    ):
        image = Image.new("RGB", (320, 256), "white")
        draw = ImageDraw.Draw(image)
        if shape == "circle":
            draw.ellipse((80, 48, 240, 208), fill=color)
        elif shape == "square":
            draw.rectangle((80, 48, 240, 208), fill=color)
        else:
            draw.polygon([(160, 32), (64, 224), (256, 224)], fill=color)
        stream = io.BytesIO()
        image.save(stream, format="PNG")
        values.append(stream.getvalue())
    return values


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    torch.set_num_threads(4)
    encoder = ClipEncoder(args.workspace)
    cache = args.workspace / "models" / "reference"
    cache.mkdir(parents=True, exist_ok=True)
    text_spec = encoder.specs["clip_text"]
    print("Loading pinned CPU text reference", flush=True)
    text_model = AutoModel.from_pretrained(
        text_spec.model_id, revision=text_spec.revision, cache_dir=cache, token=False
    )
    text_model.eval()
    texts = [
        "Красный круг на белом фоне",
        "Синий квадрат",
        "Зелёный треугольник",
        "Жёлтый круг",
        "Чёрный квадрат",
        "Фиолетовый треугольник",
        "a red circle on a white background",
        "фото кота на диване",
        "東京 🙂",
    ]
    tokenizer = ModelTokenizer(encoder.paths["clip_text"])
    inputs = tokenizer.batch(texts, 128)
    with torch.inference_mode():
        hidden = text_model(
            input_ids=torch.from_numpy(inputs["input_ids"]),
            attention_mask=torch.from_numpy(inputs["attention_mask"]),
        ).last_hidden_state
        mask = torch.from_numpy(inputs["attention_mask"]).unsqueeze(-1).float()
        mean = (hidden * mask).sum(1) / mask.sum(1).clamp(min=1)
        reference_text = torch.nn.functional.normalize(
            mean @ torch.from_numpy(encoder.projection).T, dim=1
        ).numpy()
    del text_model
    actual_text = encoder.encode_text(texts)
    encoder.unload()
    print("Loading pinned CPU image reference", flush=True)
    image_revision = "e6a30b603a447e251fdaca1c3056b2a16cdfebeb"
    image_model = CLIPVisionModelWithProjection.from_pretrained(
        "openai/clip-vit-base-patch32", revision=image_revision, cache_dir=cache, token=False
    ).eval()
    processor = CLIPImageProcessor.from_pretrained(
        "openai/clip-vit-base-patch32", revision=image_revision, cache_dir=cache, token=False
    )
    blobs = images()
    pil = [Image.open(io.BytesIO(blob)) for blob in blobs]
    native_pixels = processor(images=pil, return_tensors="np")["pixel_values"]
    ours = np.stack([image_tensor(blob, encoder.preprocessor) for blob in blobs])
    with torch.inference_mode():
        reference_images = torch.nn.functional.normalize(
            image_model(pixel_values=torch.from_numpy(native_pixels)).image_embeds, dim=1
        ).numpy()
    del image_model
    began = time.perf_counter()
    actual_images = np.concatenate(
        [encoder.encode_images(blobs[i : i + 2]) for i in range(0, len(blobs), 2)]
    )
    elapsed = time.perf_counter() - began
    ref_rank = np.argsort(-(reference_text @ reference_images.T), axis=1)
    actual_rank = np.argsort(-(actual_text @ actual_images.T), axis=1)
    report = {
        "device": "cpu",
        "provider": "CPUExecutionProvider",
        "text_revision": text_spec.revision,
        "image_reference_revision": image_revision,
        "image_onnx_revision": encoder.specs["clip_image"].revision,
        "synthetic_images": len(blobs),
        "synthetic_queries": len(texts),
        "dimension": 512,
        "preprocessing_max_absolute_error": float(np.max(np.abs(native_pixels - ours))),
        "text_max_absolute_error": float(np.max(np.abs(reference_text - actual_text))),
        "image_max_absolute_error": float(np.max(np.abs(reference_images - actual_images))),
        "text_min_cosine": float(np.min(np.sum(reference_text * actual_text, axis=1))),
        "image_min_cosine": float(np.min(np.sum(reference_images * actual_images, axis=1))),
        "top3_overlap": float(
            np.mean(
                [
                    len(set(a[:3]) & set(b[:3])) / 3
                    for a, b in zip(ref_rank, actual_rank, strict=True)
                ]
            )
        ),
        "ru_shape_top1_accuracy": float(np.mean(actual_rank[:6, 0] == np.arange(6))),
        "image_batch2_seconds_including_load": elapsed,
        "rss_bytes": psutil.Process().memory_info().rss,
        "scope": (
            "Procedural shapes, colors, bilingual and Unicode queries; "
            "numerical compatibility, not a real-photo quality claim."
        ),
    }
    assert report["preprocessing_max_absolute_error"] < 1e-6
    assert report["text_min_cosine"] > 0.9999 and report["image_min_cosine"] > 0.9999
    assert report["top3_overlap"] == 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    main()
