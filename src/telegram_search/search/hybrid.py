from telegram_search.search.lexical import ContextService, Filters, SearchService, fts_query
from telegram_search.search.presentation import search_options
from telegram_search.shared.errors import UserError


class HybridSearch:
    """BM25 and exact cosine retrieve the same published, prefiltered chunks."""

    def __init__(self, db, semantic, lifecycle_lock):
        self.db = db
        self.semantic = semantic
        self.lock = lifecycle_lock
        self.lexical = SearchService(db)
        self.context = ContextService(db)

    @staticmethod
    def eligible_sql(filters, space_id):
        where, params = filters.sql()
        predicate = (
            "c.embedding_space_id=? AND c.generation=s.target_generation "
            "AND s.chunk_generation=s.target_generation "
            "AND s.dense_generation=s.target_generation AND s.embedding_space_id=? "
            "AND EXISTS(SELECT 1 FROM chunk_parts p JOIN indexable_messages m "
            "ON m.chat_id=p.chat_id AND m.message_id=p.message_id "
            f"WHERE p.chunk_id=c.id AND {where})"
        )
        if filters.exclude_deleted:
            predicate += (
                " AND NOT EXISTS(SELECT 1 FROM chunk_parts dp JOIN indexable_messages dm "
                "ON dm.chat_id=dp.chat_id AND dm.message_id=dp.message_id "
                "WHERE dp.chunk_id=c.id AND dm.remote_deleted=1)"
            )
        return predicate, [space_id, space_id, *params]

    @staticmethod
    def coverage_warnings(status):
        if not status["pending_segments"]:
            return []
        return [
            f"Смысловой индекс готов для {status['ready_segments']} из "
            f"{status['total_segments']} сегментов. "
            "Эта выдача охватывает только готовую часть. "
            "Выберите «По словам», чтобы искать по всему архиву."
        ]

    def search(self, query, filters=None, exact=False, limit=None, mode="words", chunk_size=None):
        limit, chunk_size = search_options(self.db.settings, limit, chunk_size)
        if mode not in {"words", "meaning", "hybrid"}:
            raise UserError("Недопустимые параметры поиска.")
        filters = filters or Filters()
        if mode == "words" or exact:
            with self.lock:
                result = self.lexical.search(query, filters, exact, limit, chunk_size)
            return {**result, "effective_mode": "words", "warnings": []}
        if not query.strip():
            return {"results": [], "backend": "chunks", "has_more": False, "warnings": []}
        status = self.semantic.status()
        warnings = self.coverage_warnings(status)
        if not status["dense_available"] or not status["ready_segments"]:
            with self.lock:
                result = self.lexical.search(query, filters, exact, limit, chunk_size)
            warnings = ["Смысловой поиск ещё не готов. Показаны результаты по словам."]
            return {**result, "effective_mode": "words", "warnings": warnings}
        # Interactive inference happens outside the writer lock. Revalidate the space
        # after acquiring it so a model switch cannot mix query and document vectors.
        try:
            encoder, vector = self.semantic.encode_query(query)
        except UserError:
            with self.lock:
                status = self.semantic.status()
                if status["dense_available"]:
                    raise
                result = self.lexical.search(query, filters, exact, limit, chunk_size)
                return {
                    **result,
                    "effective_mode": "words",
                    "warnings": ["Модель готовится. Показаны результаты по словам."],
                }
        with self.lock:
            if self.semantic.encoder is not encoder:
                raise UserError("Модель поиска изменилась. Повторите запрос.")
            status = self.semantic.status()
            warnings = self.coverage_warnings(status)
            if not status["dense_available"] or not status["ready_segments"]:
                result = self.lexical.search(query, filters, exact, limit, chunk_size)
                warnings = ["Смысловой поиск ещё не готов. Показаны результаты по словам."]
                return {**result, "effective_mode": "words", "warnings": warnings}
            with self.db.connect() as conn:
                conn.execute("BEGIN")
                predicate, params = self.eligible_sql(filters, encoder.space_id)
                joins = (
                    "FROM chunks c JOIN index_segments s "
                    "ON s.chat_id=c.chat_id AND s.utc_day=c.utc_day "
                )
                candidates = self.db.settings.retrieval_candidates
                lexical = []
                match = fts_query(query, False)
                if mode == "hybrid" and match:
                    lexical = [
                        row[0]
                        for row in conn.execute(
                            "SELECT c.id FROM chunk_fts JOIN chunks c "
                            "ON c.rowid=chunk_fts.rowid JOIN index_segments s "
                            "ON s.chat_id=c.chat_id AND s.utc_day=c.utc_day "
                            f"WHERE chunk_fts MATCH ? AND {predicate} "
                            "ORDER BY bm25(chunk_fts),c.id LIMIT ?",
                            [match, *params, candidates],
                        )
                    ]

                def eligible(ids):
                    placeholders = ",".join("?" for _ in ids)
                    return [
                        row[0]
                        for row in conn.execute(
                            f"SELECT c.id {joins} WHERE c.id IN ({placeholders}) AND {predicate}",
                            [*ids, *params],
                        )
                    ]

                found = []
                if conn.execute(f"SELECT 1 {joins} WHERE {predicate} LIMIT 1", params).fetchone():
                    found = self.semantic._vector_store().exact_filtered(
                        encoder.space_id,
                        encoder.spec.dimension,
                        vector,
                        eligible,
                        candidates,
                        predicate="utc_day <> 'media'",
                    )
                dense = [item["id"] for item in found]
                scores, branches = {}, {}
                for name, ranked, weight in (
                    ("words", lexical, self.db.settings.lexical_weight),
                    ("meaning", dense, self.db.settings.dense_weight),
                ):
                    for rank, chunk_id in enumerate(ranked, 1):
                        scores[chunk_id] = scores.get(chunk_id, 0) + weight / (
                            self.db.settings.rrf_k + rank
                        )
                        branches.setdefault(chunk_id, []).append(name)
                results, seen = [], set()
                more = False
                witness, witness_params = filters.sql()
                # Prefer a source message containing query terms when a small
                # display window cannot show the complete indexed chunk.
                anchor_order = ""
                anchor_params = []
                if match:
                    anchor_order = (
                        "CASE WHEN EXISTS (SELECT 1 FROM message_fts "
                        "WHERE rowid=m.rowid AND message_fts MATCH ?) THEN 0 ELSE 1 END,"
                    )
                    anchor_params = [match.replace(" AND ", " OR ")]
                for chunk_id in sorted(scores, key=lambda key: (-scores[key], key)):
                    anchor = conn.execute(
                        "SELECT m.*,ch.name AS chat_name FROM chunk_parts p "
                        "JOIN indexable_messages m "
                        "ON m.chat_id=p.chat_id AND m.message_id=p.message_id "
                        "JOIN chats ch ON ch.id=m.chat_id "
                        f"WHERE p.chunk_id=? AND {witness} "
                        f"ORDER BY {anchor_order}p.ordinal LIMIT 1",
                        [chunk_id, *witness_params, *anchor_params],
                    ).fetchone()
                    if not anchor or (anchor["chat_id"], anchor["message_id"]) in seen:
                        continue
                    if len(results) == limit:
                        more = True
                        break
                    context = self.context.get_result_context(conn, anchor, chunk_size, filters)
                    seen.update((anchor["chat_id"], item["message_id"]) for item in context)
                    parts = [
                        dict(row)
                        for row in conn.execute(
                            "SELECT message_id,char_start,char_end FROM chunk_parts "
                            "WHERE chunk_id=? ORDER BY ordinal",
                            (chunk_id,),
                        )
                    ]
                    results.append(
                        {
                            "chat_id": anchor["chat_id"],
                            "chat_name": anchor["chat_name"],
                            "anchor_message_id": anchor["message_id"],
                            "message_id": anchor["message_id"],
                            "chunk_id": chunk_id,
                            "timestamp": anchor["timestamp"],
                            "messages": context,
                            "matched_parts": parts,
                            "matched_by": branches[chunk_id],
                            "score": scores[chunk_id],
                        }
                    )
                return {
                    "results": results,
                    "backend": "e5_chunks_" + mode,
                    "effective_mode": mode,
                    "has_more": more,
                    "warnings": warnings,
                    "embedding_space_id": encoder.space_id,
                }
