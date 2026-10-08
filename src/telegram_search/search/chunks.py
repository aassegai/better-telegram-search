"""Compatible facade for legacy windows; v2 episodes require an explicit frozen policy."""

from telegram_search.search.chunking.legacy import (
    PREPROCESSING_VERSION,
    Chunk,
    ChunkBuilder,
    SourceMessage,
    TextPart,
    TokenCounter,
)

__all__ = [
    "PREPROCESSING_VERSION",
    "Chunk",
    "ChunkBuilder",
    "SourceMessage",
    "TextPart",
    "TokenCounter",
]
