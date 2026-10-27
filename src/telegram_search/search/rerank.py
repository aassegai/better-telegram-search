"""Rerank bounded, already filtered text/OCR branches before modality RRF."""

import hashlib
import time
from collections import OrderedDict

from telegram_search.search.cache import SearchCache
from telegram_search.search.execution import advance, checkpoint, phase, search_lock
from telegram_search.shared.errors import UserError


class SearchReranker:
    def __init__(self, db, service):
        self.db, self.service = db, service
        self.cache = OrderedDict()
        self.cache_bytes = 0

    def _documents(self, hits, kind, filters):
        documents = []
        where, params = filters.sql()
        with self.db.connect() as conn:
            conn.execute("BEGIN")
            for hit in hits:
                checkpoint()
                row = conn.execute(
                    f"SELECT m.text FROM indexable_messages m WHERE {where} "
                    "AND m.chat_id=? AND m.message_id=?",
                    [*params, hit["chat_id"], hit["message_id"]],
                ).fetchone()
                if row is None:
                    raise UserError("Архив изменился во время поиска. Повторите запрос.")
                if kind == "ocr":
                    text = hit.get("ocr_text") or ""
                    span = hit.get("ocr_range")
                    if span:
                        text = text[span["char_start"] : span["char_end"]]
                elif hit.get("chunk_id"):
                    row = conn.execute(
                        "SELECT c.text FROM chunks c JOIN index_segments s ON s.chat_id=c.chat_id "
                        "AND s.utc_day=c.utc_day JOIN semantic_state st ON st.id=1 "
                        "WHERE c.id=? AND c.generation=s.target_generation "
                        "AND c.embedding_space_id=st.active_space_id "
                        "AND s.dense_generation=s.target_generation",
                        (hit["chunk_id"],),
                    ).fetchone()
                    if row is None:
                        raise UserError("Архив изменился во время поиска. Повторите запрос.")
                    text = row[0]
                else:
                    text = row[0]
                documents.append(text)
        return documents

    def _remember(self, key, vector):
        import numpy as np

        value = np.array(vector, dtype=np.float32, copy=True)
        if key in self.cache:
            self.cache_bytes -= self.cache.pop(key).nbytes
        self.cache[key] = value
        self.cache_bytes += value.nbytes
        maximum = min(16 * 1024**2, self.db.settings.memory_limit_mib * 1024**2 // 100)
        while self.cache and (len(self.cache) > 2048 or self.cache_bytes > maximum):
            self.cache_bytes -= self.cache.popitem(last=False)[1].nbytes

    def _vectors(self, encoder, texts, purpose, revision):
        import numpy as np

        keys = [
            (
                self.service.spec.identity,
                revision,
                purpose,
                hashlib.sha256(text.encode()).hexdigest(),
            )
            for text in texts
        ]
        missing = list(dict.fromkeys(key for key in keys if key not in self.cache))
        phase("rerank_" + purpose, total=len(missing), unit="texts")
        vectors = {key: self.cache[key] for key in keys if key in self.cache}
        for key in vectors:
            self.cache.move_to_end(key)
        lookup = dict(zip(keys, texts, strict=True))
        batch_size = min(self.db.settings.embedding_batch, getattr(encoder, "max_batch", 4), 4)
        for start in range(0, len(missing), batch_size):
            checkpoint()
            batch = missing[start : start + batch_size]
            values = encoder.encode_text([lookup[k] for k in batch], purpose, interactive=True)
            checkpoint()
            advance(len(batch))
            for key, vector in zip(batch, values, strict=True):
                vectors[key] = vector
                self._remember(key, vector)
        return np.stack([vectors[key] for key in keys])

    def apply(self, query, hits, kind, filters, *, exact=False):
        from telegram_search.config.model_registry import rerank_spec

        settings = self.db.settings
        expected_identity = rerank_spec(settings.rerank_profile, self.db.workspace).identity
        metadata = {"kind": kind, "applied": False, "reason": "disabled", "candidates": 0}
        if not settings.giga_rerank_enabled:
            return hits, metadata
        if kind == "images" or exact:
            metadata["reason"] = "images_only" if kind == "images" else "exact_phrase"
            return hits, metadata
        if not hits:
            metadata["reason"] = "no_candidates"
            return hits, metadata
        revision = SearchCache.revision(self.db, settings=settings)
        selected = hits[: min(50, self.db.settings.retrieval_candidates)]
        documents = self._documents(selected, kind, filters)
        started = time.perf_counter()
        with search_lock(self.service.lock):
            try:
                import numpy as np

                if (
                    self.service.spec.identity != expected_identity
                    or SearchCache.revision(self.db) != revision
                ):
                    raise UserError("Архив изменился во время поиска. Повторите запрос.")
                encoder = self.service.get_encoder()
                # A reranker is optional: refuse a new session when it would exceed
                # the shared application RAM budget, rather than disrupt base search.
                self.service.check_memory(encoder)
                self.service.mark_used()
                if hasattr(encoder, "score_pairs"):
                    scores, truncated, query_truncated = self._pair_scores(
                        encoder, query, documents, revision
                    )
                else:
                    prefix = self.service.spec.manifest["query_prefix"]
                    truncated = sum(encoder.tokenizer.count(text) > 512 for text in documents)
                    query_truncated = encoder.tokenizer.count(prefix + query) > 512
                    vector = self._vectors(encoder, [query], "query", revision)[0]
                    values = self._vectors(encoder, documents, "passage", revision)
                    scores = values.astype(np.float32) @ vector.astype(np.float32)
                if not np.isfinite(scores).all():
                    raise UserError("Модель переранжирования вернула недопустимые оценки.")
                order = sorted(
                    range(len(selected)),
                    key=lambda i: (
                        -float(scores[i]),
                        i,
                        selected[i]["chat_id"],
                        selected[i]["message_id"],
                    ),
                )
                ranked = [{**selected[i], "rerank_score": float(scores[i])} for i in order]
                metadata.update(
                    applied=True,
                    reason=None,
                    candidates=len(selected),
                    model_id=self.service.spec.model_id,
                    profile=self.service.spec.profile,
                    manifest_id=self.service.spec.identity,
                    truncated_candidates=truncated,
                    query_truncated=query_truncated,
                    seconds=time.perf_counter() - started,
                )
            except Exception as exc:
                metadata.update(
                    reason=str(exc)
                    if isinstance(exc, UserError)
                    else "Переранжирование недоступно. Базовый порядок сохранён.",
                    seconds=time.perf_counter() - started,
                )
                if self.service.encoder:
                    self.service.encoder.unload()
                ranked = selected
        # Never relax filters/exclusions to recover from an archive change during inference.
        if SearchCache.revision(self.db) != revision:
            raise UserError("Архив изменился во время поиска. Повторите запрос.")
        self._documents(selected, kind, filters)
        return [*ranked, *hits[len(selected) :]], metadata

    def _pair_scores(self, encoder, query, texts, revision):
        import numpy as np

        from telegram_search.inference.rerank_contract import validate_scores

        query_hash = hashlib.sha256(query.encode()).hexdigest()
        keys = [
            (
                self.service.spec.identity,
                revision,
                "pair:" + query_hash,
                hashlib.sha256(text.encode()).hexdigest(),
            )
            for text in texts
        ]
        missing = list(dict.fromkeys(key for key in keys if key not in self.cache))
        values = {key: self.cache[key] for key in keys if key in self.cache}
        for key in values:
            self.cache.move_to_end(key)
        lookup = dict(zip(keys, texts, strict=True))
        phase("rerank_passage", total=len(missing), unit="texts")
        batch_size = min(self.db.settings.embedding_batch, getattr(encoder, "max_batch", 4), 4)
        for start in range(0, len(missing), batch_size):
            checkpoint()
            batch = missing[start : start + batch_size]
            ids = [str(i) for i in range(len(batch))]
            rows = validate_scores(
                ids,
                encoder.score_pairs(
                    query,
                    [
                        {"candidate_id": key, "text": lookup[item]}
                        for key, item in zip(ids, batch, strict=True)
                    ],
                ),
            )
            checkpoint()
            for key, row in zip(batch, rows, strict=True):
                values[key] = np.array(
                    [row["score"], row["truncated"], row["query_truncated"]], dtype=np.float32
                )
                self._remember(key, values[key])
            advance(len(batch))
        result = [values[key] for key in keys]
        return (
            np.array([value[0] for value in result], dtype=np.float32),
            sum(bool(value[1]) for value in result),
            any(bool(value[2]) for value in result),
        )
