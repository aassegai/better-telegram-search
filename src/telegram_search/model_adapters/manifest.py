"""Data-only inspection of a versioned local adapter package; never imports its code."""

import hashlib
import json
import re
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from telegram_search.security.paths import is_link_or_reparse
from telegram_search.shared.errors import UserError
from telegram_search.shared.text import serialize

ROLES = frozenset({"text_embedding", "image_embedding", "reranker", "ocr", "asr"})
MAX_MANIFEST_BYTES = 1024 * 1024


def fail(message="Invalid custom model manifest."):
    raise UserError(message)


def strict_json(value):
    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                fail("Duplicate JSON field in adapter data.")
            result[key] = item
        return result

    def constant(_value):
        fail("Nonfinite JSON number in adapter data.")

    try:
        return json.loads(value, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, RecursionError) as exc:
        raise UserError("Invalid adapter JSON.") from exc


def relative_file(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 512:
        fail()
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or any(p in {"", ".", ".."} for p in value.split("/"))
        or any(c in value for c in ("\\", ":", "\x00"))
        or any(c in value for c in '<>"|?*')
        or any(ord(c) < 32 for c in value)
        or any(
            p.endswith((".", " "))
            or p.split(".")[0].upper()
            in {
                "CON",
                "PRN",
                "AUX",
                "NUL",
                *[f"COM{i}" for i in range(1, 10)],
                *[f"LPT{i}" for i in range(1, 10)],
            }
            for p in path.parts
        )
    ):
        fail("Adapter files must have relative portable paths.")
    return path


def plain_path(path):
    path = path.expanduser().absolute()
    if any(is_link_or_reparse(p) for p in (path, *path.parents)):
        fail("Adapter package paths cannot contain links or junctions.")
    return path


def checksum(path, check=lambda: None, progress=lambda _count: None):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024**2):
            check()
            digest.update(block)
            progress(len(block))
    return digest.hexdigest()


def text(value, maximum=256):
    return isinstance(value, str) and 1 <= len(value) <= maximum and "\x00" not in value


@dataclass(frozen=True)
class AdapterManifest:
    # Store canonical JSON, not an externally mutable nested dictionary.
    canonical: str

    @property
    def data(self):
        return json.loads(self.canonical)

    @property
    def identity(self):
        return hashlib.sha256(self.canonical.encode()).hexdigest()

    @property
    def role(self):
        return self.data["role"]

    @classmethod
    def parse(cls, data):
        required = {
            "schema_version",
            "adapter_api_version",
            "id",
            "role",
            "entrypoint",
            "revision",
            "supported_languages",
            "runtime",
            "devices",
            "input_contract",
            "output_contract",
            "dependencies",
            "files",
            "preprocessing",
            "network",
        }
        if (
            not isinstance(data, dict)
            or set(data) - (required | {"description"})
            or required - data.keys()
        ):
            fail()
        if any(
            type(data[key]) is not int or data[key] != 1
            for key in ("schema_version", "adapter_api_version")
        ):
            fail("Unsupported adapter API or schema version.")
        if (
            not isinstance(data["id"], str)
            or not re.fullmatch(r"[a-z][a-z0-9._-]{0,63}", data["id"])
            or not text(data["role"])
            or data["role"] not in ROLES
            or not text(data["revision"], 128)
            or data["network"] is not False
        ):
            fail()
        if "description" in data and not text(data["description"], 2000):
            fail()
        entry = data["entrypoint"]
        if not isinstance(entry, str) or entry.count(":") != 1:
            fail("Entrypoint must be a relative Python file and class name.")
        filename, factory = entry.split(":")
        if relative_file(filename).suffix != ".py" or not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*", factory
        ):
            fail("Invalid adapter entrypoint.")
        for key, allowed in (("devices", {"cpu", "gpu"}), ("supported_languages", None)):
            values = data[key]
            if (
                not isinstance(values, list)
                or not 1 <= len(values) <= 64
                or not all(text(item, 32) for item in values)
                or len(values) != len(set(values))
                or (allowed is not None and not set(values) <= allowed)
            ):
                fail()
        runtime = data["runtime"]
        if (
            not isinstance(runtime, dict)
            or set(runtime) != {"name", "version"}
            or not all(text(v, 128) for v in runtime.values())
        ):
            fail()
        dependencies = data["dependencies"]
        if (
            not isinstance(dependencies, dict)
            or len(dependencies) > 256
            or not all(
                isinstance(k, str)
                and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", k)
                and text(v, 128)
                for k, v in dependencies.items()
            )
        ):
            fail("Adapter dependencies must pin exact installed distribution versions.")
        if runtime["name"] != "python" and dependencies.get(runtime["name"]) != runtime["version"]:
            fail("The declared runtime must have an exact dependency pin.")
        if runtime["name"] == "python" and runtime["version"] != "3":
            fail("The stdlib adapter runtime is Python 3.")
        if not isinstance(data["preprocessing"], dict) or not text(
            data["preprocessing"].get("version")
        ):
            fail("Preprocessing identity is required.")
        if not isinstance(data["input_contract"], dict) or not isinstance(
            data["output_contract"], dict
        ):
            fail()
        inputs, outputs = data["input_contract"], data["output_contract"]
        batch = inputs.get("max_batch")
        if type(batch) is not int or not 1 <= batch <= 128:
            fail("Adapter max_batch must be between 1 and 128.")
        if data["role"] in {"text_embedding", "image_embedding"}:
            dimension = outputs.get("dimension")
            if (
                type(dimension) is not int
                or not 1 <= dimension <= 16384
                or outputs.get("dtype") != "float32"
                or outputs.get("metric") != "cosine"
                or outputs.get("normalization") != "l2"
            ):
                fail("Embedding adapters require finite L2 float32 cosine vectors.")
        if data["role"] == "text_embedding":
            for key in ("query_max_tokens", "document_max_tokens"):
                if type(inputs.get(key)) is not int or not 8 <= inputs[key] <= 32768:
                    fail("Text token budgets must be explicit.")
            if inputs["document_max_tokens"] < 480:
                fail("Text adapters must accept the application's 480-token document chunks.")
            for key in ("query_prefix", "document_prefix"):
                if (
                    not isinstance(data["preprocessing"].get(key), str)
                    or len(data["preprocessing"][key]) > 4096
                ):
                    fail("Text prefixes must be explicit.")
        if data["role"] == "reranker" and outputs.get("higher_is_better") is not True:
            fail("Reranker scores must increase with relevance.")
        if data["role"] == "ocr" and outputs.get("bbox") != "pixel-xyxy":
            fail("OCR regions use pixel xyxy coordinates.")
        if data["role"] in {"ocr", "asr"} and (
            outputs.get("confidence_range") != [0, 1]
            or any(type(v) is not int for v in outputs["confidence_range"])
        ):
            fail("Confidence, when present, must be normalized to [0, 1].")
        if data["role"] == "asr" and (
            type(inputs.get("sample_rate")) is not int or not 8000 <= inputs["sample_rate"] <= 48000
        ):
            fail("ASR must declare a supported PCM sample rate.")
        files = data["files"]
        if not isinstance(files, list) or not 1 <= len(files) <= 1024:
            fail()
        names = set()
        for item in files:
            if not isinstance(item, dict) or set(item) != {"path", "bytes", "sha256"}:
                fail()
            name = relative_file(item["path"]).as_posix()
            if (
                name.casefold() == "manifest.json"
                or name.casefold() in names
                or type(item["bytes"]) is not int
                or not 0 <= item["bytes"] <= 16 * 1024**3
                or not isinstance(item["sha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])
            ):
                fail("Invalid or duplicate adapter file record.")
            names.add(name.casefold())
        if filename not in {item["path"] for item in files}:
            fail("The adapter entrypoint must be checksum-pinned.")
        try:
            canonical = serialize(data)
        except (ValueError, TypeError, RecursionError) as exc:
            raise UserError("Invalid adapter manifest values.") from exc
        if len(canonical.encode()) > MAX_MANIFEST_BYTES:
            fail("Adapter manifest is too large.")
        # Programmatic callers must also reject NaN/Inf in arbitrary metadata.
        strict_json(canonical)
        return cls(canonical)

    @classmethod
    def read(cls, path):
        path = plain_path(Path(path))
        if not stat.S_ISREG(path.stat().st_mode) or path.stat().st_size > MAX_MANIFEST_BYTES:
            fail("Adapter manifest must be a small regular JSON file.")
        return cls.parse(strict_json(path.read_text(encoding="utf-8")))

    def verify_files(self, root, *, closed=False, check=lambda: None, on_progress=None):
        root = plain_path(Path(root))
        completed = 0
        total = sum(item["bytes"] for item in self.data["files"])

        def advance(count):
            nonlocal completed
            completed += count
            if on_progress:
                on_progress(
                    {"phase": "verifying", "completed": completed, "total": total, "unit": "bytes"}
                )

        for item in self.data["files"]:
            check()
            path = plain_path(root / item["path"])
            if (
                not path.is_relative_to(root)
                or not stat.S_ISREG(path.stat().st_mode)
                or path.stat().st_size != item["bytes"]
                or checksum(path, check, advance) != item["sha256"]
            ):
                fail("Adapter package file checksum mismatch.")
        if closed:
            allowed = {item["path"] for item in self.data["files"]} | {"manifest.json"}
            for path in root.rglob("*"):
                plain_path(path)
                if not path.is_dir() and path.relative_to(root).as_posix() not in allowed:
                    fail("The checked adapter package contains an unpinned file.")

    def summary(self):
        data = self.data
        return {
            key: data[key]
            for key in ("id", "role", "revision", "runtime", "devices", "supported_languages")
        } | {
            "identity": self.identity,
            "file_bytes": sum(f["bytes"] for f in data["files"]),
            "adapter_api_version": 1,
        }
