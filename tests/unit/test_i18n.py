from __future__ import annotations

import pytest

from server import i18n
from server.channels.renderer import EN_DEFAULTS, render_transition


def test_bundles_cover_every_renderer_key_in_both_languages() -> None:
    for lang in i18n.SUPPORTED:
        missing = [k for k in EN_DEFAULTS if k not in i18n.bundle(lang)]
        assert missing == [], (lang, missing)
    assert i18n.missing_keys("ko") == []


def test_language_resolution_and_fallbacks() -> None:
    assert i18n.resolve_language("ko", None) == "ko"
    assert i18n.resolve_language("ko", "en") == "en"  # channel override wins
    assert i18n.resolve_language(None, None) == "en"
    assert i18n.resolve_language("xx", "yy") == "en"
    with pytest.raises(i18n.UnsupportedLanguageError):
        i18n.bundle("fr")


def test_ids_and_codes_are_never_translated() -> None:
    text_ko = i18n.translate("renderer.transition.verifying", "ko", verification_id="vr-123")
    text_en = i18n.translate("renderer.transition.verifying", "en", verification_id="vr-123")
    assert "vr-123" in text_ko and "vr-123" in text_en and text_ko != text_en
    assert i18n.translate("unknown.key", "ko") == "unknown.key"


def test_renderer_uses_the_selected_bundle() -> None:
    ko = render_transition("TASK_STARTED", {}, i18n.bundle("ko"))
    en = render_transition("TASK_STARTED", {}, i18n.bundle("en"))
    assert ko == "작업 시작" and en == "Work started"


def test_document_headings_localized_but_keys_and_order_stable() -> None:
    from server.documents.templates import SECTIONS, headings_of, render

    en = render("T", {}, "en")
    ko = render("T", {}, "ko")
    assert headings_of(en) == [h for _, h in SECTIONS]
    assert headings_of(ko)[0] == "목적과 범위" and len(headings_of(ko)) == len(SECTIONS)
    assert render("T", {}) == en  # default language is English (canonical spec text)
