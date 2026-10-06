"""Internal isolated OCR entry point. No source file names or recognized text in logs."""

import io
import json
import sys

from PIL import Image, ImageOps


def main():
    import tesserocr

    Image.MAX_IMAGE_PIXELS = 25_000_000
    data = sys.stdin.buffer.read(32 * 1024 * 1024 + 1)
    if len(data) > 32 * 1024 * 1024:
        raise ValueError("image size")
    with Image.open(io.BytesIO(data)) as original:
        if original.width * original.height > Image.MAX_IMAGE_PIXELS:
            raise ValueError("pixel budget")
        image = ImageOps.exif_transpose(original).convert("RGB")
        image.thumbnail((int(sys.argv[2]), int(sys.argv[2])), Image.Resampling.LANCZOS)
        image = ImageOps.autocontrast(image.convert("L"))
    with tesserocr.PyTessBaseAPI(path=sys.argv[1], lang="rus+eng", psm=tesserocr.PSM.AUTO) as api:
        api.SetImage(image)
        text = api.GetUTF8Text().strip()
        if len(text) > 65536:
            raise ValueError("output budget")
        print(
            json.dumps({"text": text, "confidence": float(api.MeanTextConf())}, ensure_ascii=False)
        )


if __name__ == "__main__":
    main()
