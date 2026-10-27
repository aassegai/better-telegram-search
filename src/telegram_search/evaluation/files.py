"""Bounded local JSON and atomic owned reports for offline evaluation tools."""

import json
import os
import stat
import tempfile
from pathlib import Path

from telegram_search.evaluation.noise_schema import MAX_DATASET_BYTES
from telegram_search.security.paths import is_link_or_reparse
from telegram_search.shared.errors import UserError


def safe_path(path, root, public_paths=()):
    path = Path(path).expanduser().absolute()
    for parent in (*reversed(path.parents), path):
        if is_link_or_reparse(parent):
            raise UserError("Evaluation paths cannot contain links or junctions.")
    path = path.resolve()
    if path in public_paths:
        return path
    if not any(path.is_relative_to(root / folder) for folder in ("workspace", "plans")):
        raise UserError("Evaluation inputs and reports must stay in approved ignored roots.")
    return path


def unique_object(items):
    if len({key for key, _ in items}) != len(items):
        raise UserError("Duplicate keys in evaluation JSON.")
    return dict(items)


def invalid_constant(_value):
    raise UserError("Non-finite values in evaluation JSON.")


def read_json(source):
    descriptor = os.open(
        source, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    )
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise UserError("Evaluation input must be a regular file.")
        encoded = stream.read(MAX_DATASET_BYTES + 1)
    if len(encoded) > MAX_DATASET_BYTES:
        raise UserError("Evaluation JSON exceeds its size limit.")
    return json.loads(encoded, object_pairs_hook=unique_object, parse_constant=invalid_constant)


def check_output(path, root, owner_key, owner_value, public_paths=()):
    path = safe_path(path, root, public_paths)
    if path.suffix != ".json":
        raise UserError("Evaluation reports must use the .json extension.")
    if path.exists():
        value = read_json(path)
        if not isinstance(value, dict) or value.get(owner_key) != owner_value:
            raise UserError("Output already exists and is not an owned report.")
    return path


def write_report(path, value, *, root, owner_key, owner_value, public_paths=(), check=lambda: None):
    path = check_output(path, root, owner_key, owner_value, public_paths)
    check()
    path.parent.mkdir(parents=True, exist_ok=True)
    safe_path(path, root, public_paths)
    descriptor, temporary = tempfile.mkstemp(prefix=".noise-evaluation-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
        check_output(path, root, owner_key, owner_value, public_paths)
        check()
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
