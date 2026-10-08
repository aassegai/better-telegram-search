# Telegram merge and OCR keyword search

Measured on 2026-10-08, Linux x86_64 / Python 3.12.3, CPU only. Reproduce with:

```sh
uv run --offline --no-sync python scripts/benchmark_sync_ocr.py
```

The script creates an isolated temporary workspace, 5,000 synthetic messages and
50,000 synthetic OCR records with repeated Russian vocabulary and selected target
terms. It never connects to Telegram, opens a real archive or loads ML models.
These measurements cover SQLite ingestion/retrieval, not OCR inference or network latency.

| Operation | Measured time |
| --- | --- |
| Merge 5,000 messages in pages of 100, with provenance/cursor/outbox | 1.97 s |
| Build OCR token and bi/trigram postings for 50,000 records | 27.54 s |
| Exact words: `стоимость` | 0.010–0.011 s |
| Substring: `велосипед` | 0.178–0.236 s |
| Typo: `веласипед` | 0.053–0.056 s |
| Adjacent transposition: `стоимсоть` | 0.216–0.337 s |
| No match, AND terms: `ремонт отсутствует` | 0.308–0.357 s |

Each query ran three times and requested up to 300 candidates. Pending text index
generations coalesced to one for the affected day. Final process RSS was 29.7 MiB;
SQLite was 81.4 MiB. RSS is an end-of-run sample, not a peak measurement. Repetitive
synthetic text is not representative of every archive; very long OCR or broad queries
can take longer and candidate limits can reduce recall.
Reindexing separates archived/deleted messages from live neighbors at chunk boundaries,
including overlap. The deleted-message filter then excludes only archived chunks.

Keyword OCR ranks exact tokens with BM25 first, then verified literal substrings,
then approximate matches sorted by edit count and gram-index BM25. All meaningful
terms must match. Exact phrase mode retains literal behavior. Typo verification is
limited to compact queries and a shared budget of one million text characters;
exhaustion produces a visible partial-result warning. A regression test checks
adversarial long texts with matching grams and no matching substring.

Fault tests cover history/event interleaving, atomic cursor rollback, resume,
typed peers, stale edits and exports, tombstones, attempt fencing, pause/resume,
shutdown, rate limits, auth cancellation, media corruption, shared-file deduplication,
quota and symlink/reparse boundaries. Browser tests use synthetic API responses.
Real-account delivery, native Windows/macOS permissions and full native packages
require platform verification; this report does not claim those checks were run.
