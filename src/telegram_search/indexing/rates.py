"""Bounded persistent numeric observations; old lifetime-average rates are never reused."""

import hashlib
import json
import math
import time
from collections import defaultdict

from telegram_search.indexing.forecast import WINDOW, forecast


def provider(encoder):
    execution = getattr(encoder, "execution", None)
    if execution is None:
        return "cpu"
    selected = getattr(execution, "provider", None) or execution.info().get("provider", "cpu")
    return "CPU+" + selected if getattr(encoder, "cpu_peer", None) else selected


def rate_space(encoder, settings=None):
    execution = getattr(encoder, "execution", None)
    values = [
        "batch-forecast-v2",
        getattr(encoder, "space_id", getattr(encoder, "version", "")),
        provider(encoder),
        getattr(encoder, "threads", 1),
        getattr(encoder, "rate_settings", None),
        getattr(encoder, "index_batch_limit", None),
        getattr(encoder, "batch_limit", None),
        getattr(getattr(encoder, "cpu_peer", None), "batch_limit", None),
        getattr(execution, "device_id", None),
        getattr(execution, "memory_limit_mib", None),
        [
            getattr(settings, name, None)
            for name in (
                "cpu_threads",
                "gpu_index_load_percent",
                "gpu_memory_limit_mib",
                "memory_limit_mib",
                "embedding_batch",
                "image_batch",
                "ocr_batch_size",
                "ocr_region_batch_size",
                "ocr_cpu_workers",
                "ocr_max_edge",
            )
        ],
    ]
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def record_rate(
    conn,
    kind,
    encoder,
    units,
    seconds,
    *,
    settings=None,
    batch=0,
    lane="main",
    errors=0,
    cold=False,
    variant=None,
):
    """Only successful warm batches calibrate ETA. No content, paths or image IDs."""
    if (
        errors
        or cold
        or not math.isfinite(units)
        or not math.isfinite(seconds)
        or units <= 0
        or seconds <= 0
    ):
        return
    identity, now = rate_space(encoder, settings), time.time()
    conn.execute(
        "INSERT INTO index_rate_samples(kind,identity,lane,batch_size,"
        "variant,observed_at,work,seconds) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (kind, identity, lane, batch, json.dumps(variant), now, units, seconds),
    )
    conn.execute(
        "DELETE FROM index_rate_samples WHERE kind=? AND identity=? AND lane=? "
        "AND id NOT IN (SELECT id FROM index_rate_samples WHERE kind=? AND identity=? "
        "AND lane=? ORDER BY id DESC LIMIT ?)",
        (kind, identity, lane, kind, identity, lane, WINDOW),
    )
    conn.execute(
        "DELETE FROM index_rate_samples WHERE observed_at<? OR id NOT IN "
        "(SELECT id FROM index_rate_samples ORDER BY id DESC LIMIT 1024)",
        (now - 86400,),
    )


def prediction(
    conn, kind, encoder, settings, remaining, *, batch=None, projection=0.0, variant=None
):
    lanes = defaultdict(list)
    if encoder:
        clause = " AND batch_size=?" if batch is not None else ""
        if variant is not None:
            clause += " AND variant=?"
        for row in conn.execute(
            "SELECT lane,observed_at,work,seconds FROM index_rate_samples WHERE kind=? "
            "AND identity=?" + clause + " ORDER BY id",
            (
                kind,
                rate_space(encoder, settings),
                *([batch] if batch is not None else []),
                *([json.dumps(variant)] if variant is not None else []),
            ),
        ):
            lanes[row[0]].append(tuple(row)[1:])
    predictions = [
        forecast(values, remaining, now=time.time(), projection=projection)
        for values in lanes.values()
    ]
    if kind == "ocr" and getattr(encoder, "cpu_peer", None) and set(lanes) != {"ocr", "ocr_cpu"}:
        return forecast([], remaining, now=time.time())
    if not predictions:
        return forecast([], remaining, now=time.time())
    if len(predictions) == 1:
        return predictions[0]
    if any(p["seconds_per_work"] is None for p in predictions):
        result = forecast([], remaining, now=time.time())
        result["eta_samples"] = sum(p["eta_samples"] for p in predictions)
        return result
    combined = dict(predictions[0])
    for key in ("estimated_remaining_seconds", "lower_seconds", "upper_seconds"):
        values = [p[key] for p in predictions]
        combined[key] = round(1 / sum(1 / value for value in values), 1) if all(values) else 0.0
    combined["seconds_per_work"] = 1 / sum(1 / p["seconds_per_work"] for p in predictions)
    combined["eta_samples"] = sum(p["eta_samples"] for p in predictions)
    return combined
