"""Message bundles (development plan §7H): ``i18n/{ko,en}/messages.json``.

The instance default language is set in Setup step 1 and can be overridden per channel. Only
user-facing text is localized; Event types, error codes, and IDs are never translated. Missing
keys fall back to English, then to the key itself, so a bundle can never break rendering.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "i18n"
SUPPORTED = ("en", "ko")
DEFAULT_LANGUAGE = "en"


class UnsupportedLanguageError(ValueError):
    def __init__(self, language: str) -> None:
        super().__init__(f"I18N_LANGUAGE_UNSUPPORTED: {language}")
        self.code = "I18N_LANGUAGE_UNSUPPORTED"


@lru_cache(maxsize=8)
def bundle(language: str) -> dict[str, str]:
    if language not in SUPPORTED:
        raise UnsupportedLanguageError(language)
    path = ROOT / language / "messages.json"
    data: dict[str, str] = json.loads(path.read_text(encoding="utf-8"))
    return data


def resolve_language(instance_default: str | None, channel_override: str | None) -> str:
    for candidate in (channel_override, instance_default, DEFAULT_LANGUAGE):
        if candidate and candidate in SUPPORTED:
            return candidate
    return DEFAULT_LANGUAGE


def translate(key: str, language: str, **kwargs: object) -> str:
    """Localized text for ``key``; placeholders are formatted, unknown placeholders left as-is."""
    template = bundle(language).get(key) or bundle(DEFAULT_LANGUAGE).get(key) or key
    try:
        return template.format(**kwargs)
    except (KeyError, IndexError):
        return template


def missing_keys(language: str) -> list[str]:
    """Keys present in English but absent in ``language`` (lint helper)."""
    return sorted(set(bundle(DEFAULT_LANGUAGE)) - set(bundle(language)))
