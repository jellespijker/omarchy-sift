"""Validation and normalisation of everything a user can type: tag names, descriptions, patterns, thresholds.
Pure functions (no I/O, no catalogue loading). Raise `ValidationError` with a message that is fit to show in the panel."""
from __future__ import annotations

import re
import unicodedata

from typing import Callable

# English text for every message this module can raise, so the core needs no catalogue and no file access. The application replaces
# `translate` (see sift/validate.py) with the user's language; a test keeps these equal to i18n/en.json.
MESSAGES = {
    "cli.val.descEmpty": "the description cannot be empty",
    "cli.val.descTooLong": "the description is too long ({n} characters; the limit is {max})",
    "cli.val.empty": "{what} cannot be empty",
    "cli.val.glob": "an ignore pattern must be 1 to {max} characters",
    "cli.val.mustBeText": "{what} must be text",
    "cli.val.patternEmpty": "the pattern cannot be empty",
    "cli.val.patternInvalid": "not a valid pattern: {err}",
    "cli.val.patternLong": "the pattern is too long (limit {max})",
    "cli.val.patternNested": "this pattern has a repeated group inside a repeated group, which can take forever on some text; simplify it",
    "cli.val.range": "{what} must be between {lo} and {hi}",
    "cli.val.tagNeedsLetter": "a tag name needs at least one letter or digit",
    "cli.val.tagTooLong": "a tag name can be at most {max} characters",
    "cli.val.tooLong": "{what} is too long ({n} characters; the limit is {max})",
    "cli.val.unit": "{what} must be a number between 0 and 1",
    "cli.val.whole": "{what} must be a whole number",
    "what.text": "text",
    "what.threshold": "threshold",
    "what.value": "value"
}

def _english(key: str, **kw) -> str:
    return MESSAGES[key].format(**kw)


translate: Callable[..., str] = _english


def set_translator(fn: Callable[..., str]) -> None:
    global translate
    translate = fn


def t(key: str, **kw) -> str:
    return translate(key, **kw)

MAX_TAG = 40
MAX_DESC = 300
MAX_PATTERN = 200
MAX_GLOB = 200


class ValidationError(ValueError):
    pass


def _drop_controls(s: str) -> str:
    # Cc: control characters (newlines, NUL). Cf: invisible format characters, including right-to-left overrides that can make
    # one string display as another. Neither belongs in a name or description.
    return "".join(" " if c in "\t\n\r" else c for c in s if unicodedata.category(c) not in ("Cc", "Cf") or c in "\t\n\r")


def clean_text(s: str, max_len: int = MAX_DESC, what: str = "") -> str:
    """NFC-normalise, remove control and invisible characters, collapse whitespace, enforce a length limit."""
    what = what or t("what.text")
    if not isinstance(s, str):
        raise ValidationError(t("cli.val.mustBeText", what=what))
    out = re.sub(r"\s+", " ", _drop_controls(unicodedata.normalize("NFC", s))).strip()
    is_desc = what == "description"
    if not out:
        raise ValidationError(t("cli.val.descEmpty") if is_desc else t("cli.val.empty", what=what))
    if len(out) > max_len:
        raise ValidationError(t("cli.val.descTooLong", n=len(out), max=max_len) if is_desc else t("cli.val.tooLong", what=what, n=len(out), max=max_len))
    return out


def normalize_tag(name: str) -> str:
    """A tag name in any script: letters, digits and combining marks are kept (so Chinese, Arabic, Hindi and accented Latin work),
    everything else becomes '-'. Commas and slashes can never survive, which keeps the on-disk format and Dolphin's tags:/ safe."""
    s = unicodedata.normalize("NFC", _drop_controls(str(name))).lower()
    s = "".join(c if (unicodedata.category(c)[0] in "LNM" or c in "_-") else "-" for c in s)
    return re.sub(r"-{2,}", "-", s).strip("-")


def valid_tag(name: str) -> str:
    tag = normalize_tag(name)
    if not tag:
        raise ValidationError(t("cli.val.tagNeedsLetter"))
    if len(tag) > MAX_TAG:
        raise ValidationError(t("cli.val.tagTooLong", max=MAX_TAG))
    return tag


_NESTED_QUANTIFIER = re.compile(r"\((?:[^()\\]|\\.)*[+*](?:[^()\\]|\\.)*\)\s*(?:[+*]|\{\d+,?\d*\})")


def _risky(parsed, inside_repeat: bool = False) -> bool:
    """True when the parsed pattern can backtrack catastrophically: an unbounded repeat whose body contains an alternation or
    another unbounded repeat, such as `(a|aa)+` or `(a+)+`. Walks the real parse tree, so it catches shapes a text search misses."""
    import re._constants as c                                   # noqa: PLC2701  (the parse tree has no public API)
    for op, av in parsed:
        if op is c.BRANCH and inside_repeat:
            return True
        if op in (c.MAX_REPEAT, c.MIN_REPEAT):
            lo, hi, body = av
            unbounded = hi == c.MAXREPEAT or hi > 50
            if unbounded and inside_repeat:
                return True
            if _risky(body, inside_repeat or unbounded):
                return True
        elif op is c.SUBPATTERN:
            if _risky(av[-1], inside_repeat):
                return True
        elif op is c.BRANCH:
            if any(_risky(b, inside_repeat) for b in av[1]):
                return True
        elif op in (c.ASSERT, c.ASSERT_NOT):
            if _risky(av[1], inside_repeat):
                return True
        elif op is c.ATOMIC_GROUP:
            if _risky(av, inside_repeat):
                return True
    return False


def valid_pattern(p: str) -> str:
    """A regular expression for an evidence gate: must compile, stay short, and not be a known catastrophic-backtracking shape
    such as `(a+)+` (Python's `re` cannot be interrupted, so such a pattern could freeze a scan)."""
    p = _drop_controls(str(p)).strip()
    if not p:
        raise ValidationError(t("cli.val.patternEmpty"))
    if len(p) > MAX_PATTERN:
        raise ValidationError(t("cli.val.patternLong", max=MAX_PATTERN))
    try:
        re.compile(p)
    except re.error as e:
        raise ValidationError(t("cli.val.patternInvalid", err=e)) from e
    import re._parser as parser                                 # noqa: PLC2701
    if _NESTED_QUANTIFIER.search(p) or _risky(parser.parse(p)):
        raise ValidationError(t("cli.val.patternNested"))
    return p


def valid_glob(g: str) -> str:
    g = _drop_controls(str(g)).strip()
    if not g or len(g) > MAX_GLOB:
        raise ValidationError(t("cli.val.glob", max=MAX_GLOB))
    return g


def valid_unit_interval(x: float, what: str = "") -> float:
    what = what or t("what.threshold")
    try:
        v = float(x)
    except (TypeError, ValueError) as e:
        raise ValidationError(t("cli.val.unit", what=what)) from e
    if not 0.0 <= v <= 1.0:
        raise ValidationError(t("cli.val.unit", what=what))
    return v


def valid_count(x: int, lo: int, hi: int, what: str = "") -> int:
    what = what or t("what.value")
    try:
        v = int(x)
    except (TypeError, ValueError) as e:
        raise ValidationError(t("cli.val.whole", what=what)) from e
    if not lo <= v <= hi:
        raise ValidationError(t("cli.val.range", what=what, lo=lo, hi=hi))
    return v


MAX_PROMPT = 1500
_PLACEHOLDER = re.compile(r"\{(\w+)\}")


def valid_prompt(text: str, allowed: tuple[str, ...] = (), max_len: int = MAX_PROMPT) -> str:
    """A user-written prompt: control characters (except newlines) removed, length limited, only the placeholders named in `allowed`."""
    if not isinstance(text, str):
        raise ValidationError("a prompt must be text")
    out = "".join(c for c in unicodedata.normalize("NFC", text) if c == "\n" or c == "\t" or unicodedata.category(c) not in ("Cc", "Cf")).strip()
    if not out:
        raise ValidationError("the prompt cannot be empty")
    if len(out) > max_len:
        raise ValidationError(f"the prompt is too long ({len(out)} characters; the limit is {max_len})")
    bad = sorted(set(_PLACEHOLDER.findall(out)) - set(allowed))
    if bad:
        raise ValidationError("unknown placeholder " + ", ".join("{" + b + "}" for b in bad) + ("; allowed: " + ", ".join("{" + a + "}" for a in allowed) if allowed else "; this prompt takes none"))
    return out
