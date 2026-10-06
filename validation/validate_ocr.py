"""Actual CPU Tesseract verification using synthetic RU/EN screenshots only."""

import argparse
import io
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from telegram_search.inference.ocr import OcrEngine


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument(
        "--font", type=Path, default=Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    engine = OcrEngine(args.workspace)
    engine.verify()
    cases = [
        "Номер заказа 123456. Стоимость ремонта 7800 рублей.",
        "Invoice 987654. Delivery tomorrow. Total 4200 USD.",
        "Встреча 15:30. Office 204. Телефон 1234567890.",
    ]
    required = [
        ("123456", "7800", "ремонта"),
        ("987654", "4200", "Delivery"),
        ("15:30", "204", "1234567890"),
    ]
    passed = 0
    for text, words in zip(cases, required, strict=True):
        image = Image.new("RGB", (1500, 220), "white")
        ImageDraw.Draw(image).text(
            (40, 70), text, font=ImageFont.truetype(str(args.font), 36), fill="black"
        )
        stream = io.BytesIO()
        image.save(stream, format="PNG")
        result = engine.recognize(stream.getvalue())
        assert all(word in result["text"] for word in words), "Synthetic OCR verification failed"
        passed += 1
    report = {
        "device": "cpu",
        "provider": "tesserocr",
        "languages": "rus+eng",
        "synthetic_screenshots": len(cases),
        "passed": passed,
        "ocr_cache_version": engine.version,
        "dictionary_revision": engine.manifest["revision"],
        "scope": (
            "Clear procedural screenshots with Cyrillic, Latin, numbers and punctuation; "
            "not a noisy-photo quality claim."
        ),
    }
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
