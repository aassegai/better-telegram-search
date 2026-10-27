"""Bounded annotations and supplied retrieval judgments, without executable rules."""

import re

from telegram_search.search.chunking.classification import whole_word
from telegram_search.shared.errors import UserError
from telegram_search.shared.text import normalize_text

MAX_DATASET_BYTES = 16 * 1024**2
SPLITS = frozenset({"development", "test"})
MODES = ("exact_seeds", "seeds_plus_typos", "seeds_plus_repetitions", "full_candidates")


def require(condition):
    if not condition:
        raise UserError("Invalid noise evaluation dataset or leaking partition.")


def text(value, maximum=128, *, empty=False):
    return isinstance(value, str) and (empty or bool(value.strip())) and len(value) <= maximum


def keys(value, required, optional=()):
    return (
        isinstance(value, dict)
        and set(required) <= value.keys()
        and value.keys() <= set(required) | set(optional)
    )


def integer(value, maximum):
    return type(value) is int and 0 <= value <= maximum


def validate_dataset(data, check=lambda: None):
    require(
        keys(
            data,
            {"schema_version", "kind", "label_source", "partition_by", "scopes", "cases"},
            {"retrieval"},
        )
    )
    require(type(data["schema_version"]) is int and data["schema_version"] == 1)
    require(type(data["kind"]) is str and data["kind"] in {"synthetic", "annotated"})
    require(
        data["label_source"] == ("synthetic" if data["kind"] == "synthetic" else "human_reviewed")
    )
    partition = data["partition_by"]
    require(
        isinstance(partition, list)
        and 1 <= len(partition) <= 3
        and all(type(item) is str for item in partition)
    )
    require(
        len(set(partition)) == len(partition)
        and "family" in partition
        and set(partition) <= {"family", "scope", "period"}
    )
    require(isinstance(data["scopes"], list) and 1 <= len(data["scopes"]) <= 64)
    scopes, frequency_count = {}, 0
    for scope in data["scopes"]:
        check()
        require(
            keys(scope, {"id", "frequencies"}) and text(scope["id"]) and scope["id"] not in scopes
        )
        require(isinstance(scope["frequencies"], dict) and len(scope["frequencies"]) <= 50000)
        frequency_count += len(scope["frequencies"])
        require(frequency_count <= 100000)
        for form, counts in scope["frequencies"].items():
            check()
            require(text(form, 64) and whole_word(form) == form)
            require(keys(counts, {"whole", "eligible", "phrase"}))
            require(
                all(integer(value, 10**9) for value in counts.values())
                and counts["eligible"] <= counts["whole"]
            )
        scopes[scope["id"]] = scope["frequencies"]
    require(isinstance(data["cases"], list) and 1 <= len(data["cases"]) <= 10000)
    ids, groups, canonical = set(), {}, {}
    required = {
        "id",
        "scope",
        "family",
        "period",
        "split",
        "text",
        "label",
        "has_attachment",
        "protected",
        "parent_text",
    }
    for case in data["cases"]:
        check()
        require(keys(case, required))
        require(text(case["id"]) and case["id"] not in ids)
        ids.add(case["id"])
        require(type(case["split"]) is str and case["split"] in SPLITS)
        require(text(case["scope"]) and case["scope"] in scopes)
        require(text(case["family"]) and text(case["period"], 32))
        require(text(case["text"], 16384) and text(case["parent_text"], 16384, empty=True))
        require(type(case["label"]) is str and case["label"] in {"keep", "noise", "uncertain"})
        require(type(case["has_attachment"]) is bool and type(case["protected"]) is bool)
        form = whole_word(case["text"])
        canonical_text = (
            form if form is not None else " ".join(normalize_text(case["text"]).split())
        )
        require(len(canonical_text) <= 32768)
        require(canonical.setdefault(canonical_text, case["split"]) == case["split"])
        if form is not None and len(form) <= 64:
            require(form in scopes[case["scope"]] and scopes[case["scope"]][form]["whole"] > 0)
        for name in partition:
            key = name, case[name]
            require(groups.setdefault(key, case["split"]) == case["split"])
    if "retrieval" in data:
        validate_retrieval(
            data["retrieval"], check, {case["family"]: case["split"] for case in data["cases"]}
        )
    return scopes


def validate_retrieval(value, check, case_families):
    required = {
        "source_before",
        "source_after",
        "space_before",
        "space_after",
        "filters_before",
        "filters_after",
        "policy_before",
        "policy_after",
        "queries",
    }
    require(keys(value, required))
    for field in required - {"queries"}:
        require(
            isinstance(value[field], str)
            and re.fullmatch(r"[a-f0-9]{64}", value[field]) is not None
        )
    require(
        all(
            value[name + "_before"] == value[name + "_after"]
            for name in ("source", "space", "filters")
        )
    )
    require(isinstance(value["queries"], list) and len(value["queries"]) <= 1000)
    ids, families = set(), dict(case_families)
    for query in value["queries"]:
        check()
        require(keys(query, {"id", "family", "split", "relevance", "before", "after"}))
        require(text(query["id"]) and query["id"] not in ids)
        ids.add(query["id"])
        require(text(query["family"]) and type(query["split"]) is str and query["split"] in SPLITS)
        require(families.setdefault(query["family"], query["split"]) == query["split"])
        relevance = query["relevance"]
        require(isinstance(relevance, dict) and len(relevance) <= 1000)
        require(all(text(doc) and integer(grade, 3) for doc, grade in relevance.items()))
        for field in ("before", "after"):
            ranking = query[field]
            require(isinstance(ranking, list) and len(ranking) <= 100)
            require(all(text(doc) and doc in relevance for doc in ranking))
            require(len(set(ranking)) == len(ranking))
