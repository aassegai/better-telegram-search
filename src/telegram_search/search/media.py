import heapq

from telegram_search.search.lexical import ContextService, fts_query
from telegram_search.search.presentation import search_options
from telegram_search.shared.errors import UserError
from telegram_search.shared.text import normalize_text


class MediaSearch:
    def __init__(self, db, media, semantic, lock):
        self.db, self.media, self.semantic, self.lock = db, media, semantic, lock
        self.context = ContextService(db)

    def _dense(self, space, dimension, vector, kind, filters, version=None):
        where, params = filters.sql("m")
        extra, arguments = ("AND e.ocr_version=?", [version]) if kind == "ocr" else ("", [])
        best = []
        maximum = self.db.settings.retrieval_candidates
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT e.id FROM media_embeddings e WHERE e.space_id=? AND e.kind=? "
                f"{extra} AND EXISTS (SELECT 1 FROM media_refs r JOIN messages m "
                "ON m.chat_id=r.chat_id AND m.message_id=r.message_id WHERE r.sha256=e.sha256 "
                f"AND r.status='ready' AND r.kind='photo' AND {where})",
                (space, kind, *arguments, *params),
            )
            while batch := rows.fetchmany(512):
                for hit in self.semantic._vector_store().exact(
                    space, dimension, vector, [row[0] for row in batch], maximum
                ):
                    candidate = (-hit["_distance"], hit["id"])
                    if len(best) < maximum:
                        heapq.heappush(best, candidate)
                    elif candidate > best[0]:
                        heapq.heapreplace(best, candidate)
        return [key for _, key in sorted(best, reverse=True)]

    def search(
        self, query, filters, *, kind, exact=False, mode="words", limit=100, chunk_size=None
    ):
        limit, chunk_size = search_options(self.db.settings, limit, chunk_size)
        engine, clip, encoder = self.media.ocr, self.media.clip, self.semantic.encoder
        warnings, lexical, dense = [], [], []
        if kind == "images":
            if exact:
                return [], [
                    "Точная фраза ищется в сообщениях и OCR; "
                    "описания фотографий используют смысловой поиск."
                ]
            if not clip:
                return [], [
                    "Поиск по описанию фотографий станет доступен "
                    "после подготовки CLIP в настройках."
                ]
            vector = clip.encode_text([query])[0]
            space, dimension = clip.space_id, 512
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
                dense = self._dense(space, dimension, vector, "image", filters)
                version = None
            else:
                version = engine.version
                match = fts_query(query, exact)
                where, params = filters.sql("m")
                if match and (exact or mode != "meaning" or vector is None):
                    extra, arguments = (
                        ("AND instr(o.text_normalized,?)>0", [normalize_text(query.strip())])
                        if exact
                        else ("", [])
                    )
                    with self.db.connect() as conn:
                        lexical = [
                            str(row[0])
                            for row in conn.execute(
                                "SELECT o.rowid FROM ocr_fts JOIN ocr_cache o "
                                "ON o.rowid=ocr_fts.rowid "
                                "WHERE ocr_fts MATCH ? AND o.version=? AND o.state='ready' "
                                f"{extra} AND EXISTS (SELECT 1 FROM media_refs r JOIN messages m "
                                "ON m.chat_id=r.chat_id AND m.message_id=r.message_id "
                                "WHERE r.sha256=o.sha256 AND r.kind='photo' "
                                f"AND r.status='ready' AND {where}) "
                                "ORDER BY bm25(ocr_fts),o.rowid LIMIT ?",
                                (
                                    match,
                                    version,
                                    *arguments,
                                    *params,
                                    self.db.settings.retrieval_candidates,
                                ),
                            )
                        ]
                if vector is not None:
                    dense = self._dense(space, dimension, vector, "ocr", filters, version)
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
                            "FROM media_refs r JOIN messages m ON m.chat_id=r.chat_id "
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
    def __init__(self, text, media):
        self.text, self.media = text, media

    def search(self, query, filters, exact, limit, mode, tab="text", chunk_size=None):
        limit, chunk_size = search_options(self.media.db.settings, limit, chunk_size)
        if tab not in {"all", "text", "images", "ocr"}:
            raise UserError("Неизвестная вкладка поиска.")
        result = {"results": [], "warnings": [], "effective_mode": mode, "has_more": False}
        branches = []
        if tab in {"all", "text"}:
            result = self.text.search(
                query, filters, exact, limit if tab == "text" else 100, mode, chunk_size
            )
            for hit in result["results"]:
                hit["result_type"] = "text"
            if tab == "text":
                return result
            branches.append(result["results"])
        for kind in ("images", "ocr"):
            if tab not in {"all", kind}:
                continue
            try:
                hits, warnings = self.media.search(
                    query, filters, kind=kind, exact=exact, mode=mode, chunk_size=chunk_size
                )
            except UserError as exc:
                hits, warnings = [], [str(exc)]
            branches.append(hits)
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
                for name in ("media_id", "ocr_text", "ocr_confidence", "ocr_range"):
                    if hit.get(name) is not None:
                        current[name] = hit[name]
        hits = sorted(
            combined.values(), key=lambda hit: (-hit["score"], hit["chat_id"], hit["message_id"])
        )
        result.update(
            results=hits[:limit], has_more=len(hits) > limit or result["has_more"], tab=tab
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
        return result
