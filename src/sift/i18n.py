"""Translations. The catalog for a language is a flat JSON file in `sift/i18n/`, with English as the fallback for any missing key.
The language follows the system (LC_ALL, LC_MESSAGES, LANGUAGE, LANG) unless the user picked one with `sift config set language CODE`."""
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Mapping

SUPPORTED = ("en", "nl", "fr", "de", "es", "zh", "ar")
NAMES = {"en": "English", "nl": "Nederlands", "fr": "Français", "de": "Deutsch", "es": "Español", "zh": "中文", "ar": "العربية"}
RTL = frozenset({"ar"})
_DIR = Path(__file__).parent / "i18n"
_current: str | None = None


def from_locale(value: str) -> str | None:
    """'nl_NL.UTF-8' -> 'nl'; 'zh_TW' -> 'zh'; unsupported -> None."""
    code = value.split(".")[0].split("@")[0].split("_")[0].split("-")[0].lower()
    return code if code in SUPPORTED else None


def detect(env: Mapping[str, str] | None = None) -> str:
    """The system language: the first supported entry of LC_ALL, LC_MESSAGES, LANGUAGE (a colon list) or LANG."""
    env = os.environ if env is None else env
    for var in ("LC_ALL", "LC_MESSAGES", "LANGUAGE", "LANG"):
        for part in (env.get(var) or "").split(":"):
            if part and part not in ("C", "POSIX"):
                code = from_locale(part)
                if code:
                    return code
    return "en"


def resolve(cfg: Mapping | None = None, env: Mapping[str, str] | None = None) -> tuple[str, str]:
    """(setting, language): setting is what the user chose ('auto' or a code), language what is actually used."""
    env = os.environ if env is None else env
    setting = str((cfg or {}).get("language") or env.get("SIFT_LANG") or "auto").lower()
    if setting in SUPPORTED:
        return setting, setting
    return "auto", detect(env)


@lru_cache(maxsize=None)
def _load(lang: str) -> dict[str, str]:
    try:
        return json.loads((_DIR / f"{lang}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def catalog(lang: str) -> dict[str, str]:
    return {**_load("en"), **_load(lang)} if lang != "en" else dict(_load("en"))


def direction(lang: str) -> str:
    return "rtl" if lang in RTL else "ltr"


def init(cfg: Mapping | None = None, env: Mapping[str, str] | None = None) -> str:
    global _current
    _current = resolve(cfg, env)[1]
    return _current


def t(key: str, **kw: object) -> str:
    """Translate a message for the terminal and the panel. Unknown keys come back unchanged, so a missing entry is visible, not fatal."""
    text = catalog(_current or detect()).get(key, key)
    for k, v in kw.items():
        text = text.replace("{" + k + "}", str(v))
    return text
