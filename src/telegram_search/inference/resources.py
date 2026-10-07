import sys
from collections import deque
from contextlib import ExitStack, contextmanager, nullcontext
from threading import Condition, Lock, get_ident, local


# CPU jobs share a queue. Each GPU has a separate queue so CPU OCR can overlap
# GPU inference without launching competing GPU jobs on the same device.
class CpuGate:
    def __init__(self):
        self.condition = Condition()
        self.owner = None
        self.depth = 0
        self.interactive_waiters = 0
        self.local = local()
        self.waiters = deque()

    @contextmanager
    def slot(self, *, interactive=False):
        with self.condition:
            reentrant = self.owner == get_ident()
            ticket = object()
            if not reentrant:
                self.waiters.append(ticket)
            if interactive:
                self.interactive_waiters += 1
            try:
                while (
                    self.owner is not None
                    and self.owner != get_ident()
                    or (self.owner != get_ident() and not interactive and self.interactive_waiters)
                    or (not reentrant and not interactive and self.waiters[0] is not ticket)
                ):
                    self.condition.wait()
                self.owner = get_ident()
                self.depth += 1
            finally:
                if not reentrant:
                    self.waiters.remove(ticket)
                if interactive:
                    self.interactive_waiters -= 1
        try:
            yield
        finally:
            with self.condition:
                self.depth -= 1
                if not self.depth:
                    self.owner = None
                self.condition.notify_all()

    def __enter__(self):
        # Each acquisition needs its own context even for reentrant calls.
        stack = getattr(self.local, "stack", [])
        context = self.slot()
        context.__enter__()
        self.local.stack = [*stack, context]
        return self

    def __exit__(self, *args):
        return self.local.stack.pop().__exit__(*args)


class CombinedGate(CpuGate):
    """Auto may fall back in a child: reserve CPU then GPU in a stable order."""

    def __init__(self, cpu, gpu):
        super().__init__()
        self.gates = (cpu, gpu)

    @contextmanager
    def slot(self, *, interactive=False):
        with ExitStack() as stack:
            for gate in self.gates:
                stack.enter_context(gate.slot(interactive=interactive))
            yield


compute_lock = CpuGate()
_gpu_gates = {}
_gates_lock = Lock()


def compute_gate(execution=None):
    automatic = getattr(execution, "device", None) == "auto"
    provider = getattr(execution, "provider", "CPUExecutionProvider")
    if automatic:
        provider = (
            "CoreMLExecutionProvider" if sys.platform == "darwin" else "CUDAExecutionProvider"
        )
    if provider == "CPUExecutionProvider":
        return compute_lock
    key = (provider, getattr(execution, "device_id", 0))
    with _gates_lock:
        gpu = _gpu_gates.setdefault(key, CpuGate())
    return CombinedGate(compute_lock, gpu) if automatic else gpu


def memory_exhausted(exc):
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        message = str(exc).lower()
        if (
            isinstance(exc, MemoryError)
            or any(
                term in message
                for term in (
                    "out of memory",
                    "bad_alloc",
                    "failed to allocate",
                    "cublas_status_alloc_failed",
                    "cudnn_status_alloc_failed",
                    "cuda_error_out_of_memory",
                    "cudaerrormemoryallocation",
                )
            )
            or ("available memory of" in message and "smaller than requested" in message)
        ):
            return True
        exc = exc.__cause__ or exc.__context__
    return False


def indexing_batch_size(encoder, requested):
    return min(requested, getattr(encoder, "index_batch_limit", None) or requested)


def backoff_indexing_batch(encoder, batch_size):
    with getattr(encoder, "condition", nullcontext()):
        limit = indexing_batch_size(encoder, max(1, batch_size // 2))
        encoder.index_batch_limit = limit
    return limit
