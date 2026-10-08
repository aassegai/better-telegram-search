"""Pinned SigLIP 2 pair with the same lifecycle as the legacy visual adapter."""

import hashlib
import importlib.metadata
import io
import threading
import time

import numpy as np
from PIL import Image, ImageOps

from telegram_search.config.model_registry import visual_specs
from telegram_search.inference.bundles import BundleStore
from telegram_search.inference.clip import ClipEncoder, normalize
from telegram_search.inference.compatibility import check_vectors
from telegram_search.inference.providers import CPU, Execution, runtime_version
from telegram_search.inference.resources import compute_gate
from telegram_search.inference.tokenization import ModelTokenizer
from telegram_search.shared.errors import UserError
from telegram_search.shared.text import serialize


def image_tensor(data):
    if len(data) > 32 * 1024**2:
        raise UserError("Фотография больше 32 МиБ.")
    try:
        with Image.open(io.BytesIO(data)) as original:
            if original.width * original.height > 25_000_000:
                raise ValueError("pixel budget")
            image = ImageOps.exif_transpose(original).convert("RGB")
            image = image.resize((224, 224), Image.Resampling.BILINEAR)
            pixels = np.asarray(image, dtype=np.float32) * np.float32(1 / 255)
        pixels = (pixels - np.float32(0.5)) / np.float32(0.5)
        return np.ascontiguousarray(pixels.transpose(2, 0, 1))
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise UserError("Не удалось безопасно прочитать фотографию.") from exc


class SiglipEncoder(ClipEncoder):
    profile = "siglip2"
    dimension = 768

    def __init__(
        self,
        workspace,
        *,
        threads=4,
        device="cpu",
        search_device=None,
        gpu_device_id=0,
        gpu_memory_limit_mib=4096,
    ):
        options = dict(
            device_id=gpu_device_id,
            memory_limit_mib=gpu_memory_limit_mib,
            threads=threads,
            probe=False,
        )
        self.execution = Execution(device, **options)
        self.query_execution = Execution(search_device or device, **options)
        self.threads = threads
        self.lock = threading.RLock()
        self.last_used = time.monotonic()
        self.sessions, self.validated = {}, set()
        self.spec = visual_specs("siglip2")["siglip2"]
        self.path = BundleStore(workspace).verify(self.spec)
        self.tokenizer = ModelTokenizer(self.path)
        if self.tokenizer.pad_id != 0:
            raise UserError("SigLIP 2 требует закреплённые PAD/EOS токены.")
        self.space_manifest = {
            "models": {"siglip2": self.spec.manifest},
            "runtime": runtime_version(),
            "tokenizers": importlib.metadata.version("tokenizers"),
            "pillow": importlib.metadata.version("pillow"),
            "provider": CPU,
            "dtype": "mixed-fp16",
            "preprocessing": self.spec.manifest["preprocessing_version"],
        }
        self.space_id = hashlib.sha256(serialize(self.space_manifest).encode()).hexdigest()

    def compatible_spaces(self):
        versions = self.spec.manifest["validated_runtime_versions"]
        if runtime_version() not in versions:
            versions = [runtime_version()]
        return {
            hashlib.sha256(serialize({**self.space_manifest, "runtime": v}).encode()).hexdigest()
            for v in versions
        }

    def _feed(self, kind):
        if kind == "image":
            return {
                "pixel_values": np.stack(
                    [np.full((3, 224, 224), value, dtype=np.float32) for value in (-1, 0, 1)]
                )
            }
        batch = self.tokenizer.batch(
            ["Photos from a summer trip.", "Фотография кота в саду.", "A diagram of a meeting."],
            limit=64,
            fixed_length=64,
        )
        return {"input_ids": batch["input_ids"]}

    def _session(self, kind):
        if kind in self.sessions:
            return self.sessions[kind]
        execution = self.query_execution if kind == "text" else self.execution
        execution.threads = self.threads
        name = "text" if kind == "text" else "vision"
        path = str(self.path / f"onnx/{name}_model_fp16.onnx")
        try:
            optimizers = self.spec.manifest.get("disabled_optimizers", {}).get(runtime_version())
            session = execution.session(path, disabled_optimizers=optimizers)
            inputs = {v.name: v.type for v in session.get_inputs()}
            expected = (
                {"input_ids": "tensor(int64)"}
                if kind == "text"
                else {"pixel_values": "tensor(float)"}
            )
            outputs = {v.name: v for v in session.get_outputs()}
            output = outputs.get("pooler_output")
            if inputs != expected or output is None or output.type != "tensor(float)":
                raise UserError("Контракт SigLIP 2 ONNX не совпадает с manifest.")
            if execution.provider != CPU and (kind, execution.provider) not in self.validated:
                reference = Execution("cpu", threads=self.threads).session(
                    path, disabled_optimizers=optimizers
                )
                feed = self._feed(kind)
                try:
                    check_vectors(
                        normalize(reference.run(["pooler_output"], feed)[0]),
                        normalize(session.run(["pooler_output"], feed)[0]),
                        **self.spec.manifest["device_tolerance"],
                    )
                except UserError:
                    raise
                except Exception as exc:
                    raise UserError(
                        "Не удалось проверить точность модели на выбранном устройстве."
                    ) from exc
                self.validated.add((kind, execution.provider))
        except UserError:
            if execution.device != "auto" or execution.provider == CPU:
                raise
            execution.provider = CPU
            execution.warning = "GPU недоступен: режим Авто использует CPU."
            return self._session(kind)
        self.sessions[kind] = session
        return session

    def check_contract(self):
        for kind, execution in (("image", self.execution), ("text", self.query_execution)):
            with self.lock, compute_gate(execution).slot(interactive=True):
                vector = normalize(self._session(kind).run(["pooler_output"], self._feed(kind))[0])
                if vector.shape != (3, self.dimension):
                    raise UserError("Размерность SigLIP 2 не соответствует manifest.")
        self.unload()

    def encode_text(self, texts):
        if not 1 <= len(texts) <= 32:
            raise UserError("Батч визуальной модели должен содержать от 1 до 32 текстов.")
        batch = self.tokenizer.batch(texts, limit=64, fixed_length=64)
        with self.lock, compute_gate(self.query_execution).slot(interactive=True):
            self.last_used = time.monotonic()
            vector = normalize(
                self._session("text").run(["pooler_output"], {"input_ids": batch["input_ids"]})[0]
            )
            self.last_used = time.monotonic()
        if vector.shape != (len(texts), self.dimension):
            raise UserError("Размерность SigLIP 2 не соответствует manifest.")
        return vector

    def prepare_images(self, images):
        values = []
        for data in images:
            started = time.perf_counter()
            values.append((image_tensor(data), time.perf_counter() - started))
        return values

    def encode_images(self, images, *, _prepared=None):
        if not 1 <= len(images) <= 32:
            raise UserError("Батч изображений должен содержать от 1 до 32 файлов.")
        values = _prepared if _prepared is not None else self.prepare_images(images)
        if len(values) != len(images):
            raise UserError("Подготовленный батч не соответствует изображениям.")
        pixels = np.stack([v[0] for v in values])
        prepared = time.perf_counter()
        with self.lock, compute_gate(self.execution):
            acquired = time.perf_counter()
            raw = self._session("image").run(["pooler_output"], {"pixel_values": pixels})[0]
            inferred = time.perf_counter()
            self.last_used = time.monotonic()
        vector = normalize(raw)
        if vector.shape != (len(images), self.dimension):
            raise UserError("Размерность SigLIP 2 не соответствует manifest.")
        self.last_timings = {
            "preprocess_seconds": sum(v[1] for v in values),
            "compute_wait_seconds": acquired - prepared,
            "inference_seconds": inferred - acquired,
            "postprocess_seconds": time.perf_counter() - inferred,
            "batch_size": len(images),
            "prefetched": int(_prepared is not None),
        }
        return vector
