"""Bounded conversation episodes with separate lexical and model provenance."""

import hashlib
import re
from dataclasses import dataclass, replace

from telegram_search.search.chunking.classification import classify
from telegram_search.search.chunking.legacy import Chunk, ChunkBuilder, SourceMessage, TextPart
from telegram_search.search.chunking.policy import DEFAULT_POLICY
from telegram_search.shared.errors import UserError
from telegram_search.shared.text import serialize
from telegram_search.storage.generations import utc_day


@dataclass(frozen=True)
class Entry:
    message: SourceMessage
    part: TextPart
    counts: bool
    include: bool
    context_kind: str = "reply"


class EpisodeBuilder(ChunkBuilder):
    def __init__(
        self,
        tokenizer,
        policy=DEFAULT_POLICY,
        *,
        passage_prefix="passage: ",
        lookup=None,
        neighbor_lookup=None,
    ):
        super().__init__(
            tokenizer,
            policy.max_tokens,
            policy.max_messages,
            policy.overlap_messages,
            policy.gap_seconds,
            passage_prefix,
        )
        self.policy = policy
        self.lookup = lookup or (lambda chat_id, message_id: None)
        self.neighbor_lookup = neighbor_lookup or (lambda message: None)
        self.skipped_messages = 0
        self.source_messages = 0

    def _render(self, entries, borrowed=()):
        lines = []
        previous = None
        for entry in [*borrowed, *entries]:
            if not entry.include:
                continue
            message, part = entry.message, entry.part
            author = (message.author or "Участник")[:128]
            author = author[: self._prefix_that_fits(author, lambda value: value, 48)]
            body = message.text[part.start : part.end]
            same_turn = (
                previous
                and previous.message.author_id is not None
                and previous.message.author_id == message.author_id
                and 0 <= message.timestamp - previous.message.timestamp <= self.policy.turn_seconds
                and part.role != "borrowed_context"
                and not message.reply_to
            )
            if part.role == "borrowed_context":
                label = (
                    "Ответ на сообщение" if entry.context_kind == "reply" else "Соседний контекст"
                )
                lines.append(f"{label} ({author}): {body}")
            else:
                lines.append(body if same_turn else f"{author}: {body}")
            previous = entry
        return "\n".join(lines)

    def _parents(self, entry, known):
        message = entry.message
        parents, dependencies = [], {}
        visited = {message.message_id}
        current = message.reply_to
        neighbor = None
        if current is None:
            neighbor = self.neighbor_lookup(message)
            current = neighbor.message_id if neighbor else None
        for _ in range(self.policy.parent_depth):
            if current is None or current in visited:
                break
            visited.add(current)
            parent = neighbor if neighbor is not None else self.lookup(message.chat_id, current)
            dependencies[current] = parent.content_hash if parent else None
            if (
                parent is None
                or parent.chat_id != message.chat_id
                or parent.remote_deleted
                or parent.kind != "message"
                or abs(parent.timestamp - message.timestamp) > self.policy.parent_max_age_seconds
            ):
                break
            if current not in known and parent.text.strip():
                candidate = Entry(
                    parent,
                    TextPart(current, 0, len(parent.text), "borrowed_context"),
                    False,
                    True,
                    "neighbor" if neighbor else "reply",
                )

                # Borrow a bounded original range, with no hidden truncation of core text.
                def render(value, candidate=candidate, parent=parent):
                    part = replace(candidate.part, end=len(value))
                    shortened = replace(candidate, message=replace(parent, text=value), part=part)
                    return self.passage_prefix + self._render([], [*parents, shortened])

                end = self._prefix_that_fits(parent.text, render, self.policy.context_tokens)
                if end:
                    parents.append(replace(candidate, part=replace(candidate.part, end=end)))
            current = parent.reply_to if neighbor is None else None
        return parents, dependencies

    def _make(self, entries, generation, space):
        included = [entry for entry in entries if entry.include]
        if not included:
            return None
        known = {entry.message.message_id for entry in entries}
        borrowed, dependencies = [], {}
        # Borrow for short replies/terms; never let arbitrary ancestry grow a window.
        for entry in included:
            if entry.counts:
                continue
            parents, deps = self._parents(entry, known)
            dependencies.update(deps)
            for parent in parents:
                if parent.message.message_id not in known:
                    borrowed.append(parent)
                    known.add(parent.message.message_id)
            if borrowed:
                break
        # Full owned text has priority over all borrowed context.
        while borrowed and self.count(self._render(entries, borrowed)) > self.max_tokens:
            borrowed.pop()
        if not any(entry.part.role == "core" for entry in included) and not borrowed:
            # Isolated support-only episodes stay available in message FTS.
            return None
        text = self._render(entries, borrowed)
        if self.count(text) > self.max_tokens:
            raise UserError("Токенизатор превысил бюджет чанка.")
        first = entries[0].message
        parts = tuple(entry.part for entry in [*borrowed, *included])
        source_parts = tuple(entry.part for entry in entries)
        lexical = "\n".join(
            f"{entry.message.author}: {entry.message.text[entry.part.start : entry.part.end]}"
            for entry in entries
        )
        identity = serialize(
            [
                self.policy.identity,
                space,
                first.chat_id,
                utc_day(first.timestamp),
                generation,
                [(p.message_id, p.start, p.end, p.role) for p in parts],
                [(p.message_id, p.start, p.end, p.role) for p in source_parts],
                text,
                dependencies,
            ]
        )
        return Chunk(
            hashlib.sha256(identity.encode()).hexdigest(),
            first.chat_id,
            utc_day(first.timestamp),
            generation,
            text,
            self.count(text),
            parts,
            source_parts,
            lexical,
            self.policy.identity,
            tuple(sorted(dependencies.items())),
        )

    def _long_parts(self, entry):
        message = entry.message
        start = 0
        while start < len(message.text):
            tail = message.text[start:]
            length = self._prefix_that_fits(
                tail,
                lambda value: (
                    self.passage_prefix
                    + self._render(
                        [
                            replace(
                                entry,
                                message=replace(message, text=value),
                                part=replace(entry.part, start=0, end=len(value)),
                            )
                        ]
                    )
                ),
                self.max_tokens,
            )
            if length < 1:
                raise UserError("Не удалось вместить часть сообщения в токенный бюджет.")
            if length < len(tail):
                # Paragraph, sentence, word, then original Unicode character boundary.
                minimum = max(1, length // 2)
                for pattern in (r"\n\s*\n", r"[.!?…]+\s+", r"\s+"):
                    matches = list(re.finditer(pattern, tail[:length]))
                    choices = [match.end() for match in matches if match.end() >= minimum]
                    if choices:
                        length = choices[-1]
                        break
            end = start + length
            yield replace(entry, part=replace(entry.part, start=start, end=end))
            if end == len(message.text):
                break
            overlap_start = max(start + 1, end - 512)
            while (
                overlap_start < end
                and self.tokenizer.count(message.text[overlap_start:end])
                > self.policy.long_overlap_tokens
            ):
                overlap_start += 1
            start = overlap_start

    def _overlap(self, entries):
        kept, count = [], 0
        for entry in reversed(entries):
            if entry.counts and count == self.overlap:
                break
            kept.append(entry)
            count += int(entry.counts)
            if len(kept) >= self.policy.max_source_messages // 2:
                break
        return list(reversed(kept)) if self.overlap else []

    def build(self, messages, generation, embedding_space):
        window, last, fresh = [], None, False
        self.skipped_messages = self.source_messages = 0
        for message in messages:
            if last and (message.chat_id, message.timestamp, message.message_id) < (
                last.chat_id,
                last.timestamp,
                last.message_id,
            ):
                raise ValueError("Messages must be ordered by chat, timestamp and ID")
            boundary = last and (
                message.chat_id != last.chat_id
                or utc_day(message.timestamp) != utc_day(last.timestamp)
                or message.remote_deleted != last.remote_deleted
                or message.timestamp - last.timestamp > self.gap_seconds
            )
            if boundary:
                chunk = (
                    self._make(window, generation, embedding_space) if window and fresh else None
                )
                if chunk:
                    yield chunk
                window, fresh = [], False
            last = message
            parent = (
                self.lookup(message.chat_id, message.reply_to)
                if message.reply_to is not None
                else None
            )
            classification = classify(message, self.policy, parent)
            self.source_messages += 1
            self.skipped_messages += int(not classification.include)
            if classification.role == "service":
                continue
            entry = Entry(
                message,
                TextPart(message.message_id, 0, len(message.text), classification.role),
                classification.counts,
                classification.include,
            )
            if classification.include and self.count(self._render([entry])) > self.max_tokens:
                chunk = (
                    self._make(window, generation, embedding_space) if window and fresh else None
                )
                if chunk:
                    yield chunk
                window, fresh = [], False
                for part in self._long_parts(entry):
                    chunk = self._make([part], generation, embedding_space)
                    if chunk:
                        yield chunk
                continue
            combined = [*window, entry]
            if window and (
                sum(int(item.counts) for item in combined) > self.max_messages
                or len(combined) > self.policy.max_source_messages
                or self.count(self._render(combined)) > self.max_tokens
            ):
                chunk = self._make(window, generation, embedding_space) if fresh else None
                if chunk:
                    yield chunk
                window = self._overlap(window)
                while window and (
                    self.count(self._render([*window, entry])) > self.max_tokens
                    or sum(int(item.counts) for item in [*window, entry]) > self.max_messages
                ):
                    window.pop(0)
                fresh = False
            window.append(entry)
            fresh = True
        chunk = self._make(window, generation, embedding_space) if window and fresh else None
        if chunk:
            yield chunk
