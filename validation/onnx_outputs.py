"""Run with the application's Torch-free Python, using synthetic validation cases only."""

import argparse
import json
from pathlib import Path

import numpy as np

from telegram_search.config.model_registry import model_spec
from telegram_search.inference.bundles import BundleStore
from telegram_search.inference.e5 import E5Encoder

parser = argparse.ArgumentParser()
parser.add_argument("--workspace", type=Path, required=True)
parser.add_argument("--profile", choices=["small", "base"], required=True)
parser.add_argument("--cases", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
spec = model_spec(args.profile)
encoder = E5Encoder(spec, BundleStore(args.workspace).verify(spec))
cases = json.loads(args.cases.read_text())
outputs = {}
for purpose, texts in cases.items():
    for batch_size in (1, 2, 4):
        outputs[f"{purpose}_{batch_size}"] = np.concatenate(
            [
                encoder.encode_text(texts[index : index + batch_size], purpose)
                for index in range(0, len(texts), batch_size)
            ]
        )
np.savez(args.output, **outputs)
Path(str(args.output) + ".json").write_text(json.dumps(encoder.backend_info(), indent=2))
encoder.unload()
