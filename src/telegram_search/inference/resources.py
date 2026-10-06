from contextlib import contextmanager
from threading import Condition, get_ident, local


# All ONNX/OCR jobs share the same CPU budget in this application process.
class CpuGate:
    def __init__(self):
        self.condition = Condition()
        self.owner = None
        self.depth = 0
        self.interactive_waiters = 0
        self.local = local()

    @contextmanager
    def slot(self, *, interactive=False):
        with self.condition:
            if interactive:
                self.interactive_waiters += 1
            try:
                while (
                    self.owner is not None
                    and self.owner != get_ident()
                    or (self.owner != get_ident() and not interactive and self.interactive_waiters)
                ):
                    self.condition.wait()
                self.owner = get_ident()
                self.depth += 1
            finally:
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


compute_lock = CpuGate()
