from dataclasses import replace
from datetime import UTC, datetime

from telegram_search.search.chunking.builder import EpisodeBuilder
from telegram_search.search.chunking.classification import classify
from telegram_search.search.chunking.policy import DEFAULT_POLICY, ChunkPolicy
from telegram_search.search.chunks import ChunkBuilder, SourceMessage


class Words:
    def count(self, text):
        return len(text.split()) + 2


class Characters:
    def count(self, text):
        return len(text) + 2


def message(mid, text, **values):
    return SourceMessage("chat", mid, 1750000000 + mid, "Автор", text, **values)


def test_eight_meaningful_messages_with_retained_support_using_token_budget():
    items = []
    for index in range(9):
        items.extend(
            [
                message(index * 2, "Содержательное сообщение номер " + str(index)),
                message(index * 2 + 1, "да"),
            ]
        )
    chunks = list(EpisodeBuilder(Words()).build(items, 1, "space"))
    assert len(chunks) == 2
    assert sum(classify(items[p.message_id], DEFAULT_POLICY).counts for p in chunks[0].parts) == 8
    assert len(chunks[0].parts) == 16
    assert "\nАвтор: да" in chunks[0].text
    assert chunks[0].tokens == Words().count("passage: " + chunks[0].text)
    assert all(chunk.tokens <= 480 for chunk in chunks)


def test_support_terms_numbers_links_and_emoji_do_not_spend_message_slots():
    for text in (
        "да",
        "нет",
        "ок",
        "ага",
        "угу",
        "CUDA",
        "4060",
        "8ГБ",
        "https://example.org/a?q=4",
        "👍",
    ):
        result = classify(message(1, text), DEFAULT_POLICY)
        assert result.include and not result.counts
    assert classify(message(1, "Не работает"), DEFAULT_POLICY).counts
    assert classify(message(1, "Содержательная подпись", has_photo=True), DEFAULT_POLICY).counts


def test_support_cannot_bypass_hard_token_or_raw_message_limits():
    items = [message(i, "CUDA") for i in range(300)]
    chunks = list(EpisodeBuilder(Characters()).build(items, 1, "space"))
    assert len(chunks) > 1
    assert all(chunk.tokens <= 480 and len(chunk.source_parts) <= 32 for chunk in chunks)
    assert {p.message_id for chunk in chunks for p in chunk.parts} == set(range(300))


def test_approved_noise_is_whole_message_only_and_stays_in_source_lexical_ranges():
    items = [
        message(1, "Нужно восстановить индекс"),
        message(2, "БЛЯДЬ!!!"),
        message(3, "сука не работает"),
        message(4, "", has_photo=True),
    ]
    chunk = list(EpisodeBuilder(Words()).build(items, 1, "space"))[0]
    assert "БЛЯДЬ" not in chunk.text and "сука не работает" in chunk.text
    assert "БЛЯДЬ!!!" in chunk.lexical_text
    assert [p.message_id for p in chunk.source_parts] == [1, 2, 3, 4]
    assert [p.message_id for p in chunk.parts] == [1, 3]
    assert list(EpisodeBuilder(Words()).build([message(1, "сука")], 1, "space")) == []
    assert list(EpisodeBuilder(Words()).build([message(1, "", has_photo=True)], 1, "space")) == []
    assert (
        "сука"
        in list(
            ChunkBuilder(Words(), max_messages=1, overlap=0).build(
                [message(1, "сука")], 1, "ocr-space"
            )
        )[0].text
    )


def test_question_reply_exception_cycles_missing_deleted_and_cross_chat_parents():
    parent = message(1, "Как назывался фильм?", reply_to=2, content_hash="revision1")
    answer = message(2, "Пиздец", reply_to=1)

    def lookup(chat, mid):
        return {1: parent, 2: answer}.get(mid)

    chunks = list(EpisodeBuilder(Words(), lookup=lookup).build([answer], 1, "space"))
    assert len(chunks) == 1 and "Как назывался фильм?" in chunks[0].text
    assert len(chunks[0].parts) == 2
    assert chunks[0].parts[0].role == "borrowed_context"
    assert chunks[0].source_parts[0].message_id == 2
    for unavailable in (
        None,
        replace(parent, remote_deleted=True),
        replace(parent, chat_id="other"),
    ):
        builder = EpisodeBuilder(
            Words(), lookup=lambda chat, mid, unavailable=unavailable: unavailable
        )
        assert list(builder.build([message(3, "да", reply_to=1)], 1, "space")) == []


def test_long_unicode_ranges_cover_source_with_bounded_overlap_and_prefix():
    text = "Абзац с отрицанием: не работает 👩🏽‍💻. Проверяем дальше.\n\n" * 300
    source = message(1, text)
    chunks = list(
        EpisodeBuilder(Characters(), passage_prefix="search_document: ").build([source], 2, "space")
    )
    cursor = 0
    for chunk in chunks:
        part = chunk.parts[-1]
        assert part.start <= cursor < part.end
        cursor = part.end
        assert chunk.tokens == Characters().count("search_document: " + chunk.text) <= 480
    assert cursor == len(text)
    assert chunks[1].parts[-1].start < chunks[0].parts[-1].end


def test_policy_identity_is_document_only_and_deterministic():
    assert ChunkPolicy.from_json(DEFAULT_POLICY.json) == DEFAULT_POLICY
    assert (
        replace(DEFAULT_POLICY, filler=DEFAULT_POLICY.filler + ("synthetic",)).identity
        != DEFAULT_POLICY.identity
    )
    source = [message(1, "Проверка политики чанкинга")]
    assert list(EpisodeBuilder(Words()).build(source, 1, "space")) == list(
        EpisodeBuilder(Words()).build(source, 1, "space")
    )
    assert (
        list(EpisodeBuilder(Words()).build(source, 2, "space"))[0].id
        != list(EpisodeBuilder(Words()).build(source, 1, "space"))[0].id
    )


def test_cross_day_reply_keeps_owner_day_and_neighbor_is_explicit():
    midnight = int(datetime(2025, 6, 16, tzinfo=UTC).timestamp())
    parent = replace(
        message(1, "Встреча завтра состоится?"), timestamp=midnight - 1, content_hash="p1"
    )
    answer = replace(message(2, "да", reply_to=1), timestamp=midnight + 1)
    chunk = list(
        EpisodeBuilder(Words(), lookup=lambda chat, mid: parent).build([answer], 1, "space")
    )[0]
    assert chunk.day == "2025-06-16"
    assert chunk.dependencies == ((1, "p1"),)
    answer = replace(answer, reply_to=None)
    chunk = list(
        EpisodeBuilder(Words(), neighbor_lookup=lambda msg: parent).build([answer], 1, "space")
    )[0]
    assert "Соседний контекст" in chunk.text and "Ответ на сообщение" not in chunk.text
