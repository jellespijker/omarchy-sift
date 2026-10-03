"""Command modules. Each module exposes `register(sub)` which adds its subparsers and sets `run` as their handler."""
from __future__ import annotations

import importlib

MODULES = ("dirs", "schedule", "tags", "setup", "doctor", "evaluate", "audit", "preview", "secret", "info", "demo_cmd", "lang", "profiles", "tune", "addtag")


def register_all(sub) -> None:
    for name in MODULES:
        importlib.import_module(f"{__name__}.{name}").register(sub)
