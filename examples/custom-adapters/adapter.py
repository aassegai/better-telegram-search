"""Small deterministic protocol examples, not pretrained production models.

Replace the role computation with your local runtime. Keep IDs, float32/L2,
token-count/prefix semantics, progress, cancellation and output contracts intact.
"""

import hashlib
import math
import re


def words(text):
    return re.findall(r"\w+", text.casefold())


def unit(values):
    norm = math.hypot(*values)
    return [v / norm for v in values] if norm else [1.0, *([0.0] * (len(values) - 1))]


class Base:
    role = ""
    operations = []

    def info(self):
        return {
            "adapter_api_version": 1,
            "role": self.role,
            "devices": ["cpu"],
            "operations": self.operations,
        }

    def validate(self, context):
        if context.device != "cpu" or not context.offline:
            raise ValueError("These protocol examples require offline CPU execution.")

    def load(self, context):
        context.checkpoint()

    def unload(self, context):
        pass

    def close(self, context):
        pass


class TextEmbedding(Base):
    role = "text_embedding"
    operations = ["count_tokens", "encode_text"]

    def count_tokens(self, texts, context):
        # Includes two synthetic special tokens. Production adapters must use
        # their real tokenizer on the EXACT supplied, already-prefixed text.
        context.checkpoint()
        return [len(words(text)) + 2 for text in texts]

    def encode_text(self, items, operation, context):
        vectors = []
        for i, item in enumerate(items):
            context.checkpoint()
            values = [0.0] * 32
            for word in words(item["text"]):
                slot = int.from_bytes(hashlib.blake2b(word.encode(), digest_size=2).digest()) % 32
                values[slot] += 1
            vectors.append(unit(values))
            context.progress(i + 1, len(items))
        return {"ids": [item["id"] for item in items], "dtype": "float32", "vectors": vectors}


class ImageEmbedding(Base):
    role = "image_embedding"
    operations = ["encode_images", "encode_image_queries"]

    def encode_images(self, images, context):
        vectors = []
        for image in images:
            context.checkpoint()
            pixels = image["pixels"]  # Decoded RGB bytes, not a file path.
            vectors.append(unit([sum(pixels[channel::3]) for channel in range(3)]))
        return {"ids": [image["id"] for image in images], "dtype": "float32", "vectors": vectors}

    def encode_image_queries(self, items, context):
        colors = {
            "red": [1, 0, 0],
            "green": [0, 1, 0],
            "blue": [0, 0, 1],
            "красный": [1, 0, 0],
            "зелёный": [0, 1, 0],
            "синий": [0, 0, 1],
        }
        vectors = []
        for item in items:
            context.checkpoint()
            color = next((colors[w] for w in words(item["text"]) if w in colors), [1, 1, 1])
            vectors.append(unit(color))
        return {"ids": [item["id"] for item in items], "dtype": "float32", "vectors": vectors}


class Reranker(Base):
    role = "reranker"
    operations = ["rerank"]

    def rerank(self, query, candidates, context):
        query_words = set(words(query))
        result = []
        for candidate in candidates:
            context.checkpoint()
            overlap = len(query_words & set(words(candidate["text"])))
            result.append({"candidate_id": candidate["candidate_id"], "score": overlap})
        return result


class TinyDigitOCR(Base):
    role = "ocr"
    operations = ["recognize"]
    digits = {"111101101101111": "0", "010110010010111": "1", "111001111100111": "2"}

    def recognize(self, images, context):
        result = []
        for image in images:
            context.checkpoint()
            text = ""
            if (image["width"], image["height"]) == (3, 5):
                rgb = image["pixels"]
                pattern = "".join(
                    "1" if sum(rgb[i : i + 3]) < 384 else "0" for i in range(0, len(rgb), 3)
                )
                text = self.digits.get(pattern, "")
            regions = [{"text": text, "bbox": [0, 0, 3, 5], "confidence": 1.0}] if text else []
            result.append({"id": image["id"], "text": text, "regions": regions})
        return result


class SilenceASR(Base):
    role = "asr"
    operations = ["transcribe"]

    def transcribe(self, audio, language, context):
        context.checkpoint()
        # A working empty-audio boundary example. Do not invent transcripts:
        # replace this with a local speech model to handle actual speech.
        if any(abs(value) > 1e-6 for value in audio["pcm"]):
            raise ValueError("Replace SilenceASR with a local speech recognition runtime.")
        return {"id": audio["id"], "text": "", "language": "und", "segments": []}
