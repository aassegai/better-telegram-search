import heapq
import threading
from datetime import timedelta
from pathlib import Path

from telegram_search.shared.errors import UserError


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


class VectorStore:
    """Rebuildable LanceDB tables; callers prefilter with canonical SQLite chunk IDs."""

    def __init__(self, workspace: Path):
        import lancedb

        location = workspace.resolve() / "data" / "vectors"
        if not location.resolve().is_relative_to(workspace.resolve()):
            raise UserError("Векторный индекс должен находиться внутри workspace.")
        self.db = lancedb.connect(location)
        self.lock = threading.RLock()

    def table(self, space_id: str, dimension: int, *, create: bool = False):
        import pyarrow as pa

        name = "text_" + space_id
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
            return self.db.create_table(name, schema=schema)

    def upsert(self, space_id: str, dimension: int, chunks: list, embeddings) -> None:
        import pyarrow as pa

        if embeddings.shape != (len(chunks), dimension):
            raise UserError("Размерность векторов не совпадает с embedding space.")
        with self.lock:
            table = self.table(space_id, dimension, create=True)
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

    def exact(self, space_id: str, dimension: int, query, eligible_ids: list[str], limit: int):
        if not eligible_ids:
            return []
        with self.lock:
            table = self.table(space_id, dimension)
            if table is None:
                raise UserError("Векторная таблица отсутствует. Перестройте semantic index.")
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

    def exact_filtered(self, space_id, dimension, query, eligible, limit, *, predicate=None):
        """One bounded scan; canonical eligibility is checked before global top-k.

        ``eligible(ids)`` returns the IDs published in the caller's SQLite snapshot.
        Only one Arrow batch and ``limit`` candidates are kept, even for large archives.
        """
        import numpy as np

        if limit <= 0:
            return []
        query = np.asarray(query, dtype=np.float32)
        if query.shape != (dimension,) or not np.isfinite(query).all():
            raise UserError("Модель вернула недопустимый embedding.")
        query_norm = np.linalg.norm(query)
        if not np.isfinite(query_norm) or query_norm <= 0:
            raise UserError("Модель вернула недопустимый embedding.")
        best = []
        with self.lock:
            table = self.table(space_id, dimension)
            if table is None:
                raise UserError("Векторная таблица отсутствует. Перестройте semantic index.")
            scan = table.search().select(["id", "vector"]).limit(None)
            if predicate:
                scan = scan.where(predicate)
            with scan.to_batches(batch_size=512) as batches:
                for batch in batches:
                    # Some readers may return a larger batch than requested.
                    for start in range(0, len(batch), 512):
                        part = batch.slice(start, 512)
                        ids = part.column("id").to_pylist()
                        allowed = set(eligible(ids))
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
                        for offset in np.flatnonzero(valid):
                            candidate = (-float(distances[offset]), ids[selected[offset]])
                            if len(best) < limit:
                                heapq.heappush(best, candidate)
                            elif candidate > best[0]:
                                heapq.heapreplace(best, candidate)
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
                table.delete(predicate)

    def delete_chat(self, spaces: list[dict], chat_id: str) -> None:
        with self.lock:
            for space in spaces:
                table = self.table(space["id"], space["dimension"])
                if table is not None:
                    table.delete(f"chat_id={sql_literal(chat_id)}")

    def compact(self, spaces: list[dict]) -> None:
        with self.lock:
            for space in spaces:
                table = self.table(space["id"], space["dimension"])
                if table is not None:
                    table.optimize(cleanup_older_than=timedelta(seconds=0), delete_unverified=True)

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
                    table.delete("id IN (" + ",".join(sql_literal(key) for key in stale) + ")")
