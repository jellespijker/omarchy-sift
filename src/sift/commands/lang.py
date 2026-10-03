"""`sift i18n`: the strings for the panel, in the language in use."""
from __future__ import annotations

import json

from .. import i18n


def run(a, cfg) -> int:
    setting, lang = i18n.resolve(cfg)
    if a.lang:
        setting, lang = a.lang, a.lang if a.lang in i18n.SUPPORTED else lang
    print(json.dumps({"setting": setting, "lang": lang, "dir": i18n.direction(lang), "strings": i18n.catalog(lang)}, ensure_ascii=False))
    return 0


def register(sub) -> None:
    p = sub.add_parser("i18n", help="the interface strings in the language in use (for the panel)")
    p.add_argument("--json", action="store_true"); p.add_argument("--lang")
    p.set_defaults(run=run)
