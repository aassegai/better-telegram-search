from pathlib import Path

from telegram_search.model_adapters.client import AdapterContext
from telegram_search.model_adapters.store import AdapterStore
from telegram_search.model_adapters.validation import inspect_adapter, validate_adapter


def add_adapter_parser(commands):
    adapters = commands.add_parser(
        "adapters", help="Inspect or explicitly run a local model adapter"
    )
    actions = adapters.add_subparsers(dest="adapter_action", required=True)
    actions.add_parser("list", help="List registered adapters without executing code")
    inspect = actions.add_parser("inspect", help="Check manifest and hashes without executing code")
    inspect.add_argument("manifest", type=Path)
    validate = actions.add_parser(
        "validate", help="Execute trusted adapter code on public smoke inputs"
    )
    validate.add_argument("manifest", type=Path)
    validate.add_argument(
        "--python", type=Path, required=True, help="Python in a separate adapter environment"
    )
    validate.add_argument(
        "--register", action="store_true", help="Save only after the smoke test passes"
    )
    validate.add_argument("--device", choices=["cpu", "gpu"], default="cpu")
    validate.add_argument("--timeout", type=float, default=60)
    validate.add_argument("--gpu-device-id", type=int, default=0)
    validate.add_argument("--gpu-memory-limit-mib", type=int, default=4096)
    validate.add_argument("--cpu-threads", type=int, default=4)


def adapter_command(args):
    if args.adapter_action == "inspect":
        return inspect_adapter(args.manifest)
    store = AdapterStore(args.workspace)
    if args.adapter_action == "list":
        return {"models": [item["summary"] for item in store.registrations().values()]}
    return validate_adapter(
        store,
        args.manifest,
        args.python,
        register=args.register,
        timeout=args.timeout,
        context=AdapterContext(
            device=args.device,
            gpu_device_id=args.gpu_device_id,
            gpu_memory_limit_mib=args.gpu_memory_limit_mib,
            cpu_threads=args.cpu_threads,
        ),
    )
