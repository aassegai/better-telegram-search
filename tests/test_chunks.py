from datetime import UTC, datetime

from telegram_search.search.chunks import ChunkBuilder, SourceMessage


class CharacterTokens:
    """A conservative tokenizer double; production uses the pinned model tokenizer."""

    def count(self, text):
        return len(text) + 2


def source(mid, text="реплика", timestamp=1750000000, chat="a", **kwargs):
    return SourceMessage(chat, mid, timestamp, "Автор", text, **kwargs)


def test_bounded_windows_overlap_and_stable_local_ids():
    builder = ChunkBuilder(CharacterTokens())
    messages = [source(i) for i in range(1, 19)]
    chunks = list(builder.build(messages, 1, "synthetic"))
    assert [len(chunk.parts) for chunk in chunks] == [8, 8, 8, 6]
    assert set(part.message_id for part in chunks[0].parts) & set(
        part.message_id for part in chunks[1].parts
    ) == {5, 6, 7, 8}
    assert all(chunk.tokens <= 480 for chunk in chunks)
    assert set(part.message_id for chunk in chunks for part in chunk.parts) == set(range(1, 19))
    earlier = [source(0, timestamp=1749913600), *messages]
    augmented = list(builder.build(earlier, 1, "synthetic"))
    assert [chunk.id for chunk in augmented[1:]] == [chunk.id for chunk in chunks]
    assert list(builder.build(messages, 2, "synthetic"))[0].id != chunks[0].id


def test_long_unicode_message_ranges_cover_every_character_without_truncation():
    text = "Очень длинная реплика 👩🏽‍💻 с пробелами\n" * 400
    chunks = list(ChunkBuilder(CharacterTokens()).build([source(9, text)], 1, "synthetic"))
    ranges = [chunk.parts[0] for chunk in chunks]
    assert len(ranges) > 10
    assert ranges[0].start == 0 and ranges[-1].end == len(text)
    assert all(a.end == b.start for a, b in zip(ranges, ranges[1:], strict=False))
    assert "".join(text[part.start : part.end] for part in ranges) == text
    assert all(chunk.tokens <= 480 and len(chunk.parts) == 1 for chunk in chunks)


def test_day_gap_chat_and_service_boundaries():
    midnight = int(datetime(2025, 6, 16, tzinfo=UTC).timestamp())
    messages = [
        source(1, timestamp=midnight - 10),
        source(2, timestamp=midnight),
        source(3, timestamp=midnight + 3601),
        source(4, timestamp=midnight + 3602, kind="service"),
        source(5, timestamp=midnight + 3603, chat="b"),
    ]
    chunks = list(ChunkBuilder(CharacterTokens()).build(messages, 1, "synthetic"))
    assert len(chunks) == 4
    assert [tuple(part.message_id for part in chunk.parts) for chunk in chunks] == [
        (1,),
        (2,),
        (3,),
        (5,),
    ]
    assert chunks[0].day != chunks[1].day


def test_budget_can_reduce_overlap_and_huge_author_cannot_break_limit():
    builder = ChunkBuilder(CharacterTokens(), max_tokens=100)
    items = [source(i, "слова " * 10) for i in range(1, 8)]
    chunks = list(builder.build(items, 1, "synthetic"))
    assert all(chunk.tokens <= 100 for chunk in chunks)
    assert {part.message_id for chunk in chunks for part in chunk.parts} == set(range(1, 8))
    huge_author = SourceMessage("a", 20, 1750000000, "автор" * 1000, "текст" * 100)
    chunks = list(builder.build([huge_author], 1, "synthetic"))
    assert all(chunk.tokens <= 100 for chunk in chunks)


def test_deleted_and_live_messages_never_share_window_or_overlap():
    messages = [source(i, remote_deleted=6 <= i <= 11) for i in range(1, 19)]
    chunks = list(ChunkBuilder(CharacterTokens()).build(messages, 1, "synthetic"))
    assert {part.message_id for chunk in chunks for part in chunk.parts} == set(range(1, 19))
    for chunk in chunks:
        assert len({6 <= part.message_id <= 11 for part in chunk.parts}) == 1
