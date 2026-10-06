from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from telegram_search.config.settings import Settings
from telegram_search.search.lexical import Filters
from telegram_search.search.media import UnifiedSearch
from telegram_search.shared.errors import UserError


@pytest.fixture
def branches():
    def hit(reason, mid=1, **extra):
        return {
            "chat_id": "synthetic",
            "message_id": mid,
            "messages": [{"message_id": mid}],
            "matched_by": [reason],
            **extra,
        }

    text = SimpleNamespace(
        search=Mock(
            return_value={
                "results": [hit("words")],
                "effective_mode": "words",
                "has_more": False,
                "warnings": [],
            }
        )
    )
    media = SimpleNamespace(
        db=SimpleNamespace(settings=Settings()),
        media=SimpleNamespace(status=lambda: {"device": "cpu"}),
        search=Mock(
            side_effect=lambda *args, kind, **kwargs: (
                [
                    hit("image", media_id=7)
                    if kind == "images"
                    else hit(
                        "ocr_words", media_id=7, ocr_text="Синтетическое OCR", ocr_confidence=91
                    )
                ],
                [],
            )
        ),
    )
    return UnifiedSearch(text, media), text, media


@pytest.mark.parametrize(
    "selected",
    [
        ["text"],
        ["images"],
        ["ocr"],
        ["text", "images"],
        ["text", "ocr"],
        ["images", "ocr"],
        ["text", "images", "ocr"],
    ],
)
def test_only_selected_branches_run_and_same_message_is_merged(branches, selected):
    search, text, media = branches
    filters = Filters(["synthetic"], ["author"], 100, 200, "photo")
    result = search.search(
        "проверка", filters, True, 1, "hybrid", chunk_size=3, modalities=selected
    )
    assert result["modalities"] == selected and len(result["results"]) == 1
    expected = {"text": "words", "images": "image", "ocr": "ocr_words"}
    assert set(result["results"][0]["matched_by"]) == {expected[kind] for kind in selected}
    assert text.search.call_count == int("text" in selected)
    if "text" in selected:
        args = text.search.call_args.args
        assert args == ("проверка", filters, True, 1 if selected == ["text"] else 100, "hybrid", 3)
    assert [call.kwargs["kind"] for call in media.search.call_args_list] == [
        kind for kind in ("images", "ocr") if kind in selected
    ]
    for call in media.search.call_args_list:
        assert call.args == ("проверка", filters)
        assert call.kwargs["exact"] is True
        assert call.kwargs["mode"] == "hybrid" and call.kwargs["chunk_size"] == 3
    if "ocr" in selected:
        assert result["results"][0]["ocr_text"] == "Синтетическое OCR"
    if len(selected) > 1:
        assert result["effective_mode"] == "mixed"


def test_unavailable_text_branch_does_not_hide_available_ocr(branches):
    search, text, _ = branches
    text.search.side_effect = UserError("Синтетическая модель текста недоступна")
    result = search.search("проверка", Filters(), False, 20, "meaning", modalities=["ocr", "text"])
    assert result["modalities"] == ["text", "ocr"]
    assert result["results"][0]["matched_by"] == ["ocr_words"]
    assert result["warnings"] == ["Синтетическая модель текста недоступна"]


def test_unavailable_media_branch_does_not_hide_available_text(branches):
    search, _, media = branches
    media.search.side_effect = UserError("Синтетическая модель OCR недоступна")
    result = search.search("проверка", Filters(), False, 20, "words", modalities=["text", "ocr"])
    assert result["results"][0]["matched_by"] == ["words"]
    assert result["warnings"] == ["Синтетическая модель OCR недоступна"]


def test_selection_order_and_duplicates_do_not_change_ranking(branches):
    search, _, _ = branches
    first = search.search("проверка", Filters(), False, 20, "words", modalities=["images", "ocr"])
    second = search.search(
        "проверка", Filters(), False, 20, "words", modalities=["ocr", "images", "ocr"]
    )
    assert first == second


def test_combined_limit_keeps_more_flag_and_evidence(branches):
    search, text, _ = branches
    text.search.return_value["results"].append(
        {"chat_id": "synthetic", "message_id": 2, "messages": [], "matched_by": ["words"]}
    )
    result = search.search("проверка", Filters(), False, 1, "words", modalities=["text", "ocr"])
    assert len(result["results"]) == 1 and result["has_more"]
    assert result["results"][0]["message_id"] == 1 and result["results"][0]["media_id"] == 7


@pytest.mark.parametrize("selected", [[], ["all"], ["cuda"], ["text"] * 4, "text"])
def test_invalid_selection_fails_before_running_any_branch(branches, selected):
    search, text, media = branches
    with pytest.raises(UserError):
        search.search("проверка", Filters(), False, 20, "words", modalities=selected)
    assert not text.search.called and not media.search.called
