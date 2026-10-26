"""Explicit scoped draft/override/apply operations over immutable local reports."""

import json
import re
import sqlite3
import time

from telegram_search.search.chunking.noise.compile import NoisePolicy, compile_policy, exact_forms
from telegram_search.search.chunking.policy import (
    APPROVED_FILLER,
    DEFAULT_POLICY,
    LEGACY_POLICY_ID,
    ChunkPolicy,
)
from telegram_search.shared.errors import UserError
from telegram_search.shared.text import serialize
from telegram_search.storage.chunking import chat_policy
from telegram_search.storage.generations import bump_revision
from telegram_search.storage.noise import NoiseReport, source_fence


class NoiseDraftConflict(UserError):
    pass


def validate_overrides(values):
    if not isinstance(values, dict) or len(values) > 1000:
        raise UserError("Допускается не более 1000 переопределений в одном отчёте.")
    try:
        exact_forms(tuple(values))
    except ValueError:
        raise UserError("Переопределение должно содержать точную нормализованную форму.") from None
    if any(type(value) is not str or value not in {"keep", "omit"} for value in values.values()):
        raise UserError("Неизвестное переопределение фильтра.")
    return dict(sorted(values.items()))


class NoisePolicyService:
    def __init__(self, db, analyses, lifecycle_lock, semantic=None):
        self.db, self.analyses = db, analyses
        self.lock, self.semantic = lifecycle_lock, semantic

    @staticmethod
    def _revision(value):
        if type(value) is not int or not 0 <= value <= 2**31 - 1:
            raise UserError("Недопустимая ревизия проекта.")

    @staticmethod
    def _overrides(row):
        encoded = row["overrides_json"]
        if not isinstance(encoded, str) or len(encoded.encode()) > 512 * 1024:
            raise UserError("Проект фильтра повреждён.")
        try:
            return validate_overrides(json.loads(encoded))
        except (ValueError, TypeError, RecursionError):
            raise UserError("Проект фильтра повреждён.") from None

    def metadata(self, chat_id, job_id):
        row = self.analyses.store.get(chat_id, job_id)
        return {
            "id": job_id,
            "revision": row["draft_revision"],
            "overrides": {} if row["stale"] else self._overrides(row),
            "overrides_available": not row["stale"],
            "stale": row["stale"],
            "state": row["state"],
            "current_policy_id": row["current_policy_id"],
            "applied_policy_id": row["applied_policy_id"],
            "applied_draft_revision": row["applied_draft_revision"],
            "new_auto_omit_enabled": False,
        }

    def _prepare(self, chat_id, job_id, revision, overrides=None):
        self._revision(revision)
        row = self.analyses.store.get(chat_id, job_id)
        if row["stale"]:
            raise NoiseDraftConflict("Диалог или исключения изменились. Запустите анализ заново.")
        if row["draft_revision"] != revision:
            raise NoiseDraftConflict("Проект изменился. Обновите его перед сохранением.")
        summary = json.loads(row["summary_json"] or "null")
        if (
            row["state"] != "ready"
            or not isinstance(summary, dict)
            or summary.get("complete") is not True
            or summary.get("new_auto_omit_enabled") is not False
            or not isinstance(summary.get("report_sha256"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", summary["report_sha256"])
        ):
            raise UserError("Нужен полный актуальный анализ. Запустите анализ заново.")
        fence = json.loads(row["fence_json"])
        with self.db.connection_timeout(0.2), self.db.connect() as conn:
            stored = conn.execute(
                "SELECT policy_json FROM chunking_policies WHERE id=?", (fence["policy_id"],)
            ).fetchone()
        if fence["policy_id"] == LEGACY_POLICY_ID:
            base = DEFAULT_POLICY
        else:
            if stored is None:
                raise UserError("Политика чанкинга не найдена.")
            base = ChunkPolicy.from_json(stored[0])
            if base.identity != fence["policy_id"]:
                raise UserError("Политика чанкинга повреждена.")
        selected = self._overrides(row) if overrides is None else validate_overrides(overrides)
        report = NoiseReport(self.db, job_id, fence)
        try:
            if report.digest() != summary["report_sha256"]:
                raise UserError("Отчёт анализа изменён. Запустите анализ заново.")
            with report.connect() as conn:
                report.verify(conn)
                accepted = [
                    r[0]
                    for r in conn.execute(
                        "SELECT form FROM forms WHERE decision='omit' "
                        "AND eligible_count>0 ORDER BY form"
                    )
                ]
                # No uncalibrated novel form can enter the automatic draft.
                if any(form not in APPROVED_FILLER for form in accepted):
                    raise UserError("Версия решений анализа не поддерживается.")
                for form in selected:
                    if not conn.execute("SELECT 1 FROM forms WHERE form=?", (form,)).fetchone():
                        raise UserError("Форма не найдена в выбранном отчёте.")
            if report.digest() != summary["report_sha256"]:
                raise UserError("Отчёт анализа изменён. Запустите анализ заново.")
        except (OSError, ValueError, TypeError, RecursionError, sqlite3.Error) as exc:
            if isinstance(exc, UserError):
                raise
            raise UserError("Отчёт анализа несовместим или повреждён.") from None
        inherited_omit = set(base.omit) if isinstance(base, NoisePolicy) else set()
        inherited_keep = set(base.keep) if isinstance(base, NoisePolicy) else set()
        chosen_omit = {f for f, decision in selected.items() if decision == "omit"}
        chosen_keep = {f for f, decision in selected.items() if decision == "keep"}
        try:
            policy = compile_policy(
                base,
                omit=tuple(sorted((set(accepted) | inherited_omit | chosen_omit) - chosen_keep)),
                keep=tuple(sorted((inherited_keep | chosen_keep) - chosen_omit)),
            )
        except (ValueError, TypeError):
            raise UserError(
                "Не удалось скомпилировать правила. Проверьте лимиты проекта."
            ) from None
        return row, policy, selected

    def _guard(self, conn, chat_id, job_id, row):
        current = conn.execute(
            "SELECT * FROM noise_analyses WHERE chat_id=? AND id=?", (chat_id, job_id)
        ).fetchone()
        if current is None:
            raise KeyError(job_id)
        if current["draft_revision"] != row["draft_revision"] or current["state"] != "ready":
            raise NoiseDraftConflict("Проект изменился. Обновите его перед сохранением.")
        if source_fence(conn, chat_id) != json.loads(row["fence_json"]):
            raise NoiseDraftConflict("Диалог или исключения изменились. Запустите анализ заново.")

    def preview(self, chat_id, job_id, revision):
        row, policy, overrides = self._prepare(chat_id, job_id, revision)
        with self.db.connection_timeout(0.2), self.db.connect() as conn:
            conn.execute("BEGIN")
            self._guard(conn, chat_id, job_id, row)
        return {
            "policy_id": policy.identity,
            "version": policy.version,
            "revision": revision,
            "explicit_omit_forms": len(policy.omit),
            "keep_forms": len(policy.keep),
            "overrides": overrides,
            "new_auto_omit_enabled": False,
        }

    def save_overrides(self, chat_id, job_id, revision, overrides):
        row, _, selected = self._prepare(chat_id, job_id, revision, overrides)
        with self.db.connection_timeout(0.2), self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._guard(conn, chat_id, job_id, row)
            conn.execute(
                "UPDATE noise_analyses SET overrides_json=?,draft_revision=draft_revision+1 "
                "WHERE id=?",
                (serialize(selected), job_id),
            )
        return self.metadata(chat_id, job_id)

    def apply(self, chat_id, job_id, revision):
        self._revision(revision)
        row = self.analyses.store.get(chat_id, job_id)
        # The same applied draft is a no-op even after later imports. No stale
        # report is reinterpreted and no new generation is scheduled here.
        if (
            row["applied_policy_id"]
            and row["applied_draft_revision"] == revision == row["draft_revision"]
            and row["current_policy_id"] == row["applied_policy_id"]
        ):
            return {
                "policy_id": row["applied_policy_id"],
                "already_applied": True,
                "scheduled_segments": 0,
            }
        row, policy, _ = self._prepare(chat_id, job_id, revision)
        now = int(time.time())
        with self.lock, self.db.connection_timeout(0.2), self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            fresh = conn.execute(
                "SELECT * FROM noise_analyses WHERE chat_id=? AND id=?", (chat_id, job_id)
            ).fetchone()
            if fresh is None:
                raise KeyError(job_id)
            if (
                fresh["applied_policy_id"] == policy.identity
                and fresh["applied_draft_revision"] == revision == fresh["draft_revision"]
                and chat_policy(conn, chat_id) == policy.identity
            ):
                return {
                    "policy_id": policy.identity,
                    "already_applied": True,
                    "scheduled_segments": 0,
                }
            self._guard(conn, chat_id, job_id, row)
            if chat_policy(conn, chat_id) == policy.identity:
                # New statistics with unchanged exact semantics never rebuild
                # chunks or invalidate a query snapshot.
                conn.execute(
                    "UPDATE noise_analyses SET applied_policy_id=?,applied_draft_revision=? "
                    "WHERE id=?",
                    (policy.identity, revision, job_id),
                )
                return {
                    "policy_id": policy.identity,
                    "already_applied": True,
                    "scheduled_segments": 0,
                }
            conn.execute(
                "INSERT OR IGNORE INTO chunking_policies VALUES(?,?)",
                (policy.identity, policy.json),
            )
            if (
                conn.execute(
                    "SELECT policy_json FROM chunking_policies WHERE id=?", (policy.identity,)
                ).fetchone()[0]
                != policy.json
            ):
                raise UserError("Политика чанкинга повреждена.")
            conn.execute(
                "INSERT INTO chat_chunking VALUES(?,?) ON CONFLICT(chat_id) "
                "DO UPDATE SET policy_id=excluded.policy_id",
                (chat_id, policy.identity),
            )
            # Set-based full text rebuild: no source-message scan or per-day
            # Python/SQL loop while the main database writer is held.
            conn.execute(
                "UPDATE index_work SET state='superseded',finished_at=? WHERE chat_id=? "
                "AND state IN ('pending','running','failed')",
                (now, chat_id),
            )
            for table in ("segment_context_dependencies", "segment_context_ranges"):
                conn.execute(
                    f"DELETE FROM {table} WHERE work_id IN "
                    "(SELECT id FROM index_work WHERE chat_id=? AND state='superseded')",
                    (chat_id,),
                )
            conn.execute("DELETE FROM chunk_context_dirty WHERE chat_id=?", (chat_id,))
            scheduled = conn.execute(
                "UPDATE index_segments SET target_generation=target_generation+1,"
                "lexical_generation=target_generation+1,chunking_policy_id=? WHERE chat_id=?",
                (policy.identity, chat_id),
            ).rowcount
            conn.execute(
                "INSERT INTO index_work(id,chat_id,utc_day,generation,reason,created_at,"
                "chunking_policy_id) SELECT chat_id||':'||utc_day||':'||target_generation,"
                "chat_id,utc_day,target_generation,'noise_policy',?,chunking_policy_id "
                "FROM index_segments WHERE chat_id=?",
                (now, chat_id),
            )
            bump_revision(conn, chat_id)
            conn.execute(
                "UPDATE noise_analyses SET applied_policy_id=?,applied_draft_revision=? WHERE id=?",
                (policy.identity, revision, job_id),
            )
        if self.semantic is not None:
            self.semantic.estimates.cache.clear()
            self.semantic.wake.set()
        return {
            "policy_id": policy.identity,
            "already_applied": False,
            "scheduled_segments": scheduled,
        }
