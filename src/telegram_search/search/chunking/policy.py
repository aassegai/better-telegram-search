"""Frozen document policy; execution devices and batch sizes never enter its identity."""

import hashlib
from dataclasses import asdict, dataclass

from telegram_search.shared.text import serialize

APPROVED_FILLER = tuple(
    sorted(
        {
            "бля",
            "блядь",
            "блять",
            "пиздец",
            "пздц",
            "ппц",
            "сука",
            "ебать",
            "ебануться",
            "нахуй",
            "охуеть",
            "ахаха",
            "ахах",
            "хаха",
            "хах",
            "лол",
            "lol",
            "lmao",
            "rofl",
            "кек",
            "kek",
            "хех",
            "сук",
            "cyka",
            "сууука",
            "суууука",
            "пиздос",
            "гг",
            "ггг",
            "ыыы",
        }
    )
)
LEGACY_POLICY_ID = "telegram-windows-v1"


@dataclass(frozen=True)
class ChunkPolicy:
    version: str = "telegram-episodes-v2"
    max_messages: int = 8
    max_tokens: int = 480
    overlap_messages: int = 4
    max_source_messages: int = 32
    gap_seconds: int = 3600
    turn_seconds: int = 60
    parent_depth: int = 2
    parent_max_age_seconds: int = 2592000
    context_tokens: int = 128
    neighbor_seconds: int = 120
    long_overlap_tokens: int = 48
    filler: tuple[str, ...] = APPROVED_FILLER
    normalization: str = "whole-unicode-word-nfkc-casefold-yo-v1"
    support_count: int = 0
    count_meaningful_captions: bool = True
    bot_commands: str = "disabled"

    @property
    def identity(self):
        return hashlib.sha256(serialize(asdict(self)).encode()).hexdigest()

    @property
    def json(self):
        return serialize(asdict(self))

    @classmethod
    def from_json(cls, value):
        import json

        data = json.loads(value)
        data["filler"] = tuple(data["filler"])
        policy = cls(**data)
        if policy.version != "telegram-episodes-v2" or policy.bot_commands != "disabled":
            raise ValueError("Unsupported document policy")
        if not (1 <= policy.max_messages <= 8 and 32 <= policy.max_tokens <= 480):
            raise ValueError("Invalid document budget")
        if not 0 <= policy.overlap_messages < policy.max_messages:
            raise ValueError("Invalid document overlap")
        if not (policy.max_messages <= policy.max_source_messages <= 32):
            raise ValueError("Invalid source buffer")
        if not (0 <= policy.parent_depth <= 2 and 0 <= policy.context_tokens <= 128):
            raise ValueError("Invalid context budget")
        return policy


DEFAULT_POLICY = ChunkPolicy()
