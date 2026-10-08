import hashlib
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Protocol

from telegram_search.shared.errors import UserError
from telegram_search.shared.text import serialize
from telegram_search.storage.generations import utc_day

PREPROCESSING_VERSION = "telegram-windows-v1"


class TokenCounter(Protocol):
    def count(self, text: str) -> int: ...


@dataclass(frozen=True)
class SourceMessage:
    chat_id: str
    message_id: int
    timestamp: int
    author: str
    text: str
    has_photo: bool = False
    kind: str = "message"
    remote_deleted: bool = False


@dataclass(frozen=True)
class TextPart:
    message_id: int
    start: int
    end: int


@dataclass(frozen=True)
class Chunk:
    id: str
    chat_id: str
    day: str
    generation: int
    text: str
    tokens: int
    parts: tuple[TextPart, ...]


@dataclass(frozen=True)
class _Piece:
    message: SourceMessage
    part: TextPart
    text: str
    split: bool


class ChunkBuilder:
    """Stream bounded conversation windows; ranges refer to original Unicode text."""

    def __init__(
        self,
        tokenizer: TokenCounter,
        max_tokens: int = 480,
        max_messages: int = 8,
        overlap: int = 4,
        gap_seconds: int = 3600,
    ):
        if max_tokens < 32 or not 0 <= overlap < max_messages or gap_seconds < 1:
            raise ValueError("Invalid chunk configuration")
        self.tokenizer = tokenizer
        self.max_tokens = max_tokens
        self.max_messages = max_messages
        self.overlap = overlap
        self.gap_seconds = gap_seconds

    def count(self, text: str) -> int:
        return self.tokenizer.count("passage: " + text)

    def _prefix_that_fits(self, text: str, render, limit: int) -> int:
        # Grow a small character slice first, avoiding repeated scans of a long message.
        high = min(len(text), 2048)
        while high < len(text) and self.tokenizer.count(render(text[:high])) <= limit:
            high = min(len(text), high * 2)
        if self.tokenizer.count(render(text[:high])) <= limit:
            return high
        low = 0
        while low < high:
            middle = (low + high + 1) // 2
            if self.tokenizer.count(render(text[:middle])) <= limit:
                low = middle
            else:
                high = middle - 1
        return low

    def _pieces(self, message: SourceMessage) -> Iterator[_Piece]:
        author = message.author or "Участник"
        # Author metadata cannot consume the budget of an entire message.
        author = author[: self._prefix_that_fits(author, lambda value: value, 64)]
        suffix = " [фото]" if message.has_photo else ""

        def render(text):
            return f"{author}: {text}{suffix}"

        if self.count(render(message.text)) <= self.max_tokens:
            yield _Piece(
                message,
                TextPart(message.message_id, 0, len(message.text)),
                render(message.text),
                False,
            )
            return
        start = 0
        while start < len(message.text):
            remaining = message.text[start:]
            length = self._prefix_that_fits(
                remaining, lambda value: "passage: " + render(value), self.max_tokens
            )
            if length < 1:
                raise UserError("Не удалось вместить часть сообщения в токенный бюджет.")
            end = start + length
            piece = render(message.text[start:end])
            if self.count(piece) > self.max_tokens:
                raise UserError("Токенизатор превысил бюджет части сообщения.")
            yield _Piece(message, TextPart(message.message_id, start, end), piece, True)
            start = end

    def _chunk(self, pieces: list[_Piece], generation: int, embedding_space: str) -> Chunk:
        text = "\n".join(piece.text for piece in pieces)
        parts = tuple(piece.part for piece in pieces)
        first = pieces[0].message
        day = utc_day(first.timestamp)
        identity = serialize(
            [
                PREPROCESSING_VERSION,
                embedding_space,
                first.chat_id,
                day,
                generation,
                [(part.message_id, part.start, part.end) for part in parts],
                hashlib.sha256(text.encode()).hexdigest(),
            ]
        )
        return Chunk(
            hashlib.sha256(identity.encode()).hexdigest(),
            first.chat_id,
            day,
            generation,
            text,
            self.count(text),
            parts,
        )

    def build(
        self, messages: Iterable[SourceMessage], generation: int, embedding_space: str
    ) -> Iterator[Chunk]:
        window: list[_Piece] = []
        last: SourceMessage | None = None
        previous_indexed: SourceMessage | None = None
        has_new = False
        for message in messages:
            if last and (message.chat_id, message.timestamp, message.message_id) < (
                last.chat_id,
                last.timestamp,
                last.message_id,
            ):
                raise ValueError("Messages must be ordered by chat, timestamp and ID")
            last = message
            if message.kind != "message" or not (message.text.strip() or message.has_photo):
                continue
            if previous_indexed and (
                message.chat_id != previous_indexed.chat_id
                or bool(message.remote_deleted) != bool(previous_indexed.remote_deleted)
                or utc_day(message.timestamp) != utc_day(previous_indexed.timestamp)
                or message.timestamp - previous_indexed.timestamp > self.gap_seconds
            ):
                if window and has_new:
                    yield self._chunk(window, generation, embedding_space)
                window, has_new = [], False
            previous_indexed = message
            for piece in self._pieces(message):
                if piece.split:
                    if window and has_new:
                        yield self._chunk(window, generation, embedding_space)
                    window, has_new = [], False
                    yield self._chunk([piece], generation, embedding_space)
                    continue
                combined = "\n".join(item.text for item in [*window, piece])
                if window and (
                    len(window) >= self.max_messages or self.count(combined) > self.max_tokens
                ):
                    if has_new:
                        yield self._chunk(window, generation, embedding_space)
                    window = window[-self.overlap :] if self.overlap else []
                    # Overlap is best effort: a new message takes priority over old context.
                    while (
                        window
                        and self.count("\n".join(item.text for item in [*window, piece]))
                        > self.max_tokens
                    ):
                        window.pop(0)
                    has_new = False
                window.append(piece)
                has_new = True
        if window and has_new:
            yield self._chunk(window, generation, embedding_space)
