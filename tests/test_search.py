import pytest
from conftest import export, load, message

from telegram_search.search.lexical import (
    ContextService,
    Filters,
    SearchService,
    date_bound,
    fts_query,
)
from telegram_search.shared.errors import UserError


def test_russian_normalization_literal_query_and_phrase(importer, db, tmp_path):
    load(
        importer,
        export(
            tmp_path / "a",
            [
                message(text="Ёжик купил красный велосипед"),
                message(2, "красный очень быстрый велосипед"),
            ],
        ),
    )
    search = SearchService(db)
    assert search.search("ежик")["results"]
    assert search.search("КРАСНЫЙ велосипед", exact=True)["results"]
    assert not search.search("быстрый красный", exact=True)["results"]
    assert not search.search('" OR * NOT красный')["results"]
    assert not search.search("**")["results"]


def test_combined_filters_apply_to_one_anchor(importer, db, tmp_path):
    first = load(
        importer,
        export(
            tmp_path / "a",
            [
                message(1, "общий термин", date=100, author="alice"),
                message(2, "общий термин", date=200, author="bob"),
                message(3, "контекст", date=201, author="alice"),
            ],
        ),
    )
    load(
        importer,
        export(tmp_path / "b", [message(1, "общий термин", date=200, author="alice")], 200),
    )
    search = SearchService(db)
    assert not search.search("термин", Filters([first["chat_id"]], ["alice"], 150))["results"]
    hits = search.search("термин", Filters([first["chat_id"]], ["bob"], 150))["results"]
    assert len(hits) == 1 and hits[0]["message_id"] == 2
    assert hits[0]["messages"][0]["matches_filters"] is False
    assert hits[0]["messages"][1]["matches_filters"] is True


@pytest.mark.parametrize(
    ("text", "query"),
    [
        ("жёлтый велосипед", "ЭТО всё про ЖЁЛТЫЙ велосипед"),
        ("red bicycle", "where is the red bicycle"),
        ("red bicycle", "it's a red bicycle"),
        ("red bicycle", "it’s a red bicycle"),
        ("C R S T D 42", "the C and R S T D 42"),
    ],
)
def test_keyword_search_skips_function_words_but_requires_all_content_terms(
    importer, db, tmp_path, text, query
):
    load(
        importer,
        export(tmp_path / "source", [message(1, text), message(2, "red scooter жёлтый самокат")]),
    )
    result = SearchService(db).search(query, chunk_size=1)
    assert [hit["message_id"] for hit in result["results"]] == [1]


@pytest.mark.parametrize("query", ["и в это", "the and is", "it's", "it’s"])
def test_all_stop_word_queries_are_empty_but_can_be_searched_as_exact_phrases(
    importer, db, tmp_path, query
):
    load(importer, export(tmp_path / "source", [message(1, query)]))
    search = SearchService(db)
    assert not search.search(query)["results"]
    assert not search.search(query)["has_more"]
    assert search.search(query, exact=True)["results"][0]["message_id"] == 1


@pytest.mark.parametrize(
    ("text", "other", "query"),
    [
        ("не велосипед", "велосипед", "это не велосипед"),
        ("без сахара", "сахара", "это без сахара"),
        ("not bicycle", "bicycle", "this is not a bicycle"),
        ("never swim", "swim", "i never swim"),
        ("can't swim", "can swim", "i can't swim"),
        ("O'Reilly books", "Reilly books", "the O'Reilly books"),
    ],
)
def test_keyword_search_preserves_negations_and_apostrophized_names(
    importer, db, tmp_path, text, other, query
):
    load(importer, export(tmp_path / "source", [message(1, text), message(2, other)]))
    result = SearchService(db).search(query, chunk_size=1)
    assert [hit["message_id"] for hit in result["results"]] == [1]


def test_exact_phrase_keeps_stop_words_between_content_words(importer, db, tmp_path):
    load(
        importer,
        export(tmp_path / "source", [message(1, "cat in the box"), message(2, "cat box")]),
    )
    search = SearchService(db)
    assert len(search.search("cat in the box", chunk_size=1)["results"]) == 2
    assert [
        hit["message_id"]
        for hit in search.search("cat in the box", exact=True, chunk_size=1)["results"]
    ] == [1]


@pytest.mark.parametrize("exact", [True, False])
def test_query_word_limit_applies_before_removing_stop_words(exact):
    with pytest.raises(UserError, match="Слишком много слов"):
        fts_query("the " * 101, exact)


def test_context_order_and_overlapping_window_dedup(importer, db, tmp_path):
    first = load(
        importer, export(tmp_path / "a", [message(i, "термин", date=i) for i in range(1, 15)])
    )
    context = ContextService(db).get_context(first["chat_id"], 7, 2, 2)
    assert [m["message_id"] for m in context] == [5, 6, 7, 8, 9]
    hits = SearchService(db).search("термин", limit=2, chunk_size=6)
    assert len(hits["results"]) == 2 and hits["has_more"]
    assert [h["message_id"] for h in hits["results"]] == [1, 7]


def test_display_window_fills_boundaries_preserves_order_and_anchor(importer, db, tmp_path):
    first = load(
        importer, export(tmp_path / "a", [message(i, "термин", date=1) for i in range(1, 21)])
    )
    load(importer, export(tmp_path / "b", [message(1, "чужой контекст", date=1)], 200))
    context = ContextService(db)
    with db.connect() as conn:
        for anchor_id, size, expected in (
            (1, 1, [1]),
            (1, 4, [1, 2, 3, 4]),
            (10, 4, [9, 10, 11, 12]),
            (20, 4, [17, 18, 19, 20]),
            (10, 100, list(range(1, 21))),
        ):
            anchor = conn.execute(
                "SELECT * FROM messages WHERE chat_id=? AND message_id=?",
                (first["chat_id"], anchor_id),
            ).fetchone()
            rows = context.get_result_context(conn, anchor, size, Filters())
            assert [row["message_id"] for row in rows] == expected
            assert all(row["chat_id"] == first["chat_id"] for row in rows)


def test_date_bound_utc_and_empty_import(importer, db, tmp_path):
    assert date_bound("2026-10-05", end=True) - date_bound("2026-10-05") == 86400
    first = load(importer, export(tmp_path / "a", []))
    assert first["processed"] == 0
    assert SearchService(db).chats()[0]["messages"] == 0
