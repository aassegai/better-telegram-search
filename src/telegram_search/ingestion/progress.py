"""Content-free phase events shared by import workers, HTTP status and the CLI."""

import json
import time

from telegram_search.shared.text import serialize

TABLES = {"import": "imports", "preview": "import_previews"}
PUBLIC = (
    "phase",
    "completed",
    "total",
    "unit",
    "elapsed_seconds",
    "phase_elapsed_seconds",
    "rate",
    "eta_seconds",
    "activity",
)


class ImportInterrupted(Exception):
    """A control action or shutdown stopped work before its next checkpoint."""


def progress_snapshot(raw, state, *, processed=None, now=None):
    value = json.loads(raw or "{}")
    if not value:
        return None
    if processed is not None and value.get("phase") in {"checking_messages", "writing_messages"}:
        value["completed"] = processed
    if state == "running" and value.get("active"):
        extra = max(0, (time.time() if now is None else now) - value["updated_at"])
        value["elapsed_seconds"] += extra
        value["phase_elapsed_seconds"] += extra
    elapsed = value.get("phase_elapsed_seconds", 0)
    delta = value.get("completed", 0) - value.get("initial_completed", 0)
    rate = delta / elapsed if elapsed >= 1 and value.get("samples", 0) >= 3 and delta > 0 else None
    value["rate"] = rate if state == "running" else None
    value["eta_seconds"] = (
        max(0, value["total"] - value["completed"]) / rate
        if state == "running" and rate and value.get("total") is not None
        else None
    )
    return {key: value.get(key) for key in PUBLIC}


def freeze_progress(conn, table, job_id):
    if table not in TABLES.values():
        raise ValueError("Unknown import progress table")
    row = conn.execute(
        f"SELECT progress_json,state,processed FROM {table} WHERE id=?", (job_id,)
    ).fetchone()
    if not row or not row["progress_json"] or row["progress_json"] == "{}":
        return
    value = json.loads(row["progress_json"])
    snapshot = progress_snapshot(row["progress_json"], row["state"], processed=row["processed"])
    value.update(snapshot)
    value.update(active=False, updated_at=time.time(), rate=None, eta_seconds=None)
    conn.execute(f"UPDATE {table} SET progress_json=? WHERE id=?", (serialize(value), job_id))


class ImportProgress:
    interval = 0.25

    def __init__(self, importer, kind, job_id=None):
        self.importer, self.kind, self.job_id = importer, kind, job_id
        self.table = TABLES[kind]
        self.started = self.phase_started = time.monotonic()
        self.last_check = self.last_flush = 0.0
        self.previous_completed = 0
        self.base_elapsed = 0.0
        if job_id:
            with importer.db.connect() as conn:
                row = conn.execute(
                    f"SELECT progress_json FROM {self.table} WHERE id=?", (job_id,)
                ).fetchone()
            if row:
                self.base_elapsed = json.loads(row[0]).get("elapsed_seconds", 0.0)
        self.value = {}

    def check(self, *, force=False):
        now = time.monotonic()
        if not force and now - self.last_check < 0.1:
            return
        self.last_check = now
        if self.importer.stopping.is_set():
            raise ImportInterrupted()
        if self.job_id:
            with self.importer.db.connect() as conn:
                row = conn.execute(
                    f"SELECT state FROM {self.table} WHERE id=?", (self.job_id,)
                ).fetchone()
            if not row or row[0] != "running":
                raise ImportInterrupted()

    def phase(self, name, *, total=None, unit="messages", completed=0):
        self.check(force=True)
        self.phase_started = time.monotonic()
        self.previous_completed = completed
        self.value = {
            "phase": name,
            "completed": completed,
            "initial_completed": completed,
            "total": total,
            "unit": unit,
            "samples": 0,
            "activity": None,
        }
        self.flush(force=True)

    def update(self, completed, *, force=False):
        self.check()
        self.value["completed"] = completed
        self.flush(force=force)

    def activity(self, name, completed=0, total=None, unit="bytes"):
        self.check()
        self.value["activity"] = (
            {
                "phase": name,
                "completed": completed,
                "total": total,
                "unit": unit,
            }
            if name
            else None
        )
        self.flush()

    def flush(self, *, force=False):
        now = time.monotonic()
        if not force and now - self.last_flush < self.interval:
            return
        self.check(force=True)
        if self.value["completed"] > self.previous_completed:
            self.value["samples"] += 1
            self.previous_completed = self.value["completed"]
        self.value.update(
            elapsed_seconds=self.base_elapsed + now - self.started,
            phase_elapsed_seconds=now - self.phase_started,
            updated_at=time.time(),
            active=True,
        )
        if self.job_id:
            with self.importer.db.connect() as conn:
                changed = conn.execute(
                    f"UPDATE {self.table} SET progress_json=? WHERE id=? AND state='running'",
                    (serialize(self.value), self.job_id),
                ).rowcount
                if not changed:
                    raise ImportInterrupted()
        observer = self.importer.progress_observer
        if observer:
            observer(progress_snapshot(serialize(self.value), "running"))
        self.last_flush = now

    def finish(self, phase, completed):
        self.phase(phase, total=completed, completed=completed)


class TerminalProgress:
    """The same events rendered to stderr; stdout remains the JSON report."""

    def __init__(self, enabled=True):
        self.enabled = enabled
        self.bar = None
        self.phase = None

    def __call__(self, event):
        if not self.enabled:
            return
        from tqdm import tqdm

        phase = event["phase"]
        if self.phase != phase:
            self.close()
            self.phase = phase
            self.bar = tqdm(
                total=event["total"],
                desc=phase,
                unit=event["unit"],
                unit_scale=event["unit"] == "bytes",
                bar_format="{desc}: {n_fmt}/{total_fmt} {unit} |{bar}| {postfix}",
                leave=False,
            )
        details = [f"{event['elapsed_seconds']:.0f}s"]
        if event.get("rate") is not None:
            details.append(f"{event['rate']:.1f} {event['unit']}/s")
        if event.get("eta_seconds") is not None:
            details.append(f"ETA {event['eta_seconds']:.0f}s")
        if event.get("activity"):
            details.append(event["activity"]["phase"])
        self.bar.set_postfix_str(", ".join(details), refresh=False)
        self.bar.update(event["completed"] - self.bar.n)

    def close(self):
        if self.bar is not None:
            self.bar.close()
            self.bar = None
