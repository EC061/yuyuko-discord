"""Simple JSON-backed i18n helper (Chinese default, English supported)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from wows_bot.config import Language


class I18n:
    def __init__(self, locales_dir: Path, default_lang: Language = "zh") -> None:
        self._default_lang: Language = default_lang
        self._catalogs: dict[str, dict[str, Any]] = {}
        for lang in ("zh", "en"):
            path = locales_dir / f"{lang}.json"
            if path.exists():
                self._catalogs[lang] = json.loads(path.read_text(encoding="utf-8"))

    @property
    def default_lang(self) -> Language:
        return self._default_lang

    def catalog(self, lang: str | None = None) -> dict[str, Any]:
        key = lang if lang in self._catalogs else self._default_lang
        return self._catalogs.get(key, self._catalogs.get("zh", {}))

    def t(self, dotted_key: str, lang: str | None = None, **kwargs: Any) -> str:
        val: Any = self.catalog(lang)
        for part in dotted_key.split("."):
            if isinstance(val, dict) and part in val:
                val = val[part]
            else:
                val = self.catalog(self._default_lang)
                for p in dotted_key.split("."):
                    if isinstance(val, dict) and p in val:
                        val = val[p]
                    else:
                        return dotted_key
                break
        text = str(val)
        return text.format(**kwargs) if kwargs else text
