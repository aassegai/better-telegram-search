"""Offline CLI adapter for the same analysis and exact-policy services as the API."""

import sqlite3
import threading
import time

from filelock import FileLock

from telegram_search.indexing.noise_analysis import NoiseAnalysisService
from telegram_search.indexing.noise_policy import NoisePolicyService
from telegram_search.search.chunking.noise.contracts import AnalysisOptions
from telegram_search.shared.errors import UserError


def add_noise_parser(commands):
    noise = commands.add_parser("noise", help="Локальный анализ шума и точные правила диалога")
    actions = noise.add_subparsers(dest="noise_action", required=True)
    for action in ("analyze", "list", "status", "report", "override", "preview", "apply", "delete"):
        parser = actions.add_parser(action)
        parser.add_argument("--chat-id", required=True)
        if action not in {"analyze", "list"}:
            parser.add_argument("--job-id", required=True)
        if action in {"override", "preview", "apply"}:
            parser.add_argument("--revision", type=int, required=True)
        if action == "analyze":
            parser.add_argument("--max-messages", type=int, default=100000)
            parser.add_argument("--max-forms", type=int, default=50000)
            parser.add_argument("--time-limit-seconds", type=int, default=300)
        if action == "report":
            parser.add_argument("--kind", choices=("forms", "families"), default="forms")
            parser.add_argument("--decision", choices=("keep", "review", "omit"))
            parser.add_argument("--offset", type=int, default=0)
            parser.add_argument("--limit", type=int, default=50)
            parser.add_argument(
                "--show-private",
                action="store_true",
                help="Явно разрешить вывод приватных форм в stdout",
            )
        if action == "override":
            for decision in ("keep", "omit", "clear"):
                parser.add_argument("--" + decision, action="append", default=[])


def noise_command(db, args):
    # The application and all standalone writers share this lease. In particular
    # a status command must never run recovery over a live app's analysis jobs.
    with FileLock(db.workspace / ".writer.lock", timeout=0):
        analyses = None
        try:
            with db.connection_timeout(0.2):
                analyses = NoiseAnalysisService(db)
            policies = NoisePolicyService(db, analyses, threading.RLock())
            return _command(analyses, policies, args)
        except KeyError:
            raise UserError("Диалог или анализ не найден.") from None
        except sqlite3.Error:
            raise UserError("Не удалось выполнить операцию с базой анализа.") from None
        except KeyboardInterrupt:
            raise UserError("Операция анализа отменена.") from None
        finally:
            if analyses is not None:
                analyses.close()


def _command(analyses, policies, args):
    action, chat = args.noise_action, args.chat_id
    if action == "analyze":
        try:
            options = AnalysisOptions(args.max_messages, args.max_forms, args.time_limit_seconds)
        except ValueError:
            raise UserError("Недопустимые лимиты анализа.") from None
        job = analyses.create(chat, options)
        while True:
            with analyses.lock:
                active = job["id"] in analyses.jobs and not analyses.persistence_error
            if not active:
                return analyses.status(chat, job["id"])
            if analyses.thread is not None and not analyses.thread.is_alive():
                raise UserError("Анализатор остановился. Запустите анализ заново.")
            time.sleep(0.05)
    if action == "list":
        return analyses.list(chat)
    if action == "status":
        return analyses.status(chat, args.job_id)
    if action == "report":
        if not args.show_private:
            raise UserError("Для вывода приватных форм укажите --show-private.")
        page = {"offset": args.offset, "limit": args.limit}
        if args.kind == "forms":
            page["decision"] = args.decision
        return analyses.report(chat, args.job_id, kind=args.kind, **page)
    if action == "delete":
        analyses.delete(chat, args.job_id)
        return {"deleted": True}
    if action == "override":
        current = policies.metadata(chat, args.job_id)
        selected = current["overrides"]
        for form in args.clear:
            selected.pop(form, None)
        for form in args.omit:
            selected[form] = "omit"
        for form in args.keep:
            selected[form] = "keep"
        saved = policies.save_overrides(chat, args.job_id, args.revision, selected)
        # Private forms are only emitted by the explicitly opted-in report action.
        return {
            "id": saved["id"],
            "revision": saved["revision"],
            "overrides_count": len(saved["overrides"]),
        }
    if action == "preview":
        result = policies.preview(chat, args.job_id, args.revision)
        result.pop("overrides")
        return result
    if action == "apply":
        return policies.apply(chat, args.job_id, args.revision)
    raise UserError("Неизвестная операция анализа.")
