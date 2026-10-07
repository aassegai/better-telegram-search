"""Internal isolated OCR entry point. No source file names or recognized text in logs."""

import io
import json
import sys

from PIL import Image, ImageOps

from telegram_search.inference.ocr_process import MAX_IMAGE_BYTES


def recognize(data, max_edge, api):
    with Image.open(io.BytesIO(data)) as original:
        if original.width * original.height > Image.MAX_IMAGE_PIXELS:
            raise ValueError("pixel budget")
        image = ImageOps.exif_transpose(original).convert("RGB")
        image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
        image = ImageOps.autocontrast(image.convert("L"))
    api.Clear()
    api.ClearAdaptiveClassifier()
    api.SetImage(image)
    text = api.GetUTF8Text().strip()
    if len(text) > 65536:
        raise ValueError("output budget")
    return {"text": text, "confidence": float(api.MeanTextConf())}


def serve(api, max_edge, *, onnx=False):
    while header := sys.stdin.buffer.readline(16):
        if not header.endswith(b"\n") or not header[:-1].isdigit():
            raise ValueError("frame header")
        length = int(header[:-1])
        if not 0 < length <= MAX_IMAGE_BYTES:
            raise ValueError("frame size")
        data = sys.stdin.buffer.read(length)
        if len(data) != length:
            raise ValueError("truncated image")
        try:
            value = api.recognize(data) if onnx else recognize(data, max_edge, api)
        except Exception:
            # Never emit file names, recognized content, or native tracebacks.
            value = {"error": True}
        print(json.dumps(value, ensure_ascii=True), flush=True)


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if args and args[0] == "--onnx":
        from pathlib import Path

        from telegram_search.inference.ocr_pipeline import OnnxOcrPipeline

        if len(args) != 8 or args[-1] != "--server":
            raise ValueError("worker arguments")
        Image.MAX_IMAGE_PIXELS = 25_000_000
        engine = OnnxOcrPipeline(
            Path(args[1]),
            max_edge=int(args[2]),
            device=args[3],
            device_id=int(args[4]),
            memory_limit_mib=int(args[5]),
            threads=int(args[6]),
        )
        serve(engine, int(args[2]), onnx=True)
        return
    import tesserocr

    if args == ["--runtime"]:
        print(tesserocr.tesseract_version())
        return
    if len(args) not in {2, 3} or (len(args) == 3 and args[2] != "--server"):
        raise ValueError("worker arguments")
    Image.MAX_IMAGE_PIXELS = 25_000_000
    with tesserocr.PyTessBaseAPI(path=args[0], lang="rus+eng", psm=tesserocr.PSM.AUTO) as api:
        if len(args) == 3:
            serve(api, int(args[1]))
        else:
            data = sys.stdin.buffer.read(MAX_IMAGE_BYTES + 1)
            if len(data) > MAX_IMAGE_BYTES:
                raise ValueError("image size")
            print(json.dumps(recognize(data, int(args[1]), api), ensure_ascii=True))


if __name__ == "__main__":
    main()
