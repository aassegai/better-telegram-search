"""Validate partial reports without confusing safe exact rules with full coverage."""

COVERAGE_VERSION = "bounded-exact-rules-v2"
LEGACY_COVERAGE_VERSION = "source-and-evidence-v1"
EVIDENCE_LIMITS = frozenset({"candidate_limit", "payload_limit"})
SOURCE_LIMITS = frozenset(
    {
        "message_limit",
        "form_limit",
        "phrase_form_limit",
        "text_limit",
        "time_limit",
        "report_size_limit",
    }
)
KNOWN_LIMITS = SOURCE_LIMITS | EVIDENCE_LIMITS


def coverage(limits, *, legacy=False):
    limits = set(limits)
    source_complete = not (limits - EVIDENCE_LIMITS)
    return {
        "coverage_version": LEGACY_COVERAGE_VERSION if legacy else COVERAGE_VERSION,
        "source_complete": source_complete,
        "evidence_complete": not (limits & EVIDENCE_LIMITS),
        # Only approved seeds and explicit exact overrides can be compiled.
        # Unknown formats/limits and automatic novel omissions remain rejected.
        "policy_applicable": source_complete if legacy else not (limits - KNOWN_LIMITS),
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
        # An old unversioned partial report cannot prove its contract.
        versioned_fields = {
            "source_complete",
            "evidence_complete",
            "policy_applicable",
            "limited_evidence_forms",
        }
        return (
            summary["complete"] is True and not limits and not (versioned_fields & summary.keys())
        )
    version = summary["coverage_version"]
    if (
        not isinstance(version, str)
        or version not in {COVERAGE_VERSION, LEGACY_COVERAGE_VERSION}
        or limits - KNOWN_LIMITS
    ):
        return False
    expected = coverage(limits, legacy=version == LEGACY_COVERAGE_VERSION)
    return all(
        type(summary.get(key)) is bool and summary[key] == expected[key]
        for key in ("source_complete", "evidence_complete", "policy_applicable")
    ) and summary["complete"] == (not limits)
