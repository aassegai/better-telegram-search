"""Bounded image downloads, atomic publication, shared-file-safe garbage collection."""

import asyncio
import errno
import hashlib
import os
import re
import shutil
import time
import uuid
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from telegram_search.storage.generations import bump_revision, invalidate_segments, utc_day
from telegram_search.telegram_sync.models import Peer, SourceFailure
from telegram_search.telegram_sync.paths import owned_path

UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
MANAGED_FILE = re.compile(rf"^{UUID}\.(?:jpg|png|webp|gif|[0-9a-f]{{32}}\.part)$")


def validate_image(path):
    try:
        with Image.open(path) as image:
            if image.width * image.height > 40_000_000:
                raise SourceFailure("media_limit")
            extension = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp", "GIF": "gif"}.get(
                image.format
            )
            if not extension:
                raise SourceFailure("media_invalid")
            image.verify()
        with Image.open(path) as image:
            image.load()
        return extension
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise SourceFailure("media_invalid") from exc


class MediaDownloadWorker:
    def __init__(self, store, settings):
        self.store, self.db, self.settings = store, store.db, settings

    def _snapshot(self, job, binding):
        with self.store.lock, self.db.connect() as conn:
            if not self.store.current(conn, binding, media=True):
                return None
            row = conn.execute(
                "SELECT a.*,s.relative_path AS root FROM telegram_assets a "
                "JOIN source_roots s ON s.id=? JOIN telegram_message_provenance p "
                "ON p.chat_id=a.chat_id AND p.message_id=a.message_id "
                "JOIN telegram_jobs j ON j.asset_id=a.id "
                "WHERE a.id=? AND p.remote_media_id=a.remote_identity AND j.id=? "
                "AND j.state='running' AND j.attempt=? "
                "AND NOT EXISTS(SELECT 1 FROM telegram_tombstones t "
                "WHERE t.chat_id=a.chat_id AND t.message_id=a.message_id)",
                (binding["source_root_id"], job["asset_id"], job["id"], job["attempt"]),
            ).fetchone()
            return dict(row) if row else None

    def _reserve(self, job, binding, free_bytes):
        limit = self.settings.max_image_mib * 1024 * 1024
        with self.store.lock, self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not self.store.current(conn, binding, media=True) or not self.store.job_current(
                conn, job
            ):
                return False
            size = conn.execute(
                "SELECT size FROM telegram_assets WHERE id=?", (job["asset_id"],)
            ).fetchone()
            reserve = size[0] if size and size[0] else limit
            if reserve > limit:
                raise SourceFailure("media_limit")
            ready = conn.execute(
                "SELECT COALESCE(SUM(size),0) FROM (SELECT DISTINCT b.sha256,b.size FROM "
                "media_blobs b "
                "JOIN media_refs r ON r.sha256=b.sha256 JOIN source_roots s ON "
                "s.id=r.source_root_id "
                "WHERE s.managed=1 AND r.status='ready')"
            ).fetchone()[0]
            pending = conn.execute(
                "SELECT COALESCE(SUM(reserved_bytes),0) FROM telegram_assets WHERE id<>?",
                (job["asset_id"],),
            ).fetchone()[0]
            if ready + pending + reserve > self.settings.media_quota_mib * 1024 * 1024:
                raise SourceFailure("disk_full")
            if free_bytes - pending - reserve < self.settings.min_free_mib * 1024 * 1024:
                raise SourceFailure("disk_full")
            conn.execute(
                "UPDATE telegram_assets SET reserved_bytes=? WHERE id=?", (reserve, job["asset_id"])
            )
        return True

    def _cancel(self, job):
        with self.store.lock, self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not self.store.job_current(conn, job):
                return
            self._return_job(conn, job)

    @staticmethod
    def _return_job(conn, job):
        # A settings change is a temporary fence, not permanent cancellation.
        valid = conn.execute(
            "SELECT 1 FROM telegram_assets a JOIN telegram_message_provenance p "
            "ON p.chat_id=a.chat_id AND p.message_id=a.message_id WHERE a.id=? "
            "AND a.remote_identity=p.remote_media_id AND NOT EXISTS "
            "(SELECT 1 FROM telegram_tombstones t "
            "WHERE t.chat_id=a.chat_id AND t.message_id=a.message_id)",
            (job["asset_id"],),
        ).fetchone()
        conn.execute(
            "UPDATE telegram_jobs SET state=?,available_at=?,lease_until=0 "
            "WHERE id=? AND attempt=?",
            ("pending" if valid else "cancelled", int(time.time()) + 5, job["id"], job["attempt"]),
        )
        conn.execute("UPDATE telegram_assets SET reserved_bytes=0 WHERE id=?", (job["asset_id"],))

    def _release(self, job):
        with self.store.lock, self.db.connect() as conn:
            conn.execute(
                "UPDATE telegram_assets SET reserved_bytes=0 WHERE id=? AND EXISTS "
                "(SELECT 1 FROM telegram_jobs WHERE id=? AND attempt=?)",
                (job["asset_id"], job["id"], job["attempt"]),
            )

    async def run(self, job, binding, client):
        asset = await asyncio.to_thread(self._snapshot, job, binding)
        if not asset:
            return await asyncio.to_thread(self._cancel, job)
        root = owned_path(self.db.workspace, asset["root"])
        expected_root = self.db.workspace / "data/media/telegram"
        if not root.is_relative_to(expected_root):
            raise SourceFailure("storage")
        root.mkdir(parents=True, exist_ok=True)
        folder = owned_path(self.db.workspace, root / binding["chat_id"])
        folder.mkdir(mode=0o700, exist_ok=True)
        if os.name != "nt":
            folder.chmod(0o700)
        if not await asyncio.to_thread(self._reserve, job, binding, shutil.disk_usage(root).free):
            return await asyncio.to_thread(self._cancel, job)
        part = folder / f"{asset['id']}.{uuid.uuid4().hex}.part"
        stream = None
        try:
            digest, size = hashlib.sha256(), 0
            stream = part.open("xb")
            if os.name != "nt":
                part.chmod(0o600)
            blocks = client.download(
                Peer.from_binding(binding), asset["message_id"], asset["remote_identity"]
            )
            try:
                while True:
                    try:
                        block = await asyncio.wait_for(anext(blocks), timeout=45)
                    except StopAsyncIteration:
                        break
                    size += len(block)
                    if size > self.settings.max_image_mib * 1024 * 1024:
                        raise SourceFailure("media_limit")
                    if (
                        shutil.disk_usage(root).free - len(block)
                        < self.settings.min_free_mib * 1024 * 1024
                    ):
                        raise SourceFailure("disk_full")
                    digest.update(block)
                    stream.write(block)
                    await asyncio.to_thread(self.store.renew, job)
            finally:
                await blocks.aclose()
            stream.flush()
            os.fsync(stream.fileno())
            stream.close()
            stream = None
            extension = await asyncio.to_thread(validate_image, part)
            sha256 = digest.hexdigest()
            relative = f"{binding['chat_id']}/{asset['id']}.{extension}"
            await asyncio.to_thread(
                self._publish, job, binding, asset, relative, sha256, size, part
            )
        except OSError as exc:
            if exc.errno == errno.ENOSPC:
                raise SourceFailure("disk_full") from None
            raise SourceFailure("storage") from None
        finally:
            if stream:
                stream.close()
            part.unlink(missing_ok=True)
            await asyncio.to_thread(self._release, job)

    def _publish(self, job, binding, asset, relative, sha256, size, part):
        with self.store.lock, self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not self.store.job_current(conn, job):
                return False
            if not self.store.current(conn, binding, media=True):
                self._return_job(conn, job)
                return False
            current = conn.execute(
                "SELECT m.timestamp FROM messages m JOIN telegram_message_provenance p "
                "ON p.chat_id=m.chat_id AND p.message_id=m.message_id JOIN telegram_jobs j ON "
                "j.id=? "
                "JOIN telegram_assets a ON a.id=j.asset_id "
                "WHERE m.chat_id=? AND m.message_id=? AND p.remote_media_id=? "
                "AND a.remote_identity=p.remote_media_id AND j.state='running' "
                "AND NOT EXISTS(SELECT 1 FROM telegram_tombstones t WHERE t.chat_id=m.chat_id AND "
                "t.message_id=m.message_id)",
                (job["id"], binding["chat_id"], asset["message_id"], asset["remote_identity"]),
            ).fetchone()
            if not current:
                conn.execute(
                    "UPDATE telegram_jobs SET state='cancelled',lease_until=0 WHERE id=?",
                    (job["id"],),
                )
                return False
            # Dedup decision, final rename and reference publication share the
            # lifecycle lock with other publishers and GC. A deleted candidate
            # is replaced from our still-validated .part, never marked ready.
            root = owned_path(self.db.workspace, asset["root"])
            duplicate = None
            for row in conn.execute(
                "SELECT DISTINCT r.relative_path,s.relative_path root FROM media_refs r "
                "JOIN source_roots s ON s.id=r.source_root_id WHERE r.sha256=? "
                "AND r.status='ready' AND s.managed=1",
                (sha256,),
            ):
                if row["root"] != asset["root"]:
                    continue
                path = owned_path(self.db.workspace, root / row["relative_path"])
                if path.is_file():
                    with path.open("rb") as stream:
                        valid = (
                            path.stat().st_size == size
                            and hashlib.file_digest(stream, "sha256").hexdigest() == sha256
                        )
                    if not valid:
                        # Repair a corrupted shared copy from this validated
                        # download before publishing another ready reference.
                        os.replace(part, path)
                    duplicate = row["relative_path"]
                    break
            if duplicate:
                relative = duplicate
            else:
                target = owned_path(self.db.workspace, root / relative)
                os.replace(part, target)
            conn.execute("INSERT OR IGNORE INTO media_blobs VALUES(?,?)", (sha256, size))
            conn.execute(
                "DELETE FROM media_refs WHERE chat_id=? AND message_id=? AND source_root_id=?",
                (binding["chat_id"], asset["message_id"], binding["source_root_id"]),
            )
            conn.execute(
                "INSERT INTO "
                "media_refs(chat_id,message_id,source_root_id,relative_path,kind,sha256,status) "
                "VALUES(?,?,?,?,'photo',?,'ready')",
                (
                    binding["chat_id"],
                    asset["message_id"],
                    binding["source_root_id"],
                    relative,
                    sha256,
                ),
            )
            conn.execute(
                "UPDATE telegram_assets SET "
                "state='ready',relative_path=?,sha256=?,size=?,reserved_bytes=0,error_code=NULL "
                "WHERE id=?",
                (relative, sha256, size, asset["id"]),
            )
            conn.execute(
                "UPDATE telegram_jobs SET state='done',lease_until=0,error_code=NULL WHERE id=?",
                (job["id"],),
            )
            invalidate_segments(conn, binding["chat_id"], {utc_day(current[0])}, "telegram")
            bump_revision(conn, binding["chat_id"])
            return True

    def collect_orphans(self, grace_seconds=86400):
        try:
            root = owned_path(self.db.workspace, "data/media/telegram")
        except SourceFailure:
            return 0
        if not root.is_dir():
            return 0
        removed, cutoff = 0, time.time() - grace_seconds
        for directory, children, files in os.walk(root, followlinks=False):
            parent = Path(directory)
            try:
                owned_path(self.db.workspace, parent)
            except SourceFailure:
                children[:] = []
                continue
            safe_children = []
            for name in children:
                if re.fullmatch(UUID, name):
                    try:
                        owned_path(self.db.workspace, parent / name)
                        safe_children.append(name)
                    except SourceFailure:
                        pass
            children[:] = safe_children
            parts = parent.relative_to(root).parts
            if len(parts) != 2:
                continue
            source_root = (root / parts[0]).relative_to(self.db.workspace).as_posix()
            for name in files:
                if not MANAGED_FILE.fullmatch(name):
                    continue
                try:
                    path = owned_path(self.db.workspace, parent / name)
                    if path.stat().st_mtime > cutoff:
                        continue
                    relative = f"{parts[1]}/{name}"
                    with self.store.lock, self.db.connect() as conn:
                        # Recheck the file boundary after taking the publisher lock.
                        owned_path(self.db.workspace, path)
                        if not conn.execute(
                            "SELECT 1 FROM media_refs r JOIN source_roots s "
                            "ON s.id=r.source_root_id WHERE s.managed=1 "
                            "AND s.relative_path=? AND r.relative_path=? LIMIT 1",
                            (source_root, relative),
                        ).fetchone():
                            path.unlink(missing_ok=True)
                            removed += 1
                except (FileNotFoundError, SourceFailure):
                    continue
        return removed
