"""Two bounded streaming passes through the actual frozen chunk builders."""

import hashlib
import importlib.metadata
import json
import sqlite3
import time
from dataclasses import asdict, dataclass

import psutil

from telegram_search.config.model_registry import model_spec
from telegram_search.indexing.noise_policy import NoiseDraftConflict
from telegram_search.indexing.noise_source import DryRunLimited, NoiseSource
from telegram_search.search.chunking.builder import EpisodeBuilder
from telegram_search.search.chunking.classification import classify, whole_word
from telegram_search.search.chunking.legacy import ChunkBuilder
from telegram_search.search.chunking.noise.contracts import DryRunOptions
from telegram_search.search.chunking.noise.dry_run import (
    DRY_RUN_VERSION,
    ChunkMetrics,
    OwnedMessages,
)
from telegram_search.search.chunking.policy import APPROVED_FILLER, LEGACY_POLICY_ID
from telegram_search.security.paths import is_link_or_reparse
from telegram_search.shared.errors import UserError
from telegram_search.shared.text import serialize
from telegram_search.storage.noise import NoiseReport


class TokenizerUnavailable(UserError):
    pass


@dataclass(frozen=True)
class TokenContract:
    tokenizer: object
    space_id: str
    model_id: str
    prefix: str
    max_tokens: int
    batch_size: int
    tokenizer_version: str

    def __post_init__(self):
        if (
            type(self.batch_size) is not int
            or not 1 <= self.batch_size <= 128
            or type(self.max_tokens) is not int
            or not 32 <= self.max_tokens <= 512
            or not isinstance(self.prefix, str)
            or len(self.prefix) > 256
        ):
            raise UserError("Контракт токенизации не поддерживается.")


def local_token_contract(db, chat_id, check=lambda: None):
    """Load only the active model's pinned local tokenizer, never ONNX weights.

    Verify model identity and both tokenizer files, rather than constructing a
    SemanticService (which recovers indexing jobs and imports inference runtime).
    Offline planned batches use the configured size; future OOM backoff is unknown.
    """
    from telegram_search.inference.bundles import BundleStore
    from telegram_search.inference.tokenization import ModelTokenizer

    with db.connection_timeout(0.2), db.connect() as conn:
        row = conn.execute(
            "SELECT e.id,e.profile,e.manifest_json,c.text_batch FROM chats c "
            "JOIN semantic_state s ON s.id=1 JOIN embedding_spaces e ON e.id=s.active_space_id "
            "WHERE c.id=?",
            (chat_id,),
        ).fetchone()
    if row is None:
        raise TokenizerUnavailable(
            "Для dry-run нужен локальный токенизатор активной текстовой модели."
        )
    check()
    try:
        spec = model_spec(row["profile"])
        manifest = json.loads(row["manifest_json"])
        tokenizer_version = importlib.metadata.version("tokenizers")
        if (
            manifest["model"] != spec.manifest
            or hashlib.sha256(serialize(manifest).encode()).hexdigest() != row["id"]
            or manifest["runtime"]["tokenizers_version"] != tokenizer_version
        ):
            raise ValueError()
        bundle = BundleStore(db.workspace).path(spec)
        current = db.workspace
        for part in bundle.relative_to(db.workspace).parts:
            current /= part
            if is_link_or_reparse(current):
                raise ValueError()
        files = {item["name"]: item for item in spec.manifest["files"]}
        for name in ("tokenizer.json", "tokenizer_config.json"):
            check()
            path, item = bundle / name, files[name]
            if (
                is_link_or_reparse(path)
                or not 0 < item["bytes"] <= 64 * 1024**2
                or path.stat().st_size != item["bytes"]
            ):
                raise ValueError()
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                while block := stream.read(1024**2):
                    check()
                    digest.update(block)
            if digest.hexdigest() != item["sha256"]:
                raise ValueError()
        tokenizer = ModelTokenizer(bundle)
        check()
        return TokenContract(
            tokenizer,
            row["id"],
            spec.model_id,
            spec.manifest["passage_prefix"],
            spec.manifest["chunk_max_tokens"],
            row["text_batch"] or db.settings.embedding_batch,
            tokenizer_version,
        )
    except (ImportError, OSError, ValueError, TypeError, KeyError, RecursionError):
        raise TokenizerUnavailable(
            "Локальный токенизатор отсутствует или повреждён. Подготовьте текстовую модель."
        ) from None


class NoiseDryRun:
    def __init__(self, policies):
        self.policies, self.db = policies, policies.db

    def compare(
        self,
        chat_id,
        job_id,
        revision,
        *,
        options=None,
        contract=None,
        cancelled=lambda: False,
        progress=lambda value: None,
        admission_check=lambda: None,
    ):
        options = options or DryRunOptions()
        began, cpu_began = time.monotonic(), time.thread_time()
        deadline = began + options.time_limit_seconds
        process = psutil.Process()
        baseline = peak = process.memory_info().rss
        sampled = 0

        def interrupted():
            return cancelled() or time.monotonic() >= deadline

        def check():
            nonlocal peak, sampled
            if cancelled():
                raise UserError("Сравнение чанкинга отменено.")
            now = time.monotonic()
            if now >= deadline:
                raise DryRunLimited("time_limit")
            if now - sampled >= 0.1:
                sampled = now
                peak = max(peak, process.memory_info().rss)

        class CheckedTokenizer:
            def count(self, text):
                check()
                result = contract.tokenizer.count(text)
                check()
                return result

        results, limits, difference = [], set(), None
        row = proposed = None
        owned = source = None
        try:
            row, proposed, _ = self.policies._prepare(chat_id, job_id, revision, check=check)
            admission_check()
            fence = json.loads(row["fence_json"])
            contract = contract or local_token_contract(self.db, chat_id, check)
            admission_check()
            before = self.policies._source_policy(fence)
            report = NoiseReport(self.db, job_id, fence)
            with report.connect() as conn:
                review = frozenset(
                    r[0] for r in conn.execute("SELECT form FROM forms WHERE decision='review'")
                )
            # Classify diagnostic noise-only chunks against the same exact set
            # in both passes, independent of which policy retains each form.
            noise_forms = frozenset((*APPROVED_FILLER, *proposed.omit))

            def guard(conn):
                self.policies._guard(conn, chat_id, job_id, row)
                active = conn.execute(
                    "SELECT active_space_id FROM semantic_state WHERE id=1"
                ).fetchone()[0]
                batch = conn.execute(
                    "SELECT text_batch FROM chats WHERE id=?", (chat_id,)
                ).fetchone()[0]
                if (
                    active != contract.space_id
                    or (batch or self.db.settings.embedding_batch) != contract.batch_size
                ):
                    raise NoiseDraftConflict(
                        "Модель или размер батча изменились. Повторите dry-run."
                    )

            owned = OwnedMessages(interrupted)
            for side, policy in enumerate((before, proposed)):
                source = NoiseSource(self.db, chat_id, guard, check, interrupted)
                counts = {
                    "classified_omitted_messages": 0,
                    "protected_noise_messages": 0,
                    "review_messages": 0,
                }
                effective_omitted = frozenset(
                    (*policy.filler, *getattr(policy, "omit", ()))
                ) - frozenset(getattr(policy, "keep", ()))
                legacy = side == 0 and fence["policy_id"] == LEGACY_POLICY_ID

                def messages(
                    source=source,
                    policy=policy,
                    legacy=legacy,
                    counts=counts,
                    side=side,
                    effective_omitted=effective_omitted,
                ):
                    for message in source.messages(options.max_messages):
                        if not legacy:
                            parent = (
                                source.lookup(chat_id, message.reply_to)
                                if message.reply_to is not None
                                else None
                            )
                            classified = classify(message, policy, parent)
                            form = whole_word(message.text)
                            counts["classified_omitted_messages"] += int(
                                classified.role == "filler" and not classified.include
                            )
                            counts["protected_noise_messages"] += int(
                                # Inclusion of an effective exact omission is
                                # determined by the actual versioned classifier,
                                # including v3 structural/compound protections.
                                classified.include and form in effective_omitted
                            )
                            counts["review_messages"] += int(form in review)
                        if source.scanned % 128 == 1:
                            progress(
                                {
                                    "phase": "before" if side == 0 else "after",
                                    "scanned_messages": source.scanned,
                                    "total_messages": results[0]["source_messages"]
                                    if side
                                    else None,
                                    "elapsed_seconds": round(time.monotonic() - began, 3),
                                }
                            )
                        yield message

                builder = (
                    ChunkBuilder(CheckedTokenizer(), passage_prefix=contract.prefix)
                    if legacy
                    else EpisodeBuilder(
                        CheckedTokenizer(),
                        policy,
                        passage_prefix=contract.prefix,
                        lookup=source.lookup,
                        neighbor_lookup=lambda message, source=source, policy=policy: (
                            source.neighbor(message, policy.neighbor_seconds)
                        ),
                    )
                )
                metrics = ChunkMetrics(contract.batch_size)
                for chunk in builder.build(messages(), 1, contract.space_id):
                    check()
                    if chunk.tokens > contract.max_tokens:
                        raise UserError("Чанки превышают лимит активной модели.")
                    metrics.add(chunk, noise_forms, source.own)
                    owned.add(side, chunk.parts)
                check()
                own_count = owned.count(side)
                results.append(
                    {
                        **metrics.result(),
                        **{key: None if legacy else value for key, value in counts.items()},
                        "source_messages": source.scanned,
                        "own_messages": own_count,
                        "skipped_messages": source.scanned - own_count,
                        "classification_available": not legacy,
                    }
                )
                if source.limited:
                    limits.add("message_limit")
            check()
            measured_difference = owned.differences()
            admission_check()
            with self.db.connection_timeout(0.2), self.db.connect() as conn:
                conn.execute("BEGIN")
                guard(conn)
            check()
            difference = measured_difference
        except DryRunLimited as exc:
            limits.add(exc.reason)
        except sqlite3.Error:
            try:
                check()  # Translate a cooperative SQL interruption to its cause.
            except DryRunLimited as exc:
                limits.add(exc.reason)
            else:
                raise
        finally:
            if owned is not None:
                owned.close()
        comparison_complete = len(results) == 2 and difference is not None
        return {
            "version": DRY_RUN_VERSION,
            "state": "limited" if limits else "ready",
            "complete": comparison_complete and not limits,
            "comparison_complete": comparison_complete,
            "source_verified": comparison_complete,
            "options": asdict(options),
            "progress": {
                "phase": "complete"
                if comparison_complete
                else "before"
                if not results
                else "after",
                "completed_passes": len(results),
                "scanned_messages_in_last_pass": source.scanned if source is not None else 0,
            },
            "limits": sorted(limits),
            "draft_revision": revision,
            "source_policy_id": json.loads(row["fence_json"])["policy_id"] if row else None,
            "proposed_policy_id": proposed.identity if proposed else None,
            "space_id": contract.space_id if contract else None,
            "model_id": contract.model_id if contract else None,
            "tokenizer_version": contract.tokenizer_version if contract else None,
            "batch_size": contract.batch_size if contract else None,
            "batch_basis": "fixed_configured_size_per_utc_day_without_oom_backoff",
            "before": results[0] if results else None,
            "after": results[1] if len(results) == 2 else None,
            "delta": {
                **{
                    key: results[1][key] - results[0][key]
                    for key in (
                        "chunks",
                        "tokens",
                        "padded_token_slots",
                        "padding_tokens",
                        "batches",
                        "noise_only_chunks",
                    )
                },
                **difference,
            }
            if comparison_complete
            else None,
            "elapsed_seconds": round(time.monotonic() - began, 3),
            "cpu_seconds": round(time.thread_time() - cpu_began, 3),
            "process_peak_rss_bytes": peak,
            "process_rss_baseline_bytes": baseline,
            "new_auto_omit_enabled": False,
        }
