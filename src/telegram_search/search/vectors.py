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
