"""Deterministic language detection by function-word frequency. Used instead of a classifier, which is unreliable for this."""
from __future__ import annotations

import re

_NL = frozenset("de het een van en ik je niet dat is op te voor met zijn dit ook maar als bij naar wordt worden aan nog dan wel kan kunnen moet om uit door over".split())
_EN = frozenset("the of and to in is that for it with as was on are be this by or from at have not but an they which you were all can".split())
MIN_WORDS = 30


def detect_language(text: str) -> str | None:
    """'nl', 'en', 'mixed', or None when the text is too short to tell."""
    words = re.findall(r"[a-z]+", text.lower())
    if len(words) < MIN_WORDS:
        return None
    nl = sum(w in _NL for w in words) / len(words)
    en = sum(w in _EN for w in words) / len(words)
    if nl > en * 1.2 and nl > 0.06:
        return "nl"
    return "en" if en >= nl else "mixed"


def matches_detector(detector: str, text: str) -> bool:
    """`language:nl` / `language:en`."""
    kind, _, arg = detector.partition(":")
    return kind == "language" and detect_language(text) == arg
