"""Reproducible offline benchmark; never opens a real archive or Telegram session."""

import argparse
import json
import random
import tempfile
import threading
import time
import uuid
from pathlib import Path

import psutil

from telegram_search.search.lexical import Filters
from telegram_search.search.ocr_words import ocr_word_search
from telegram_search.storage.database import Database
from telegram_search.telegram_sync.models import NormalizedMessage, Peer
from telegram_search.telegram_sync.store import SyncStore


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--messages", type=int, default=5000)
    parser.add_argument("--images", type=int, default=50000)
    args = parser.parse_args()
    if args.messages < 1 or args.images < 1:
        parser.error("Synthetic counts must be positive")
    with tempfile.TemporaryDirectory(prefix="bts-sync-ocr-bench-") as directory:
        db = Database(Path(directory))
        db.initialize()
        chat, binding, account = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO chats(id,scope,external_id,name,kind,created_at) "
                "VALUES(?,'synthetic','100','Synthetic','personal_chat',1)",
                (chat,),
            )
            conn.execute(
                "INSERT INTO source_roots(chat_id,relative_path,managed) VALUES(?,'synthetic',1)",
                (chat,),
            )
            root = conn.execute("SELECT id FROM source_roots").fetchone()[0]
            conn.execute(
                "INSERT INTO telegram_connections(id,account_user_id,state,reconnect,"
                "created_at,updated_at) "
                "VALUES(?,999,'live',1,1,1)",
                (account,),
            )
            conn.execute(
                "INSERT INTO dialog_sync_bindings(id,chat_id,connection_id,account_user_id,"
                "peer_type,peer_id,source_root_id) "
                "VALUES(?,?,?,999,'user',100,?)",
                (binding, chat, account, root),
            )
            conn.execute(
                "INSERT INTO dialog_sync_cursors(binding_id,baseline_id,scanned_through_id) "
                "VALUES(?,0,0)",
                (binding,),
            )
        store = SyncStore(db, threading.RLock())
        snapshot = store.binding(chat)
        peer = Peer("user", 100)
        start = time.perf_counter()
        for first in range(1, args.messages + 1, 100):
            records = [
                NormalizedMessage(
                    peer,
                    {
                        "id": mid,
                        "type": "message",
                        "date_unixtime": str(1750000000 + mid),
                        "from_id": "user1",
                        "text": f"Synthetic message {mid}",
                    },
                )
                for mid in range(first, min(first + 100, args.messages + 1))
            ]
            store.merge(snapshot, records, cursor=records[-1].data["id"])
        message_seconds = time.perf_counter() - start
        words = (
            "заказ документ доставка ремонт рублей магазин договор номер "
            "клиент адрес работа архив тест"
        ).split()
        rng = random.Random(42)
        start = time.perf_counter()
        with db.connect() as conn:
            for index in range(args.images):
                sha = f"{index:064x}"
                text = " ".join(rng.choices(words, k=14))
                if index % 101 == 0:
                    text += " электровелосипеда стоимость 12345"
                conn.execute("INSERT INTO media_blobs VALUES(?,1)", (sha,))
                conn.execute(
                    "INSERT INTO media_refs(chat_id,message_id,source_root_id,relative_path,"
                    "kind,sha256,status) "
                    "VALUES(?,?,?,?,'photo',?,'ready')",
                    (chat, index % args.messages + 1, root, sha + ".png", sha),
                )
                conn.execute(
                    "INSERT INTO ocr_cache(sha256,version,state,text,text_normalized,confidence) "
                    "VALUES(?,'synthetic','ready',?,?,90)",
                    (sha, text, text),
                )
        build_seconds = time.perf_counter() - start
        queries = {}
        with db.connect() as conn:
            for query in ("стоимость", "велосипед", "веласипед", "стоимсоть", "ремонт отсутствует"):
                samples = []
                for _ in range(3):
                    start = time.perf_counter()
                    keys, _, warnings = ocr_word_search(conn, query, "synthetic", Filters(), 300)
                    samples.append(round(time.perf_counter() - start, 4))
                queries[query] = {
                    "seconds": samples,
                    "candidates": len(keys),
                    "partial_fuzzy": bool(warnings),
                }
            work = conn.execute("SELECT COUNT(*) FROM index_work WHERE state='pending'").fetchone()[
                0
            ]
        print(
            json.dumps(
                {
                    "synthetic_messages": args.messages,
                    "synthetic_images": args.images,
                    "message_merge_seconds": round(message_seconds, 3),
                    "pending_segment_generations": work,
                    "ocr_postings_build_seconds": round(build_seconds, 3),
                    "ocr_queries": queries,
                    "rss_mib": round(psutil.Process().memory_info().rss / 1024**2, 1),
                    "sqlite_mib": round(db.path.stat().st_size / 1024**2, 1),
                },
                ensure_ascii=False,
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
