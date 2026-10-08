import random
import time

import pytest
from conftest import load
from test_media import fake_ocr, photo_export, services

from telegram_search.search.lexical import Filters
from telegram_search.search.media import MediaSearch
from telegram_search.search.ocr_words import VerificationBudget, ocr_word_search, substring_distance
from telegram_search.shared.text import normalize_text


@pytest.fixture
def ocr_search(db, importer, tmp_path):
    job = load(importer, photo_export(tmp_path / "source"))
    semantic, media = services(db, importer)
    fake_ocr(media, text="Стоимость ремонта электровелосипеда 12345 рублей")
    assert media._ocr_one()
    return MediaSearch(db, media, semantic, importer.lifecycle_lock), job, media


@pytest.mark.parametrize(
    "query,kind",
    [
        ("стоимость ремонта", "exact"),
        ("велосипед", "substring"),
        ("стоимост ремнта", "fuzzy"),
        ("стоимсоть", "fuzzy"),
        ("электровеласипеда", "fuzzy"),
    ],
)
def test_bm25_ocr_accepts_substrings_and_typos(ocr_search, query, kind):
    search, _, _ = ocr_search
    hits, warnings = search.search(query, Filters(), kind="ocr", chunk_size=1)
    assert not warnings
    assert len(hits) == 1 and hits[0]["ocr_match"]["kind"] == kind
    assert hits[0]["matched_by"] == ["ocr_words"]


def test_ocr_all_meaningful_terms_and_exact_phrase(ocr_search):
    search, _, _ = ocr_search
    assert not search.search("велосипед отсутствует", Filters(), kind="ocr")[0]
    assert not search.search("велосипед", Filters(), kind="ocr", exact=True)[0]
    assert not search.search("ремнта", Filters(), kind="ocr", exact=True)[0]
    assert search.search("стоимость ремонта", Filters(), kind="ocr", exact=True)[0]


def test_substring_filters_versions_and_fts_lifecycle(ocr_search, db):
    search, job, media = ocr_search
    for filters in (
        Filters(chat_ids=["absent"]),
        Filters(author_ids=["absent"]),
        Filters(date_from=2_000_000_000),
        Filters(date_to=1),
    ):
        assert not search.search("велосипед", filters, kind="ocr")[0]
    with db.connect() as conn:
        conn.execute("UPDATE messages SET remote_deleted=1")
    assert not search.search("велосипед", Filters(exclude_deleted=True), kind="ocr")[0]
    assert search.search("велосипед", Filters(chat_ids=[job["chat_id"]]), kind="ocr")[0]
    media.ocr.version = "other"
    assert not search.search("велосипед", Filters(), kind="ocr")[0]
    media.ocr.version = "synthetic-ocr-v1"
    with db.connect() as conn:
        conn.execute("UPDATE ocr_cache SET text_normalized=?,text=?", ("другое", "другое"))
    assert not search.search("велосипед", Filters(), kind="ocr")[0]
    assert search.search("ругое", Filters(), kind="ocr")[0]
    db.rebuild()
    assert search.search("ругое", Filters(), kind="ocr")[0]
    with db.connect() as conn:
        conn.execute("DELETE FROM ocr_cache")
    assert not search.search("ругое", Filters(), kind="ocr")[0]


def test_exact_tokens_rank_before_substring_and_fuzzy(ocr_search, db):
    _, _, media = ocr_search
    with db.connect() as conn:
        for sha, text in [("a" * 64, "велосипед"), ("b" * 64, "веласипед")]:
            conn.execute("INSERT INTO media_blobs VALUES(?,1)", (sha,))
            source = conn.execute("SELECT * FROM media_refs LIMIT 1").fetchone()
            conn.execute(
                "INSERT INTO media_refs(chat_id,message_id,source_root_id,relative_path,kind,"
                "sha256,status) VALUES(?,?,?,?,'photo',?,'ready')",
                (
                    source["chat_id"],
                    source["message_id"],
                    source["source_root_id"],
                    sha + ".png",
                    sha,
                ),
            )
            conn.execute(
                "INSERT INTO ocr_cache(sha256,version,state,text,text_normalized,confidence) "
                "VALUES(?,?,'ready',?,?,90)",
                (sha, media.ocr.version, text, text),
            )
        keys, evidence, warnings = ocr_word_search(
            conn, "велосипед", media.ocr.version, Filters(), 10
        )
    assert not warnings
    assert [evidence[key]["kind"] for key in keys] == ["exact", "substring", "fuzzy"]


def test_approximate_substring_matches_reference_dp():
    rng = random.Random(42)

    def reference(pattern, text):
        row = list(range(len(pattern) + 1))
        best = len(pattern)
        for char in text:
            new = [0]
            for i, expected in enumerate(pattern, 1):
                new.append(min(row[i] + 1, new[-1] + 1, row[i - 1] + (char != expected)))
            row = new
            best = min(best, row[-1])
        return best

    for _ in range(1000):
        pattern = "".join(rng.choices("abcdef", k=rng.randint(4, 20)))
        text = "".join(rng.choices("abcdef", k=100))
        expected = reference(pattern, text)
        actual = substring_distance(pattern, text, 2)
        # Adjacent transposition is intentionally also accepted at distance one.
        swaps = [
            pattern[:i] + pattern[i + 1] + pattern[i] + pattern[i + 2 :]
            for i in range(len(pattern) - 1)
        ]
        if pattern in text:
            assert actual == 0
        elif any(value in text for value in swaps):
            assert actual == 1
        else:
            assert actual == (expected if expected <= 2 else None)


def test_adversarial_fuzzy_verification_has_shared_work_budget():
    budget = VerificationBudget(100_000)
    # All query bigrams occur, yet no approximate substring. Repeated candidates
    # cannot force unlimited Python verification while holding the writer lock.
    pattern = "abcdefghijklm"
    text = normalize_text(" ab bc cd de ef fg gh hi ij jk kl lm " * 2000)
    started = time.monotonic()
    for _ in range(100):
        assert substring_distance(pattern, text, 2, budget) is None
    assert budget.exhausted and budget.remaining >= 0
    assert time.monotonic() - started < 2
