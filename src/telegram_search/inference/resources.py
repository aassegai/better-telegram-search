import sys
import time
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

    def background_delay(self):
        return 0.0

    def released(self, started, interactive):
        pass

    @contextmanager
    def slot(self, *, interactive=False, _ticket=None):
        with self.condition:
            reentrant = self.owner == get_ident()
            ticket = _ticket if _ticket is not None else object()
            if not reentrant and _ticket is None:
                self.waiters.append(ticket)
            if interactive:
                self.interactive_waiters += 1
            try:
                while True:
                    blocked = (
                        self.owner is not None
                        and self.owner != get_ident()
                        or (
                            self.owner != get_ident()
                            and not interactive
                            and self.interactive_waiters
                        )
                        or (not reentrant and not interactive and self.waiters[0] is not ticket)
                    )
                    delay = 0.0 if reentrant or interactive else self.background_delay()
                    if not blocked and delay <= 0:
                        break
                    self.condition.wait(timeout=delay if delay > 0 else None)
                self.owner = get_ident()
                self.depth += 1
                started = time.monotonic()
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
                    self.released(started, interactive)
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
        cpu, gpu = self.gates
        # Keep a FIFO GPU ticket, but do not occupy CPU while merely cooling
        # down. Once ready, the original CPU -> GPU acquisition order remains.
        reservation = nullcontext(None) if interactive else gpu.reserve_background()
        with reservation as ticket, ExitStack() as stack:
            stack.enter_context(cpu.slot(interactive=interactive))
            stack.enter_context(gpu.slot(interactive=interactive, _ticket=ticket))
            yield


class GpuGate(CpuGate):
    """Approximate indexing duty cycle shared by all models on one GPU.

    Cooldowns release the condition lock; interactive queries bypass them. A
    bounded pause keeps settings/shutdown responsive after a long first batch.
    This controls application scheduling, not hardware utilization or power.
    """

    def __init__(self, percent=100, *, clock=time.monotonic):
        super().__init__()
        self.percent, self.clock = percent, clock
        self.next_background = 0.0

    def background_delay(self):
        return max(0.0, self.next_background - self.clock())

    def released(self, started, interactive):
        if not interactive:
            elapsed = max(0.0, time.monotonic() - started)
            self.next_background = self.clock() + min(2.0, elapsed * (100 / self.percent - 1))

    def configure(self, percent):
        with self.condition:
            self.percent = percent
            self.next_background = 0.0
            self.condition.notify_all()

    @contextmanager
    def reserve_background(self):
        ticket = object()
        with self.condition:
            reentrant = self.owner == get_ident()
            if not reentrant:
                self.waiters.append(ticket)
        try:
            if not reentrant:
                with self.condition:
                    while True:
                        delay = self.background_delay()
                        blocked = (
                            self.owner is not None
                            or self.interactive_waiters
                            or self.waiters[0] is not ticket
                        )
                        if not blocked and delay <= 0:
                            break
                        self.condition.wait(timeout=delay if delay > 0 else None)
            yield None if reentrant else ticket
        finally:
            with self.condition:
                if ticket in self.waiters:
                    self.waiters.remove(ticket)
                    self.condition.notify_all()


compute_lock = CpuGate()
_gpu_gates = {}
_gates_lock = Lock()
_gpu_index_load_percent = 100


def configure_gpu_load(percent):
    global _gpu_index_load_percent
    with _gates_lock:
        _gpu_index_load_percent = percent
        for gate in _gpu_gates.values():
            gate.configure(percent)


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
        gpu = _gpu_gates.setdefault(key, GpuGate(_gpu_index_load_percent))
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
