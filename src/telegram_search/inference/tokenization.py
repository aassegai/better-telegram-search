import json
import threading
from pathlib import Path

from telegram_search.shared.errors import UserError


class ModelTokenizer:
    def __init__(self, bundle: Path):
        from tokenizers import Tokenizer

        self.tokenizer = Tokenizer.from_file(str(bundle / "tokenizer.json"))
        self.tokenizer.no_truncation()
        self.tokenizer.no_padding()
        config = json.loads((bundle / "tokenizer_config.json").read_text())
        pad = config["pad_token"]
        self.pad_id = self.tokenizer.token_to_id(pad if isinstance(pad, str) else pad["content"])
        if self.pad_id is None:
            raise UserError("В закреплённом tokenizer отсутствует padding token.")
        self.lock = threading.RLock()

    def count(self, text: str) -> int:
        with self.lock:
            return len(self.tokenizer.encode(text, add_special_tokens=True).ids)

    def batch(self, texts: list[str], limit: int = 512):
        import numpy as np

        with self.lock:
            encodings = self.tokenizer.encode_batch(texts, add_special_tokens=True)
        length = max((len(item.ids) for item in encodings), default=0)
        if length > limit:
            raise UserError(
                "Текст превышает лимит модели. Уточните запрос или пересоберите chunks."
            )
        shape = (len(encodings), length)
        ids = np.full(shape, self.pad_id, dtype=np.int64)
        mask = np.zeros(shape, dtype=np.int64)
        types = np.zeros(shape, dtype=np.int64)
        for index, item in enumerate(encodings):
            size = len(item.ids)
            ids[index, :size] = item.ids
            mask[index, :size] = item.attention_mask
            types[index, :size] = item.type_ids
        return {"input_ids": ids, "attention_mask": mask, "token_type_ids": types}
