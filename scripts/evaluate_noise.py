"""Offline, bounded noise diagnostics; private inputs/reports stay in ignored roots."""

import argparse
import json
import time
from pathlib import Path

from telegram_search.evaluation.files import check_output, read_json, safe_path, write_report
from telegram_search.evaluation.noise import evaluate_dataset
from telegram_search.shared.errors import UserError

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_FIXTURE = ROOT / "tests/fixtures/synthetic/noise-evaluation.json"


def confined(path, *, public_input=False):
    return safe_path(path, ROOT, (PUBLIC_FIXTURE,) if public_input else ())


def run(source, output, *, seconds=300):
    if type(seconds) is not int or not 1 <= seconds <= 3600:
        raise UserError("Invalid noise evaluation time limit.")
    source, output = confined(source, public_input=True), confined(output)
    if source == output or output.exists() and source.samefile(output):
        raise UserError("Evaluation output cannot replace its input.")
    deadline = time.monotonic() + seconds

    def check():
        if time.monotonic() >= deadline:
            raise UserError("Noise evaluation time limit reached.")

    output = check_output(output, ROOT, "evaluation_version", "noise-diagnostics-v1")
    check()
    data = read_json(source)
    result = evaluate_dataset(data, check=check)
    check()
    result["elapsed_seconds"] = round(seconds - (deadline - time.monotonic()), 3)
    write_report(
        output,
        result,
        root=ROOT,
        owner_key="evaluation_version",
        owner_value="noise-diagnostics-v1",
        check=check,
    )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / "workspace/evaluation/noise.json")
    parser.add_argument("--seconds", type=int, default=300)
    args = parser.parse_args()
    try:
        result = run(args.source, args.output, seconds=args.seconds)
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    except (OSError, ValueError, TypeError, RecursionError):
        parser.exit(
            1, "Noise evaluation failed: invalid input, unsafe path, leakage or time limit.\n"
        )


if __name__ == "__main__":
    main()
