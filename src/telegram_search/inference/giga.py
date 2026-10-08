"""Bidirectional Giga encoder used only for bounded query-time reranking."""

import time

from telegram_search.inference.e5 import E5Encoder
from telegram_search.shared.errors import UserError


class GigaEncoder(E5Encoder):
    def prepare_text(self, texts, purpose):
        if purpose not in {"query", "passage"} or not 1 <= len(texts) <= 128:
            raise UserError("Недопустимый батч модели.")
        started = time.perf_counter()
        prefix = self.spec.manifest[f"{purpose}_prefix"]
        inputs = self.tokenizer.batch([prefix + text for text in texts], 512, truncate=True)
        return inputs, len(texts), purpose, time.perf_counter() - started, self
