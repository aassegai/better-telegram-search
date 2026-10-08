import pytest

from telegram_search.search.cache import SearchCache


def result(count=100, more=False):
    return {"results": [{"message_id": i} for i in range(count)], "has_more": more}


def test_paging_keeps_order_is_replayable_and_never_mutates_the_snapshot():
    cache = SearchCache()
    first = cache.remember(result(), "revision", 20, 5)
    assert first["next_offset"] == 20 and first["cached_results"] == 100
    token = first["search_id"]
    all_hits = first["results"]
    for offset in range(20, 100, 20):
        page = cache.page(token, "revision", offset, 20)
        all_hits += page["results"]
    assert [hit["message_id"] for hit in all_hits] == list(range(100))
    assert page["next_offset"] is None and not page["has_more"]
    page["results"][0]["message_id"] = -1
    assert cache.page(token, "revision", 80, 20)["results"][0]["message_id"] == 80


def test_expiry_invalidation_lru_and_byte_budget():
    now = [0]
    cache = SearchCache(ttl=10, max_entries=2, clock=lambda: now[0])
    a = cache.remember(result(), "r1", 5, 1)["search_id"]
    b = cache.remember(result(), "r1", 5, 1)["search_id"]
    cache.page(a, "r1", 5, 5)
    cache.remember(result(), "r1", 5, 1)
    with pytest.raises(KeyError):
        cache.page(b, "r1", 5, 5)
    with pytest.raises(ValueError):
        cache.page(a, "r2", 5, 5)
    assert a not in cache.entries
    now[0] = 10
    with pytest.raises(KeyError):
        cache.page(a, "r1", 5, 5)
    cache._expire()
    assert cache.bytes == 0 and not cache.entries
    small = SearchCache(max_bytes=5)
    assert small.remember(result(), "r1", 5, 1)["search_id"] is None
    assert small.bytes == 0


def test_finished_indexing_does_not_expire_snapshot_but_archive_changes_do(db):
    before = SearchCache.revision(db)
    with db.connect() as conn:
        conn.execute("UPDATE semantic_state SET paused=1,download_completed_bytes=100")
        conn.execute("UPDATE media_state SET paused=1,ocr_paused=1")
    assert SearchCache.revision(db) == before
    with db.connect() as conn:
        conn.execute("UPDATE media_state SET images_enabled=1")
    assert SearchCache.revision(db) != before
