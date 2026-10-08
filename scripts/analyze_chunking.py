"""Explicit, read-only export statistics. Reports stay in an ignored local directory."""

import argparse
import csv
import json
import re
import sqlite3
import tempfile
from collections import Counter
from pathlib import Path

import ijson

from telegram_search.shared.text import flatten_text, normalize_text

WORD = re.compile(r"[^\W_]+(?:[-'’][^\W_]+)*", re.UNICODE)


def single_word(text):
    normalized = normalize_text(text.strip())
    words = list(WORD.finditer(normalized))
    if len(words) != 1:
        return None
    word = words[0]
    # URL, code and technical identifiers are kept separate, never proposed as noise.
    outer = normalized[: word.start()] + normalized[word.end() :]
    if any(character.isalnum() or character in "_/@#:=\\" for character in outer):
        return None
    return word.group()


def analyze(source, output):
    output.mkdir(parents=True, exist_ok=True)
    summary = Counter()
    # One export is processed once; duplicates are keyed by chat AND message ID.
    with tempfile.TemporaryDirectory(prefix="stats-", dir=output) as temporary:
        conn = sqlite3.connect(Path(temporary) / "counts.sqlite")
        conn.executescript(
            "CREATE TABLE seen(chat TEXT, id INTEGER, PRIMARY KEY(chat,id));"
            "CREATE TABLE words(form TEXT PRIMARY KEY,n INTEGER,reply INTEGER,media INTEGER);"
        )
        # Find the structure without materializing the list of chats or messages.
        with source.open("rb") as handle:
            multi = any(prefix == "chats.list" for prefix, _, _ in _header(handle))
        with source.open("rb") as handle:
            prefix = "chats.list.item.messages.item" if multi else "messages.item"
            # Track chat boundaries in the parser, not by potentially reused message IDs.
            chat = "single"
            objects = ijson.parse(handle)
            for field, event, value in objects:
                if multi and field == "chats.list.item.id" and event in {"number", "string"}:
                    chat = str(value)
                if field != prefix or event != "start_map":
                    continue
                message = _object(objects)
                mid = message.get("id")
                if not isinstance(mid, int):
                    summary["invalid_identity"] += 1
                    continue
                if not conn.execute("INSERT OR IGNORE INTO seen VALUES(?,?)", (chat, mid)).rowcount:
                    summary["duplicates"] += 1
                    continue
                summary["messages"] += 1
                if message.get("type") != "message":
                    summary["service"] += 1
                    continue
                text = flatten_text(message.get("text"))
                media = bool(
                    message.get("photo") or message.get("file") or message.get("media_type")
                )
                summary["with_attachment"] += int(media)
                if not text.strip():
                    summary["without_text"] += 1
                    continue
                summary["text_messages"] += 1
                summary["one_field"] += int(len(text.split()) == 1)
                form = single_word(text)
                if form is None:
                    continue
                summary["single_word"] += 1
                conn.execute(
                    "INSERT INTO words VALUES(?,1,?,?) ON CONFLICT(form) DO UPDATE SET "
                    "n=n+1,reply=reply+excluded.reply,media=media+excluded.media",
                    (form, int(bool(message.get("reply_to_message_id"))), int(media)),
                )
        summary["unique_forms"] = conn.execute("SELECT count(*) FROM words").fetchone()[0]
        with (output / "single_word_candidates.csv").open(
            "w", newline="", encoding="utf-8"
        ) as handle:
            writer = csv.writer(handle)
            writer.writerow(
                ["form", "message_count", "reply_count", "attachment_count", "decision"]
            )
            writer.writerows(
                (*row, "candidate")
                for row in conn.execute("SELECT * FROM words ORDER BY n DESC,form")
            )
        conn.close()
    (output / "summary.json").write_text(json.dumps(dict(summary), indent=2), encoding="utf-8")
    return dict(summary)


def _header(handle):
    for prefix, event, value in ijson.parse(handle):
        yield prefix, event, value
        if prefix == "messages" or prefix == "chats.list":
            return


def _object(events):
    """Consume one JSON object using ijson's bounded per-message builder."""
    builder = ijson.ObjectBuilder()
    builder.event("start_map", None)
    depth = 1
    for _, event, value in events:
        builder.event(event, value)
        depth += (event in {"start_map", "start_array"}) - (event in {"end_map", "end_array"})
        if depth == 0:
            return builder.value
    raise ValueError("Incomplete message object")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path, default=Path("workspace/analysis/chunking"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    destination = args.output.resolve()
    if not any(destination.is_relative_to(root / folder) for folder in ("workspace", "plans")):
        parser.error("Private reports must stay inside ignored workspace/ or plans/.")
    print(json.dumps(analyze(args.source, destination), ensure_ascii=False))
