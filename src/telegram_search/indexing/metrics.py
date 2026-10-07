"""Bounded, numeric-only sliding diagnostics. Never retain image/text/path payloads."""

import math
import threading
import time
from collections import deque

import psutil


class ResourceMetrics:
    def __init__(self):
        self.previous = None
        self.lock = threading.Lock()

    def snapshot(self):
        parent = psutil.Process()
        rss = child_rss = cpu = threads = 0
        for process in [parent, *parent.children(recursive=True)]:
            try:
                memory = process.memory_info().rss
                cpu_times = process.cpu_times()
                rss += memory
                child_rss += memory if process.pid != parent.pid else 0
                cpu += cpu_times.user + cpu_times.system
                threads += process.num_threads()
            except psutil.Error:
                continue
        now = time.monotonic()
        with self.lock:
            previous, self.previous = self.previous, (now, cpu)
        utilization = (
            max(0, (cpu - previous[1]) / (now - previous[0]) * 100)
            if previous and now > previous[0]
            else None
        )
        return {
            "rss_bytes": rss,
            "child_rss_bytes": child_rss,
            "native_threads": threads,
            "cpu_percent_one_core": round(utilization, 1) if utilization is not None else None,
            "ram_available_bytes": psutil.virtual_memory().available,
            "gpu_utilization_percent": None,
            "vram_bytes": None,
        }


class StageMetrics:
    def __init__(self, window=64):
        self.samples = {}
        self.lock = threading.Lock()
        self.window = window

    def reset(self, stage):
        with self.lock:
            self.samples.pop(stage, None)

    def record(self, stage, identity, start, finish, *, units=1, errors=0, phases=None):
        if finish <= start or units <= 0:
            return
        numeric = {
            name: float(value)
            for name, value in (phases or {}).items()
            if isinstance(value, (int, float)) and math.isfinite(value) and value >= 0
        }
        with self.lock:
            previous, samples = self.samples.setdefault(
                stage, (identity, deque(maxlen=self.window))
            )
            if previous != identity:
                samples = deque(maxlen=self.window)
                self.samples[stage] = (identity, samples)
            samples.append((start, finish, units, errors, numeric))

    def snapshot(self, stage, identity, pending):
        with self.lock:
            stored, samples = self.samples.get(stage, (None, ()))
            samples = list(samples) if identity == stored else []
        # Union of intervals accounts for parallel lanes without counting idle time.
        covered = 0.0
        end = -math.inf
        for start, finish, *_ in sorted(samples):
            covered += max(0, finish - max(start, end))
            end = max(end, finish)
        units = sum(s[2] for s in samples)
        rate = units / covered if covered and len(samples) >= 2 else None
        durations = sorted((s[1] - s[0]) / s[2] for s in samples)
        phases = {}
        for name in {key for s in samples for key in s[4]}:
            values = [s[4][name] for s in samples if name in s[4]]
            phases[name] = round(sum(values) / len(values), 6)
        return {
            "pending": max(0, pending),
            "samples": len(samples),
            "completed": units,
            "errors": sum(s[3] for s in samples),
            "cold_starts": sum(s[4].get("cold_start", 0) for s in samples),
            "regions_per_second": round(sum(s[4].get("regions", 0) for s in samples) / covered, 2)
            if covered
            else None,
            "units_per_minute": round(rate * 60, 2) if rate else None,
            "p50_seconds": round(durations[len(durations) // 2], 4) if durations else None,
            "p95_seconds": round(durations[math.ceil(len(durations) * 0.95) - 1], 4)
            if durations
            else None,
            "estimated_remaining_seconds": round(pending / rate, 1)
            if pending > 0 and rate
            else 0.0
            if pending <= 0
            else None,
            "phase_means": phases,
            "measuring": pending > 0 and rate is None,
        }
