import threading

from test_semantic import setup_index

from telegram_search.indexing.prefetch import BatchPrefetch


def test_e5_next_batch_prepares_during_current_inference(db, importer, tmp_path):
    _, _, worker, work = setup_index(db, importer, tmp_path, ["поезд " * 180 for _ in range(48)])
    next_prepared = threading.Event()
    prepared = []
    calls = []
    original = worker.encoder.encode_text

    def prepare(texts, purpose):
        prepared.append(tuple(texts))
        if len(prepared) > 1:
            next_prepared.set()
        return tuple(texts)

    def encode(texts, purpose, *, _prepared):
        assert _prepared == tuple(texts)
        if not calls:
            assert next_prepared.wait(timeout=5)
        calls.append(tuple(texts))
        return original(texts, purpose)

    worker.encoder.prepare_text = prepare
    worker.encoder.encode_text = encode
    result = worker.run(work)
    assert result["state"] == "done" and len(calls) >= 2
    assert worker.vectors.table("synthetic", 4).count_rows() == result["chunks_total"]


def test_prefetch_discards_old_batch_and_releases_references():
    prefetch = BatchPrefetch()
    try:
        prefetch.submit("old", lambda: "old data")
        assert prefetch.take("new", lambda: "new data") == "new data"
        prefetch.submit("next", lambda: "prepared")
        assert prefetch.take("next", lambda: "must not run") == "prepared"
        assert prefetch.pending is None
    finally:
        prefetch.close()
