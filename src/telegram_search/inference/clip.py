import gc
import hashlib
import importlib.metadata
import io
import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

from telegram_search.config.model_registry import media_registry
from telegram_search.inference.bundles import BundleStore
from telegram_search.inference.compatibility import RUNTIME_VERSIONS, check_vectors
from telegram_search.inference.providers import CPU, Execution, runtime_version
from telegram_search.inference.resources import compute_lock
from telegram_search.inference.tokenization import ModelTokenizer
from telegram_search.shared.errors import UserError
from telegram_search.shared.text import serialize


def normalize(values):
    norms = np.linalg.norm(values, axis=1, keepdims=True)
    if not np.isfinite(values).all() or np.any(norms <= 0):
        raise UserError("CLIP вернул недопустимый embedding.")
    return np.ascontiguousarray(values / norms, dtype=np.float32)


def image_tensor(data: bytes, config: dict):
    if len(data) > 32 * 1024**2:
        raise UserError("Фотография больше 32 МиБ.")
    try:
        with Image.open(io.BytesIO(data)) as original:
            if original.width * original.height > 25_000_000:
                raise ValueError("pixel budget")
            image = ImageOps.exif_transpose(original).convert("RGB")
            width, height = image.size
            size = config["size"]["shortest_edge"]
            if width < height:
                resized = (size, int(size * height / width))
            else:
                resized = (int(size * width / height), size)
            if resized[0] * resized[1] > 8_000_000 or max(resized) > 32768:
                raise ValueError("intermediate resize budget")
            image = image.resize(resized, Image.Resampling.BICUBIC)
            crop = config["crop_size"]
            left = (image.width - crop["width"]) // 2
            top = (image.height - crop["height"]) // 2
            image = image.crop((left, top, left + crop["width"], top + crop["height"]))
            pixels = np.asarray(image, dtype=np.float32) * np.float32(config["rescale_factor"])
        pixels = (pixels - np.asarray(config["image_mean"], dtype=np.float32)) / np.asarray(
            config["image_std"], dtype=np.float32
        )
        return np.ascontiguousarray(pixels.transpose(2, 0, 1), dtype=np.float32)
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise UserError("Не удалось безопасно прочитать фотографию.") from exc


class ClipEncoder:
    """Multilingual DistilBERT + learned projection paired with original CLIP ViT-B/32."""

    def __init__(
        self,
        workspace: Path,
        *,
        threads=4,
        device="cpu",
        search_device=None,
        gpu_device_id=0,
        gpu_memory_limit_mib=4096,
    ):
        from safetensors.numpy import load_file

        self.execution = Execution(
            device,
            device_id=gpu_device_id,
            memory_limit_mib=gpu_memory_limit_mib,
            threads=threads,
            probe=False,
        )
        self.query_execution = Execution(
            search_device or device,
            device_id=gpu_device_id,
            memory_limit_mib=gpu_memory_limit_mib,
            threads=threads,
            probe=False,
        )
        self.specs = media_registry()
        store = BundleStore(workspace)
        self.paths = {key: store.verify(spec) for key, spec in self.specs.items()}
        self.tokenizer = ModelTokenizer(self.paths["clip_text"])
        self.projection = load_file(self.paths["clip_text"] / "2_Dense/model.safetensors")[
            "linear.weight"
        ]
        if self.projection.shape != (512, 768):
            raise UserError("Learned projection CLIP не соответствует manifest.")
        self.preprocessor = json.loads(
            (self.paths["clip_image"] / "preprocessor_config.json").read_text()
        )
        self.threads = threads
        self.sessions = {}
        self.validated = set()
        self.space_manifest = {
            "models": {key: spec.manifest for key, spec in self.specs.items()},
            "runtime": runtime_version(),
            "tokenizers": importlib.metadata.version("tokenizers"),
            "pillow": importlib.metadata.version("pillow"),
            "provider": CPU,
            "dtype": "float32",
            "preprocessing": "clip-pil-bicubic-exif-mean-projection-l2-v1",
        }
        self.space_id = hashlib.sha256(serialize(self.space_manifest).encode()).hexdigest()

    def compatible_spaces(self):
        versions = (
            RUNTIME_VERSIONS if runtime_version() in RUNTIME_VERSIONS else {runtime_version()}
        )
        return {
            hashlib.sha256(
                serialize({**self.space_manifest, "runtime": version}).encode()
            ).hexdigest()
            for version in versions
        }

    def adopt_space(self, space_id):
        if space_id not in self.compatible_spaces():
            raise UserError("Модель или обработка изображений отличается от закреплённого индекса.")
        self.space_id = space_id

    def _session(self, kind):
        if kind in self.sessions:
            return self.sessions[kind]
        execution = self.query_execution if kind == "text" else self.execution
        execution.threads = self.threads
        key = "clip_" + kind
        graph = "onnx/vision_model.onnx" if kind == "image" else "onnx/model.onnx"
        try:
            session = execution.session(str(self.paths[key] / graph))
            if execution.provider != CPU and (kind, execution.provider) not in self.validated:
                self._check_precision(kind, session, str(self.paths[key] / graph))
                self.validated.add((kind, execution.provider))
        except UserError:
            if execution.device != "auto":
                raise
            execution.provider = CPU
            execution.warning = "GPU недоступен: режим Авто использует CPU."
            session = execution.session(str(self.paths[key] / graph))
        inputs = {item.name: item.type for item in session.get_inputs()}
        output = "image_embeds" if kind == "image" else "last_hidden_state"
        expected = (
            {"pixel_values": "tensor(float)"}
            if kind == "image"
            else {"input_ids": "tensor(int64)", "attention_mask": "tensor(int64)"}
        )
        if inputs != expected or output not in {item.name for item in session.get_outputs()}:
            raise UserError("Контракт CLIP ONNX не совпадает с закреплённой парой.")
        self.sessions[kind] = session
        return session

    def _check_precision(self, kind, candidate, path):
        reference = Execution("cpu", threads=self.threads).session(path)
        if kind == "image":
            size = self.preprocessor["crop_size"]
            feed = {
                "pixel_values": np.stack(
                    [
                        np.full((3, size["height"], size["width"]), value, dtype=np.float32)
                        for value in (-1.0, 0.0, 1.0)
                    ]
                )
            }
            check_vectors(
                normalize(reference.run(["image_embeds"], feed)[0]),
                normalize(candidate.run(["image_embeds"], feed)[0]),
            )
        else:
            inputs = self.tokenizer.batch(
                [
                    "Photos from a summer trip.",
                    "Фотографии путешествия и встречи.",
                    "A photograph of the project schedule. " * 8,
                ],
                limit=128,
            )
            feed = {key: inputs[key] for key in ("input_ids", "attention_mask")}
            mask = inputs["attention_mask"][..., None].astype(np.float32)

            def encode(session):
                hidden = session.run(["last_hidden_state"], feed)[0]
                mean = (hidden * mask).sum(axis=1) / mask.sum(axis=1).clip(min=1)
                return normalize(np.einsum("bi,oi->bo", mean, self.projection, optimize=False))

            check_vectors(encode(reference), encode(candidate))

    def check_contract(self):
        with compute_lock:
            self._session("image")
            self._session("text")
        self.unload()

    def encode_text(self, texts):
        if not 1 <= len(texts) <= 32:
            raise UserError("Батч CLIP должен содержать от 1 до 32 текстов.")
        inputs = self.tokenizer.batch(texts, limit=128)
        with compute_lock.slot(interactive=True):
            session = self._session("text")
            hidden = session.run(
                ["last_hidden_state"], {key: inputs[key] for key in ("input_ids", "attention_mask")}
            )[0]
        mask = inputs["attention_mask"][..., None].astype(np.float32)
        mean = (hidden * mask).sum(axis=1) / mask.sum(axis=1).clip(min=1)
        result = normalize(np.einsum("bi,oi->bo", mean, self.projection, optimize=False))
        if result.shape != (len(texts), 512):
            raise UserError("Размерность CLIP text embedding не соответствует паре.")
        return result

    def encode_images(self, images):
        if not 1 <= len(images) <= 8:
            raise UserError("Батч изображений должен содержать от 1 до 8 файлов.")
        pixels = np.stack([image_tensor(data, self.preprocessor) for data in images])
        with compute_lock:
            result = normalize(
                self._session("image").run(["image_embeds"], {"pixel_values": pixels})[0]
            )
        if result.shape != (len(images), 512):
            raise UserError("Размерность CLIP image embedding не соответствует паре.")
        return result

    def unload(self):
        with compute_lock:
            self.sessions.clear()
        gc.collect()

    def unload_index(self):
        with compute_lock:
            self.sessions.pop("image", None)
        gc.collect()
