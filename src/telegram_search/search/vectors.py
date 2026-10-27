import heapq
import os
import re
import threading
from datetime import timedelta
from pathlib import Path

from telegram_search.search.execution import (
    advance,
    checkpoint,
    count,
    current_execution,
    phase,
    search_lock,
    strategy,
)
from telegram_search.security.paths import owned_vector_path
from telegram_search.shared.errors import UserError


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


class VectorStore:
    """Rebuildable LanceDB tables; callers prefilter with canonical SQLite chunk IDs."""

    def __init__(self, workspace: Path):
        import lancedb

        self.workspace = workspace.resolve()
        location = owned_vector_path(self.workspace, Path("data/vectors"))
        self.db = lancedb.connect(location)
        self.location = location
        self.lock = threading.RLock()
        self.indexed_ids = set()
        self.ann_process = None
        self.ann_observer = None
        self.ann_options = None
        self.ann_threads = 4
        self.ann_memory_limit_mib = 512

    def _ann_invalidate(self, space_id):
        if self.ann_observer is not None:
            self.ann_observer.invalidate(space_id)

    def _ann_dirty(self, space_id, dimension):
        if self.ann_observer is not None:
            self.ann_observer.dirty(space_id, dimension)

    def close(self):
        if self.ann_process is not None:
            self.ann_process.close()

    def ann_status(self, space_id, dimension):
        from telegram_search.search.ann import index_status

        with search_lock(self.lock):
            table = self.table(space_id, dimension)
            return (
                index_status(table, dimension)
                if table is not None
                else {"ready": False, "reason": "ann_index_unavailable"}
            )

    def build_ann(self, space_id, dimension, *, options=None):
        """Synchronous primitive for the background builder, not an HTTP handler."""
        from telegram_search.search.ann import AnnOptions, build_index

        with self.lock:
            self._ann_invalidate(space_id)
            table = self.table(space_id, dimension)
            if table is None:
                return {"ready": False, "reason": "ann_index_unavailable"}
            return build_index(table, dimension, options or AnnOptions())

    def search_filtered(
        self,
        space_id,
        dimension,
        query,
        eligible,
        limit,
        *,
        predicate=None,
        eligible_batches=None,
        total=None,
        phase_name="vectors",
        ann_options=None,
    ):
        """Use requested ANN only with a compatible index and canonical prefilter.

        Direct calls without a search execution retain the exact default. On a
        failed native attempt, a batch factory can reopen the canonical cursor in
        the same SQLite snapshot. Legacy one-shot iterators require a full scan
        through ``eligible`` after they may have been partially consumed.
        """
        from telegram_search.search.ann import AnnOptions, index_status, retrieve
        from telegram_search.search.ann_process import AnnProcess, AnnUnavailable

        options = ann_options or self.ann_options or AnnOptions()
        execution = current_execution()
        batch_factory = eligible_batches if callable(eligible_batches) else None
        if batch_factory is not None:
            eligible_batches = batch_factory()
        arguments = dict(
            predicate=predicate,
            eligible_batches=eligible_batches,
            total=total,
            phase_name=phase_name,
        )
        if limit <= 0:
            return []
        if execution is None or execution.requested_strategy != "ann":
            return self.exact_filtered(space_id, dimension, query, eligible, limit, **arguments)
        query = self._query_vector(query, dimension)
        attempted = False
        with search_lock(self.lock):
            try:
                if total is not None and total < max(options.minimum_rows, limit * 2):
                    raise AnnUnavailable("ann_scope_too_small")
                if eligible is None:
                    raise AnnUnavailable("ann_scope_unavailable")
                table = self.table(space_id, dimension)
                if table is None:
                    raise AnnUnavailable("ann_index_unavailable")
                self._validate_table(table, dimension)
                status = index_status(table, dimension)
                if not status["ready"]:
                    raise AnnUnavailable(status["reason"])
                if self.ann_process is not None and (
                    self.ann_process.threads != self.ann_threads
                    or self.ann_process.memory_limit_mib != self.ann_memory_limit_mib
                ):
                    if not self.ann_process.close():
                        raise AnnUnavailable("ann_worker_stopping")
                    self.ann_process = None
                if self.ann_process is None:
                    self.ann_process = AnnProcess(
                        threads=self.ann_threads, memory_limit_mib=self.ann_memory_limit_mib
                    )
                attempted = True
                found = retrieve(
                    table,
                    dimension,
                    query,
                    eligible,
                    eligible_batches,
                    limit,
                    predicate,
                    options,
                    self.ann_process,
                    self.location,
                    status,
                    phase_name,
                )
                strategy("ann")
                return found
            except AnnUnavailable as exc:
                reason = exc.reason
            except UserError:
                raise
            except Exception:
                # Native index/IO failures may include private local paths.
                reason = "ann_query_failed"
            checkpoint()
            strategy("exact", reason)
            if attempted:
                # Release the consumed reader before opening another SQLite
                # cursor, including its materialized canonical-ID scope.
                arguments["eligible_batches"] = None
                eligible_batches = None
                arguments["eligible_batches"] = batch_factory() if batch_factory else None
            return self.exact_filtered(space_id, dimension, query, eligible, limit, **arguments)

    @staticmethod
    def _query_vector(query, dimension):
        import numpy as np

        query = np.asarray(query, dtype=np.float32)
        if query.shape != (dimension,) or not np.isfinite(query).all():
            raise UserError("Модель вернула недопустимый embedding.")
        norm = np.linalg.norm(query)
        if not np.isfinite(norm) or norm <= 0:
            raise UserError("Модель вернула недопустимый embedding.")
        return query

    @staticmethod
    def _validate_table(table, dimension):
        import pyarrow as pa

        vector_type = table.schema.field("vector").type
        if (
            not pa.types.is_fixed_size_list(vector_type)
            or vector_type.list_size != dimension
            or vector_type.value_type != pa.float32()
        ):
            raise UserError("Размерность векторной таблицы не совпадает с embedding space.")

    def table_path(self, space_id):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", space_id):
            raise UserError("Недопустимый идентификатор векторной таблицы.")
        return owned_vector_path(
            self.workspace, Path("data/vectors") / ("text_" + space_id + ".lance")
        )

    def table(self, space_id: str, dimension: int, *, create: bool = False):
        import pyarrow as pa

        name = "text_" + space_id
        self.table_path(space_id)
        with self.lock:
            try:
                return self.db.open_table(name)
            except ValueError:
                if not create:
                    return None
            schema = pa.schema(
                [
                    pa.field("id", pa.string()),
                    pa.field("chat_id", pa.string()),
                    pa.field("utc_day", pa.string()),
                    pa.field("generation", pa.int64()),
                    pa.field("vector", pa.list_(pa.float32(), dimension)),
                ]
            )
            self.indexed_ids.discard(space_id)
            return self.db.create_table(name, schema=schema)

    def upsert(self, space_id: str, dimension: int, chunks: list, embeddings) -> None:
        import pyarrow as pa

        if embeddings.shape != (len(chunks), dimension):
            raise UserError("Размерность векторов не совпадает с embedding space.")
        with self.lock:
            table = self.table(space_id, dimension, create=True)
            self._validate_table(table, dimension)
            data = pa.Table.from_pylist(
                [
                    {
                        "id": chunk["id"],
                        "chat_id": chunk["chat_id"],
                        "utc_day": chunk["utc_day"],
                        "generation": chunk["generation"],
                        "vector": embedding.tolist(),
                    }
                    for chunk, embedding in zip(chunks, embeddings, strict=True)
                ],
                schema=table.schema,
            )
            table.merge_insert(
                "id"
            ).when_matched_update_all().when_not_matched_insert_all().execute(data)
            if space_id not in self.indexed_ids:
                # A scalar index lets bounded canonical-ID predicates fetch only
                # scoped vectors. Lance also includes the unindexed append tail.
                if not any("id" in index.columns for index in table.list_indices()):
                    table.create_scalar_index("id", index_type="BTREE", replace=False)
                self.indexed_ids.add(space_id)
            self._ann_dirty(space_id, dimension)

    def exact(self, space_id: str, dimension: int, query, eligible_ids: list[str], limit: int):
        if not eligible_ids:
            return []
        with self.lock:
            table = self.table(space_id, dimension)
            if table is None:
                raise UserError("Векторная таблица отсутствует. Перестройте semantic index.")
            self._validate_table(table, dimension)
            query = self._query_vector(query, dimension)
            predicate = "id IN (" + ",".join(sql_literal(item) for item in eligible_ids) + ")"
            return (
                table.search(query)
                .distance_type("cosine")
                .bypass_vector_index()
                .where(predicate, prefilter=True)
                .select(["id", "_distance"])
                .limit(limit)
                .to_list()
            )

    def exact_filtered(
        self,
        space_id,
        dimension,
        query,
        eligible,
        limit,
        *,
        predicate=None,
        eligible_batches=None,
        total=None,
        phase_name="vectors",
    ):
        """Bounded exact retrieval; canonical eligibility precedes global top-k.

        ``eligible(ids)`` returns the IDs published in the caller's SQLite snapshot.
        Only one Arrow batch and ``limit`` candidates are kept, even for large archives.
        """
        import numpy as np

        if limit <= 0:
            return []
        checkpoint()
        query = self._query_vector(query, dimension)
        query_norm = np.linalg.norm(query)
        strategy("exact")
        best = []
        phase(phase_name, total=total, unit="vectors")
        with search_lock(self.lock):
            table = self.table(space_id, dimension)
            if table is None:
                raise UserError("Векторная таблица отсутствует. Перестройте semantic index.")
            self._validate_table(table, dimension)
            # Broad scopes use one sequential pass. Repeated small-ID predicates
            # would otherwise rescan a large unindexed append tail many times.
            scoped = eligible_batches is not None
            if scoped and eligible is not None and total is not None:
                rows = table.count_rows(predicate)
                scoped = total * 4 < rows
                if scoped:
                    # Older archives may lack the scalar ID index, or have a
                    # large unindexed append tail. Each small IN predicate then
                    # rereads that tail. One sequential, canonically checked
                    # pass is cheaper than repeating it for every ID batch.
                    indices = [index for index in table.list_indices() if "id" in index.columns]
                    stats = table.index_stats(indices[0].name) if indices else None
                    batches = (total + 511) // 512
                    scoped = bool(stats and total + stats.num_unindexed_rows * batches < rows)

            def scans():
                if not scoped:
                    yield predicate
                else:
                    for ids in eligible_batches:
                        checkpoint()
                        if not ids:
                            continue
                        selected_ids = "id IN (" + ",".join(sql_literal(key) for key in ids) + ")"
                        yield f"({predicate}) AND {selected_ids}" if predicate else selected_ids

            def read_batches():
                for selected_scope in scans():
                    scan = table.search().select(["id", "vector"]).limit(None)
                    if selected_scope:
                        scan = scan.where(selected_scope)
                    with scan.to_batches(batch_size=512) as reader:
                        yield from reader

            batches = read_batches()
            try:
                for batch in batches:
                    # Some readers may return a larger batch than requested.
                    for start in range(0, len(batch), 512):
                        part = batch.slice(start, 512)
                        checkpoint()
                        count("vectors_read", len(part))
                        count("exact_vectors_read", len(part))
                        ids = part.column("id").to_pylist()
                        allowed = set(ids if scoped else eligible(ids))
                        selected = [i for i, key in enumerate(ids) if key in allowed]
                        if not selected:
                            continue
                        column = part.column("vector")
                        values = column.values.slice(
                            column.offset * dimension, len(part) * dimension
                        )
                        matrix = values.to_numpy(zero_copy_only=False).reshape(-1, dimension)[
                            selected
                        ]
                        # einsum avoids starting a large BLAS thread pool for each small batch.
                        norms = np.sqrt(np.einsum("ij,ij->i", matrix, matrix))
                        valid = (
                            column.is_valid().to_numpy(zero_copy_only=False)[selected]
                            & np.isfinite(matrix).all(axis=1)
                            & np.isfinite(norms)
                            & (norms > 0)
                        )
                        distances = np.ones(len(matrix), dtype=np.float32)
                        np.divide(
                            np.einsum("ij,j->i", matrix, query),
                            norms * query_norm,
                            out=distances,
                            where=valid,
                        )
                        distances = 1 - distances
                        scored = int(np.count_nonzero(valid))
                        count("vectors_scored", scored)
                        count("exact_vectors_scored", scored)
                        advance(scored)
                        for offset in np.flatnonzero(valid):
                            candidate = (-float(distances[offset]), ids[selected[offset]])
                            if len(best) < limit:
                                heapq.heappush(best, candidate)
                            elif candidate > best[0]:
                                heapq.heapreplace(best, candidate)
            finally:
                batches.close()
        checkpoint()
        return [{"id": key, "_distance": -score} for score, key in sorted(best, reverse=True)]

    def prune_segment(
        self, spaces: list[dict], chat_id: str, day: str, keep_space: str, generation: int
    ):
        with self.lock:
            for space in spaces:
                table = self.table(space["id"], space["dimension"])
                if table is None:
                    continue
                predicate = f"chat_id={sql_literal(chat_id)} AND utc_day={sql_literal(day)}"
                if space["id"] == keep_space:
                    predicate += f" AND generation<{int(generation)}"
                else:
                    predicate += f" AND generation<={int(generation)}"
                if table.count_rows(predicate):
                    self._ann_invalidate(space["id"])
                    table.delete(predicate)

    def delete_chat(self, spaces: list[dict], chat_id: str) -> None:
        with self.lock:
            for space in spaces:
                table = self.table(space["id"], space["dimension"])
                if table is not None and table.count_rows(f"chat_id={sql_literal(chat_id)}"):
                    self._ann_invalidate(space["id"])
                    table.delete(f"chat_id={sql_literal(chat_id)}")

    def compact(self, spaces: list[dict]) -> None:
        with self.lock:
            for space in spaces:
                self._ann_invalidate(space["id"])
                table = self.table(space["id"], space["dimension"])
                if table is not None:
                    table.optimize(cleanup_older_than=timedelta(seconds=0), delete_unverified=True)

    def clear(self, spaces, *, predicate):
        return self.clear_partitions(spaces, [predicate])

    def clear_partitions(self, spaces, predicates):
        """Remove one modality, including old on-disk versions, under the writer lock.

        Text and semantic OCR can share a table. Never drop such a table while
        the other partition still contains vectors.
        """
        if not predicates:
            return 0
        removed = 0
        with self.lock:
            for space in spaces:
                self._ann_invalidate(space["id"])
                path = self.table_path(space["id"])
                for base, directories, filenames in os.walk(path, followlinks=False):
                    for name in [*directories, *filenames]:
                        owned_vector_path(
                            self.workspace, (Path(base) / name).relative_to(self.workspace)
                        )
                table = self.table(space["id"], space["dimension"])
                if table is None:
                    continue
                deleted = 0
                for predicate in predicates:
                    count = table.count_rows(predicate)
                    if count:
                        table.delete(predicate)
                        deleted += count
                if table.count_rows() == 0:
                    self.db.drop_table("text_" + space["id"])
                    self.indexed_ids.discard(space["id"])
                else:
                    # A prior attempt may have committed deletion but failed to
                    # compact. Retrying must reclaim those old versions too.
                    table.optimize(cleanup_older_than=timedelta(seconds=0), delete_unverified=True)
                removed += deleted
        return removed

    def prune_media(self, space_id, dimension, eligible):
        """Stream only media IDs; never load corpus vectors into Python during cleanup."""
        with self.lock:
            table = self.table(space_id, dimension)
            if table is None:
                return
            batches = (
                table.search()
                .where("utc_day='media'")
                .select(["id"])
                .limit(None)
                .to_batches(batch_size=512)
            )
            for batch in batches:
                ids = batch.column("id").to_pylist()
                stale = set(ids) - eligible(ids)
                if stale:
                    self._ann_invalidate(space_id)
                    table.delete("id IN (" + ",".join(sql_literal(key) for key in stale) + ")")
