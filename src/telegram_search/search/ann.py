"""IVF_FLAT metadata and bounded canonical prefilters for LanceDB 0.25/0.39."""

import heapq
import math
import re
from dataclasses import dataclass
from itertools import islice

from telegram_search.search.ann_process import AnnUnavailable
from telegram_search.search.execution import advance, checkpoint, count, phase, unknown_count

INDEX_PREFIX = "bts_ivfflat_cosine_v1"
MIN_ANN_ROWS = 1024


def _owned_index(name, dimension):
    return re.fullmatch(rf"{INDEX_PREFIX}_d{dimension}_p([1-9][0-9]{{0,3}})", name)


@dataclass(frozen=True)
class AnnOptions:
    partitions: int = 64
    probes: int = 32
    minimum_rows: int = MIN_ANN_ROWS

    def __post_init__(self):
        if (
            type(self.partitions) is not int
            or not 1 <= self.partitions <= 4096
            or type(self.probes) is not int
            or not 1 <= self.probes <= 4096
            or type(self.minimum_rows) is not int
            or not 32 <= self.minimum_rows
        ):
            raise ValueError("Invalid ANN options")


def index_status(table, dimension):
    """Only our sole compatible vector index is eligible for native selection."""
    field = table.schema.field("vector")
    if getattr(field.type, "list_size", None) != dimension:
        return {"ready": False, "reason": "ann_dimension_mismatch"}
    indices = [index for index in table.list_indices() if "vector" in index.columns]
    if not indices:
        return {"ready": False, "reason": "ann_index_unavailable"}
    if len(indices) != 1:
        return {"ready": False, "reason": "ann_index_incompatible"}
    match = _owned_index(indices[0].name, dimension)
    if not match or int(match[1]) > 4096:
        return {"ready": False, "reason": "ann_index_incompatible"}
    stats = table.index_stats(indices[0].name)
    if (
        stats is None
        or stats.index_type != "IVF_FLAT"
        or stats.distance_type != "cosine"
        or stats.num_indexed_rows <= 0
    ):
        return {"ready": False, "reason": "ann_index_incompatible"}
    return {
        "ready": True,
        "name": indices[0].name,
        "partitions": int(match[1]),
        "dimension": dimension,
        "metric": "cosine",
        "index_type": "IVF_FLAT",
        "table_version": table.version,
        "indexed_rows": stats.num_indexed_rows,
        "unindexed_rows": stats.num_unindexed_rows,
    }


def build_index(table, dimension, options):
    """Native build primitive; scheduling and resource isolation belong to the caller."""
    rows = table.count_rows()
    if rows < options.minimum_rows:
        return {"ready": False, "reason": "ann_table_too_small", "rows": rows}
    if getattr(table.schema.field("vector").type, "list_size", None) != dimension:
        raise AnnUnavailable("ann_dimension_mismatch")
    previous = [index.name for index in table.list_indices() if "vector" in index.columns]
    if any(not _owned_index(name, dimension) for name in previous):
        return {"ready": False, "reason": "ann_index_incompatible"}
    partitions = min(options.partitions, max(1, rows // 32))
    name = f"{INDEX_PREFIX}_d{dimension}_p{partitions}"
    # This API is shared by 0.25.3 (macOS Intel) and 0.39.0. No PQ compression
    # error or Torch dependency; only IVF partition selection is approximate.
    table.create_index(
        metric="cosine",
        num_partitions=partitions,
        index_type="IVF_FLAT",
        sample_rate=32,
        max_iterations=30,
        name=name,
    )
    # Keep the old usable index if training fails. After a successful native
    # commit, retire only our old configurations; a retry repairs an interrupted
    # retirement. The caller holds its vector writer lock during this primitive.
    stats = table.index_stats(name)
    if stats is None or stats.num_indexed_rows <= 0:
        raise AnnUnavailable("ann_build_failed")
    for old in previous:
        if old != name:
            table.drop_index(old)
    return index_status(table, dimension)


def canonical_scope(table, eligible, predicate):
    """Prove that the whole coarse predicate equals a canonically allowed scope.

    Metadata only, at most 512 IDs in Python. A failed proof never authorizes an
    unfiltered query: the caller then pushes canonical ID batches into native ANN.
    The caller must retain its SQLite snapshot and vector/lifecycle locks.
    """
    scan = table.search().select(["id"]).limit(None)
    if predicate:
        scan = scan.where(predicate)
    with scan.to_batches(batch_size=512) as reader:
        for batch in reader:
            for start in range(0, len(batch), 512):
                checkpoint()
                ids = batch.slice(start, 512).column("id").to_pylist()
                count("eligibility_ids_read", len(ids))
                advance(len(ids))
                if set(ids) != set(eligible(ids)):
                    return False
    return True


def retrieve(
    table,
    dimension,
    query,
    eligible,
    eligible_batches,
    limit,
    predicate,
    options,
    native,
    location,
    status,
    phase_name,
):
    phase(phase_name + "_scope", unit="ids")
    broad = canonical_scope(table, eligible, predicate)
    if not broad and eligible_batches is None:
        raise AnnUnavailable("ann_scope_unavailable")
    phase(phase_name, unit="native_queries")
    best, retained = [], set()
    version = status["table_version"]

    def scopes():
        if broad:
            yield predicate
            return
        for batch in eligible_batches:
            iterator = iter(batch)
            while ids := list(islice(iterator, 512)):
                checkpoint()
                # Even direct callers cannot turn this into an unchecked set of
                # candidate IDs. SQL eligibility remains authoritative.
                ids = sorted(set(ids).intersection(eligible(ids)))
                if ids:
                    selected = (
                        "id IN ("
                        + ",".join("'" + key.replace("'", "''") + "'" for key in ids)
                        + ")"
                    )
                    yield f"({predicate}) AND {selected}" if predicate else selected

    for scope in scopes():
        checkpoint()
        # Lance doesn't expose the internal distance count or bytes read. Returned
        # candidates are not a substitute for those measurements.
        unknown_count("vectors_read", "vectors_scored")
        count("ann_queries", 1)
        found = native.query(
            location=location,
            table=table.name,
            version=version,
            query=query,
            predicate=scope,
            probes=min(options.probes, status["partitions"]),
            limit=limit,
        )
        count("ann_candidates", len(found))
        checkpoint()
        for start in range(0, len(found), 512):
            keys = [row["id"] for row in found[start : start + 512]]
            if set(keys) - set(eligible(keys)):
                raise AnnUnavailable("ann_scope_changed")
        for row in found:
            key, distance = row["id"], row["_distance"]
            if key in retained or not math.isfinite(distance):
                continue
            candidate = (-float(distance), key)
            if len(best) < limit:
                heapq.heappush(best, candidate)
                retained.add(key)
            elif candidate > best[0]:
                retained.remove(heapq.heapreplace(best, candidate)[1])
                retained.add(key)
        advance(1)
    checkpoint()
    return [{"id": key, "_distance": -score} for score, key in sorted(best, reverse=True)]
