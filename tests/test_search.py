from conftest import export, load, message

from telegram_search.search.lexical import ContextService, Filters, SearchService, date_bound


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


def test_context_order_and_overlapping_window_dedup(importer, db, tmp_path):
    first = load(
        importer, export(tmp_path / "a", [message(i, "термин", date=i) for i in range(1, 15)])
    )
    context = ContextService(db).get_context(first["chat_id"], 7, 2, 2)
    assert [m["message_id"] for m in context] == [5, 6, 7, 8, 9]
    hits = SearchService(db).search("термин", limit=2)
    assert len(hits["results"]) == 2 and hits["has_more"]
    assert [h["message_id"] for h in hits["results"]] == [1, 5]


def test_date_bound_utc_and_empty_import(importer, db, tmp_path):
    assert date_bound("2026-10-05", end=True) - date_bound("2026-10-05") == 86400
    first = load(importer, export(tmp_path / "a", []))
    assert first["processed"] == 0
    assert SearchService(db).chats()[0]["messages"] == 0
