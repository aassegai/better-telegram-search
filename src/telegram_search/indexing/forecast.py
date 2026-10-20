"""Content-free recent-batch forecasts; work is tokens, characters or fixed-size images."""

import math
from statistics import median

WINDOW = 24
MIN_SAMPLES = 3


def paused_forecast(value):
    return {
        **value,
        "estimated_remaining_seconds": None,
        "lower_seconds": None,
        "upper_seconds": None,
        "measuring": False,
        "paused": True,
    }


def combine_forecasts(parts, remaining):
    """Consecutive workload groups; each group uses its own current batch configuration."""
    if not parts:
        return forecast([], remaining, now=0)
    result = dict(parts[0])
    for key in ("estimated_remaining_seconds", "lower_seconds", "upper_seconds"):
        values = [part[key] for part in parts]
        result[key] = round(sum(values), 1) if all(v is not None for v in values) else None
    result["eta_samples"] = min(part["eta_samples"] for part in parts)
    result["projected"] = any(part["projected"] for part in parts)
    result["stale"] = any(part.get("stale", False) for part in parts)
    result["measuring"] = result["estimated_remaining_seconds"] is None
    result["seconds_per_work"] = (
        result["estimated_remaining_seconds"] / remaining
        if remaining > 0 and result["estimated_remaining_seconds"] is not None
        else None
    )
    return result


def quantile(values, fraction):
    """Weighted quantile of (value, weight) pairs."""
    ordered = sorted(values)
    target = sum(weight for _, weight in ordered) * fraction
    through = 0.0
    for value, weight in ordered:
        through += weight
        if through >= target:
            return value
    return ordered[-1][0]


def forecast(samples, remaining, *, now, projection=0.0):
    """Samples: (observed_at, work, seconds). Never extrapolate an expired session."""
    valid = [
        (stamp, work, seconds)
        for stamp, work, seconds in samples
        if all(math.isfinite(value) for value in (stamp, work, seconds))
        and work > 0
        and seconds > 0
        and 0 <= now - stamp <= 3600
    ][-WINDOW:]
    if valid:
        expiry = min(3600, max(60, median(item[2] for item in valid) * 10))
        valid = [item for item in valid if now - item[0] <= expiry]
    result = {
        "estimated_remaining_seconds": 0.0 if remaining <= 0 else None,
        "lower_seconds": 0.0 if remaining <= 0 else None,
        "upper_seconds": 0.0 if remaining <= 0 else None,
        "eta_samples": len(valid),
        "measuring": remaining > 0 and len(valid) < MIN_SAMPLES,
        "projected": projection > 0,
        "seconds_per_work": None,
        "stale": remaining > 0 and bool(samples) and not valid,
    }
    if remaining <= 0 or len(valid) < MIN_SAMPLES:
        return result
    # Large batches contribute proportionally to their actual work. Recent batches
    # have a six-batch half-life; a tiny tail batch cannot dominate the whole queue.
    rates = [
        (seconds / work, work * 0.5 ** ((len(valid) - 1 - index) / 6))
        for index, (_, work, seconds) in enumerate(valid)
    ]
    center = quantile(rates, 0.5)
    # One lock/OS stall is not a new sustained speed. Repeated slow batches still
    # move the median and adapt the forecast rather than freezing it forever.
    bounded = [(min(center * 2, max(center / 2, value)), weight) for value, weight in rates]
    rate = sum(value * weight for value, weight in bounded)
    rate /= sum(weight for _, weight in rates)
    low = min(rate * 0.8, quantile(bounded, 0.2))
    high = max(rate * 1.2, quantile(bounded, 0.8))
    uncertain = min(1.0, max(0.0, projection)) * 0.5
    result.update(
        estimated_remaining_seconds=round(remaining * rate, 1),
        lower_seconds=round(remaining * low * (1 - uncertain), 1),
        upper_seconds=round(remaining * high * (1 + uncertain), 1),
        seconds_per_work=rate,
    )
    return result
