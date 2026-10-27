"""Separate a complete source scan from bounded, uncalibrated evidence lists."""

COVERAGE_VERSION = "source-and-evidence-v1"
EVIDENCE_LIMITS = frozenset({"candidate_limit", "payload_limit"})


def coverage(limits):
    limits = set(limits)
    source_complete = not (limits - EVIDENCE_LIMITS)
    return {
        "coverage_version": COVERAGE_VERSION,
        "source_complete": source_complete,
        "evidence_complete": not (limits & EVIDENCE_LIMITS),
        # This contract only permits approved seeds plus explicit overrides.
        # A future calibrated gate must define its own applicability contract.
        "policy_applicable": source_complete,
    }


def application_ready(summary):
    if (
        not isinstance(summary, dict)
        or summary.get("new_auto_omit_enabled") is not False
        or type(summary.get("complete")) is not bool
        or not isinstance(summary.get("limits"), list)
        or any(type(limit) is not str for limit in summary["limits"])
        or len(summary["limits"]) != len(set(summary["limits"]))
    ):
        return False
    limits = set(summary["limits"])
    if "coverage_version" not in summary:
        # Do not upgrade old limited reports or infer missing evidence metadata.
        versioned_fields = {
            "source_complete",
            "evidence_complete",
            "policy_applicable",
            "limited_evidence_forms",
        }
        return (
            summary["complete"] is True and not limits and not (versioned_fields & summary.keys())
        )
    expected = coverage(limits)
    return (
        summary["coverage_version"] == COVERAGE_VERSION
        and all(
            type(summary.get(key)) is bool and summary[key] == expected[key]
            for key in ("source_complete", "evidence_complete", "policy_applicable")
        )
        and summary["complete"] == (not limits)
        and summary["source_complete"] is True
    )
