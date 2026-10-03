"""Validation of everything a user can type. The logic is pure and lives in `core/validation.py`; this module connects it to the user's
language and is what the rest of the application imports."""
from __future__ import annotations

from .core import validation as _v
from .core.validation import (MAX_DESC, MAX_GLOB, MAX_PATTERN, MAX_PROMPT, MAX_TAG, ValidationError, _drop_controls, clean_text,  # noqa: F401
                              normalize_tag, valid_count, valid_glob, valid_pattern, valid_prompt, valid_tag, valid_unit_interval)
from .i18n import t

_v.set_translator(t)
