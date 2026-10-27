"""External model/OCR workers share the application RAM admission budget."""

from types import SimpleNamespace

import psutil
import pytest

from telegram_search.config.settings import Settings
from telegram_search.indexing.rerank import RerankService
from telegram_search.inference.memory import process_tree_rss
from telegram_search.inference.providers import CPU
from telegram_search.shared.errors import UserError


def process_tree(monkeypatch, child_memory):
    calls = []

    def memory():
        if isinstance(child_memory, Exception):
            raise child_memory
        return SimpleNamespace(rss=child_memory * 1024**2)

    def children(*, recursive):
        calls.append(recursive)
        return [SimpleNamespace(memory_info=memory)]

    parent = SimpleNamespace(
        children=children, memory_info=lambda: SimpleNamespace(rss=100 * 1024**2)
    )
    monkeypatch.setattr(psutil, "Process", lambda: parent)
    monkeypatch.setattr(psutil, "virtual_memory", lambda: SimpleNamespace(available=1024**3))
    return calls


@pytest.mark.parametrize("child_memory,admitted", [(400, False), (100, True)])
def test_reranker_admission_includes_children(monkeypatch, child_memory, admitted):
    calls = process_tree(monkeypatch, child_memory)
    service = object.__new__(RerankService)
    service.db = SimpleNamespace(settings=Settings(memory_limit_mib=512))
    encoder = SimpleNamespace(
        session=None,
        query_session=None,
        execution=SimpleNamespace(provider=CPU),
        spec=SimpleNamespace(download_bytes=0, manifest={"runtime": "python-adapter"}),
    )
    if admitted:
        service.check_memory(encoder)
    else:
        with pytest.raises(UserError, match="Недостаточно памяти"):
            service.check_memory(encoder)
    assert calls == [True]


def test_unloaded_child_is_ignored_but_unreadable_memory_fails_closed(monkeypatch):
    process_tree(monkeypatch, psutil.NoSuchProcess(123))
    assert process_tree_rss() == 100 * 1024**2
    process_tree(monkeypatch, psutil.AccessDenied(123))
    with pytest.raises(psutil.AccessDenied):
        process_tree_rss()
