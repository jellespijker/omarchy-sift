"""Ports: the places where I/O happens. Implementations live in sift.adapters."""
from __future__ import annotations

from pathlib import Path
from typing import Protocol, Sequence

from .core.model import Decision, Evidence
from .core.runner import Classifier

__all__ = ["Classifier", "Extractor", "TagStore"]


class Extractor(Protocol):
    def extract(self, path: Path) -> Evidence: ...


class TagStore(Protocol):
    def apply(self, decision: Decision, tags: Sequence[str]) -> str:
        """Record tags for the decision's file. Returns a short status string."""
