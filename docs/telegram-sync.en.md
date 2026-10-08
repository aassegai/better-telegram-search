# Telegram connection

English · [Русский](telegram-sync.md)

This optional source receives new messages, edits, deletion events and images from
selected cloud chats belonging to one account. It never sends, edits, deletes or
marks Telegram messages as read. Secret Chats and automatic group migration/merging
are not supported. Search and Desktop JSON import remain available offline.

## Developer setup

Install `uv sync --locked --extra semantic --extra ocr --extra telegram` for source runs.
Register your own developer application with
[Telegram](https://core.telegram.org/api/obtaining_api_id). Sample application IDs are
not suitable for distribution. Users of a configured build do not register developer apps.

Set `BTS_TELEGRAM_API_ID` and `BTS_TELEGRAM_API_HASH` in the environment, or create
`workspace/private/telegram.toml` with your own values:

```toml
[telegram_sync]
api_id = 123456
api_hash = "replace_with_your_32_character_hex_hash"
```

These are placeholders. Never commit the private configuration. Resource defaults
are in [configs/telegram.example.toml](../configs/telegram.example.toml).
Restart after changing the configuration.

For native builds, set GitHub Actions secrets `TELEGRAM_API_ID` and `TELEGRAM_API_HASH`.
The builder embeds only the developer application identifiers in the backend package,
not the frontend. They can be extracted from the distribution and do not authorize
access to a user's account. Builds without them support search/import but cannot
connect to Telegram. Telethon is included on all build platforms; Torch is unnecessary.

## Connect and sync

1. Open **Sources · Telegram** and enter your international phone number, login code
   and two-step verification password if Telegram requests it. Telegram chooses the
   delivery method; SMS is not guaranteed. Login expires after five minutes.
2. Load the Telegram dialog list. The filter applies to the current page; use
   **Next page** for other dialogs.
3. Choose an existing archive or create a new chat with a UTC start date. Up to
   20 overlapping messages are checked. Type/ID mismatches and unverified content
   differences block binding. Check the account and explicitly confirm the binding.
4. Open the chat's **⋯** menu for history coverage, run status, photo failures and
   indexing progress. Pause, update now, retry errors or remove the source binding.
   Existing messages survive pause, disconnect and source removal.

History up to the export's highest ID remains the seed archive. Its completeness is
not proven, and old export gaps are not automatically filled. New chats download
available history after the chosen date. Pages contain up to 100 messages, with
20 pages per bounded run and durable resume cursors. Recent edits are reconciled
every 15 minutes across the last seven days by default. Missing items in a history
page are not deletion evidence. Older edits outside the window may remain unknown.
Sync runs only while the application is open.

New images go to `workspace/data/media/telegram/`. Defaults: 40 MiB per image,
two concurrent downloads, 20 GiB quota and 512 MiB minimum free space. Only validated
images are published. Identical files are shared; unreferenced managed files are
collected after 24 hours. Desktop source files are preserved. Old missing export
photos are not automatically backfilled.

**Keep an archive copy** marks remotely deleted messages; a search checkbox hides
them when needed. **Remove from local search** deletes the copy and search fragments
on subsequent Telegram deletion events. Changing policy does not retroactively purge
earlier archive copies. Old JSON cannot resurrect tombstoned messages or overwrite
newer API revisions. Uncertain edits preserve the current copy and report a conflict count.
Semantic indexing separates archived records from live messages so hiding deleted
records preserves their live neighbors in search results.

## Session and diagnostics

The session grants account access and is stored **unencrypted** at
`workspace/private/telegram/<connection-id>.session`. POSIX directories use mode 700
and session files mode 600. Windows uses the user's private profile directory.
Do not share this directory. The app does not import `tdata` or existing sessions.
OTP/passwords are never stored in the database, logs or browser storage. API status
never returns auth keys, session strings, `api_hash`, phone numbers or private messages.

**Disconnect Telegram** preserves the session and archive. **Log out of Telegram**
revokes the current session and removes its local file. If server logout cannot be
confirmed, terminate it in Telegram → Settings → Devices. The archive remains.
Existing bindings require the same account when logging in again.

Errors and retry times appear in Sources and the chat card. FloodWait delays are
respected; retry does not bypass them. `/api/telegram/connection`,
`/api/chats/{chat_id}/telegram` and `/api/sync/runs/{run_id}` provide safe status.
Offline tests use a synthetic adapter. Real authorization and Windows/macOS filesystem
permissions still need verification on those platforms.
