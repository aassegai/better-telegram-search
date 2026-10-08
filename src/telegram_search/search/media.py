from telegram_search.search.lexical import ContextService
from telegram_search.search.ocr_words import ocr_word_search
from telegram_search.search.presentation import search_options
from telegram_search.shared.errors import UserError


class MediaSearch:
    def __init__(self, db, media, semantic, lock):
        self.db, self.media, self.semantic, self.lock = db, media, semantic, lock
        self.context = ContextService(db)

    def _dense(self, space, dimension, vector, kind, filters, version=None):
        where, params = filters.sql("m")
        extra, arguments = ("AND e.ocr_version=?", [version]) if kind == "ocr" else ("", [])
        maximum = self.db.settings.retrieval_candidates
        with self.db.connect() as conn:
            conn.execute("BEGIN")
            predicate = (
                "e.space_id=? AND e.kind=? "
                f"{extra} AND EXISTS (SELECT 1 FROM media_refs r JOIN indexable_messages m "
                "ON m.chat_id=r.chat_id AND m.message_id=r.message_id "
                "WHERE r.sha256=e.sha256 "
                f"AND r.status='ready' AND r.kind='photo' AND {where})"
            )
            parameters = (space, kind, *arguments, *params)
            if not conn.execute(
                f"SELECT 1 FROM media_embeddings e WHERE {predicate} LIMIT 1", parameters
            ).fetchone():
                return []

            def eligible(ids):
                placeholders = ",".join("?" for _ in ids)
                return [
                    row[0]
                    for row in conn.execute(
                        f"SELECT e.id FROM media_embeddings e WHERE e.id IN ({placeholders}) "
                        f"AND {predicate}",
                        (*ids, *parameters),
                    )
                ]

            found = self.semantic._vector_store().exact_filtered(
                space, dimension, vector, eligible, maximum, predicate="utc_day = 'media'"
            )
        return found

    def search(
        self, query, filters, *, kind, exact=False, mode="words", limit=100, chunk_size=None
    ):
        limit, chunk_size = search_options(self.db.settings, limit, chunk_size)
        engine, clip, encoder = self.media.ocr, self.media.clip, self.semantic.encoder
        warnings, lexical, dense = [], [], []
        image_similarities = {}
        if kind == "images":
            if exact:
                return [], [
                    "Точная фраза ищется в сообщениях и OCR; "
                    "описания фотографий используют смысловой поиск."
                ]
            if not clip:
                return [], [
                    "Поиск по описанию фотографий станет доступен "
                    "после подготовки визуальной модели в настройках."
                ]
            vector = clip.encode_text([query])[0]
            space, dimension = clip.space_id, getattr(clip, "dimension", 512)
        else:
            if not engine:
                return [], [
                    "Распознавание текста на фотографиях станет доступно после подготовки OCR."
                ]
            vector = None
            space, dimension = (
                (encoder.space_id, encoder.spec.dimension) if encoder else (None, None)
            )
            if not exact and mode != "words" and encoder:
                try:
                    encoder, vector = self.semantic.encode_query(query)
                    space, dimension = encoder.space_id, encoder.spec.dimension
                except UserError as exc:
                    warnings.append(str(exc))
            if not exact and mode != "words" and vector is None:
                warnings.append("Смысловой OCR-индекс недоступен; OCR ищется по словам.")
            if vector is not None:
                coverage = self.media.status()
                if not coverage["ocr_dense_ready"]:
                    vector = None
                    warnings.append(
                        "Смысловой OCR-индекс ещё не готов; показаны совпадения по словам."
                    )
                elif coverage["ocr_dense_ready"] < coverage["ocr_nonempty_ready"]:
                    warnings.append(
                        "Смысловой OCR-поиск охватывает только готовую часть фотографий."
                    )
        with self.lock:
            # A model/source may have changed while the query embedding was calculated.
            if (
                self.media.ocr is not engine
                or self.media.clip is not clip
                or self.semantic.encoder is not encoder
            ):
                return [], ["Индекс изменился во время поиска. Повторите запрос."]
            if kind == "images":
                matches = self._dense(space, dimension, vector, "image", filters)
                dense = [item["id"] for item in matches]
                image_similarities = {
                    item["id"]: max(-1.0, min(1.0, 1.0 - item["_distance"])) for item in matches
                }
                version = None
            else:
                version = engine.version
                lexical_evidence = {}
                if exact or mode != "meaning" or vector is None:
                    with self.db.connect() as conn:
                        lexical, lexical_evidence, lexical_warnings = ocr_word_search(
                            conn,
                            query,
                            version,
                            filters,
                            self.db.settings.retrieval_candidates,
                            exact=exact,
                        )
                        warnings.extend(lexical_warnings)
                if vector is not None:
                    dense = [
                        item["id"]
                        for item in self._dense(space, dimension, vector, "ocr", filters, version)
                    ]
            candidates = {}
            with self.db.connect() as conn:
                for channel, keys in (
                    ("ocr_words", lexical),
                    ("image" if kind == "images" else "ocr_meaning", dense),
                ):
                    for rank, key in enumerate(keys, 1):
                        if channel == "ocr_words":
                            evidence = conn.execute(
                                "SELECT sha256,text,confidence FROM ocr_cache "
                                "WHERE rowid=? AND version=?",
                                (key, version),
                            ).fetchone()
                        else:
                            evidence = conn.execute(
                                "SELECT e.sha256,o.text,o.confidence,e.char_start,e.char_end "
                                "FROM media_embeddings e LEFT JOIN ocr_cache o "
                                "ON o.sha256=e.sha256 AND o.version=? WHERE e.id=?",
                                (version or "", key),
                            ).fetchone()
                        if not evidence:
                            continue
                        where, params = filters.sql("m")
                        refs = conn.execute(
                            "SELECT m.*,c.name AS chat_name,MIN(r.id) AS media_id "
                            "FROM media_refs r JOIN indexable_messages m ON m.chat_id=r.chat_id "
                            "AND m.message_id=r.message_id JOIN chats c ON c.id=m.chat_id "
                            f"WHERE r.sha256=? AND r.status='ready' AND r.kind='photo' AND {where} "
                            "GROUP BY m.chat_id,m.message_id "
                            "ORDER BY m.timestamp,m.message_id LIMIT 100",
                            (evidence["sha256"], *params),
                        )
                        for ref in refs:
                            identity = (ref["chat_id"], ref["message_id"])
                            if identity not in candidates:
                                if len(candidates) >= self.db.settings.retrieval_candidates:
                                    continue
                                candidates[identity] = {
                                    "chat_id": ref["chat_id"],
                                    "chat_name": ref["chat_name"],
                                    "message_id": ref["message_id"],
                                    "timestamp": ref["timestamp"],
                                    "media_id": ref["media_id"],
                                    "result_type": "image" if kind == "images" else "ocr",
                                    "matched_by": [],
                                    "score": 0,
                                    "ocr_text": evidence["text"],
                                    "ocr_confidence": evidence["confidence"],
                                    "messages": self.context.get_result_context(
                                        conn, ref, chunk_size, filters
                                    ),
                                }
                            hit = candidates[identity]
                            # Multiple OCR parts are one branch vote for the same source message.
                            if channel not in hit["matched_by"]:
                                hit["matched_by"].append(channel)
                                hit["score"] += 1 / (self.db.settings.rrf_k + rank)
                            if channel == "ocr_words":
                                hit["ocr_match"] = lexical_evidence.get(key)
                            if channel == "image":
                                hit["image_similarity"] = image_similarities[key]
                            if channel == "ocr_meaning" and "ocr_range" not in hit:
                                hit["ocr_range"] = {
                                    "char_start": evidence["char_start"],
                                    "char_end": evidence["char_end"],
                                }
            return sorted(
                candidates.values(),
                key=lambda hit: (-hit["score"], hit["chat_id"], hit["message_id"]),
            )[:limit], warnings


class UnifiedSearch:
    def __init__(self, text, media, reranker=None):
        self.text, self.media, self.reranker = text, media, reranker

    def search(
        self, query, filters, exact, limit, mode, tab="text", chunk_size=None, *, modalities=None
    ):
        limit, chunk_size = search_options(self.media.db.settings, limit, chunk_size)
        available = ("text", "images", "ocr")
        if modalities is None:
            if tab not in {"all", *available}:
                raise UserError("Неизвестная вкладка поиска.")
            modalities = available if tab == "all" else (tab,)
        if (
            not isinstance(modalities, (list, tuple))
            or not 1 <= len(modalities) <= 3
            or any(kind not in available for kind in modalities)
        ):
            raise UserError("Выберите от одного до трёх типов поиска: текст, изображения, OCR.")
        # Stable branch order makes selection order and repeated values irrelevant.
        selected = tuple(kind for kind in available if kind in modalities)
        tab = selected[0] if len(selected) == 1 else "all"
        result = {"results": [], "warnings": [], "effective_mode": mode, "has_more": False}
        branches = []
        rerank_branches = []

        def rerank(hits, kind):
            if self.reranker is None:
                return hits
            hits, metadata = self.reranker.apply(query, hits, kind, filters, exact=exact)
            rerank_branches.append(metadata)
            if metadata["reason"] and metadata["reason"] not in {
                "disabled",
                "images_only",
                "exact_phrase",
                "no_candidates",
            }:
                result["warnings"].append(metadata["reason"])
            if metadata.get("truncated_candidates") or metadata.get("query_truncated"):
                result["warnings"].append(
                    "Giga обработала первые 512 токенов части фрагментов или запроса."
                )
            return hits

        def rerank_status():
            applied = any(item["applied"] for item in rerank_branches)
            return {
                "rerank_applied": applied,
                "rerank_branches": rerank_branches,
                "rerank_reason": None
                if applied
                else next((item["reason"] for item in rerank_branches), "disabled"),
            }

        if "text" in selected:
            try:
                result = self.text.search(
                    query, filters, exact, limit if tab == "text" else 100, mode, chunk_size
                )
            except UserError as exc:
                if tab == "text":
                    raise
                result["warnings"].append(str(exc))
            else:
                for hit in result["results"]:
                    hit["result_type"] = "text"
                result["results"] = rerank(result["results"], "text")
                if tab == "text":
                    return {**result, "tab": tab, "modalities": list(selected), **rerank_status()}
                branches.append(result["results"])
        for kind in ("images", "ocr"):
            if kind not in selected:
                continue
            try:
                hits, warnings = self.media.search(
                    query, filters, kind=kind, exact=exact, mode=mode, chunk_size=chunk_size
                )
            except UserError as exc:
                hits, warnings = [], [str(exc)]
            branches.append(rerank(hits, kind))
            result["warnings"].extend(warnings)
        combined = {}
        for branch in branches:
            for rank, hit in enumerate(branch, 1):
                key = (hit["chat_id"], hit["message_id"])
                if key not in combined:
                    combined[key] = {
                        **hit,
                        "score": 0,
                        "matched_by": list(hit.get("matched_by", [])),
                    }
                current = combined[key]
                current["score"] += 1 / (self.media.db.settings.rrf_k + rank)
                current["matched_by"] = list(
                    dict.fromkeys([*current["matched_by"], *hit.get("matched_by", [])])
                )
                for name in (
                    "media_id",
                    "image_similarity",
                    "ocr_text",
                    "ocr_confidence",
                    "ocr_range",
                    "ocr_match",
                ):
                    if hit.get(name) is not None:
                        current[name] = hit[name]
        hits = sorted(
            combined.values(), key=lambda hit: (-hit["score"], hit["chat_id"], hit["message_id"])
        )
        result.update(
            results=hits[:limit],
            has_more=len(hits) > limit or result["has_more"],
            tab=tab,
            modalities=list(selected),
            warnings=list(dict.fromkeys(result["warnings"])),
        )
        if tab == "all":
            result["effective_mode"] = "mixed"
        elif tab == "images":
            result["effective_mode"] = "images"
        elif tab == "ocr":
            reasons = {reason for hit in hits for reason in hit["matched_by"]}
            result["effective_mode"] = (
                "hybrid"
                if {"ocr_words", "ocr_meaning"} <= reasons
                else "meaning"
                if "ocr_meaning" in reasons
                else "words"
            )
        result["coverage"] = self.media.media.status()
        result.update(rerank_status())
        return result
