"""Pure data model. No I/O."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping


@dataclass(frozen=True)
class FileRef:
    """Identity of a file at classification time, re-verified before any write."""
    path: str
    inode: int
    mtime_ns: int
    size: int


@dataclass(frozen=True)
class Evidence:
    """A bundle of parts. v1 implements text and filename; image is reserved."""
    ref: FileRef
    filename: str
    mime: str = ""
    text: str | None = None
    image: bytes | None = None
    metadata: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Choice:
    instructions: str
    labels: Mapping[str, str | None]


@dataclass(frozen=True)
class Multi:
    """Each tag is asked as an independent yes/no question."""
    instructions: str
    tags: Mapping[str, str | None]


Question = Choice | Multi


@dataclass(frozen=True)
class Answer:
    choice: str
    probabilities: Mapping[str, float]


@dataclass(frozen=True)
class Capabilities:
    modalities: frozenset[str]
    kinds: frozenset[str]
    max_labels: int
    max_chars: int
    calibrated: bool


@dataclass(frozen=True)
class Profile:
    """Confidence thresholds for one backend, normally produced by `sift eval`."""
    act: float = 0.8
    review: float = 0.4


class Outcome(str, Enum):
    ACT = "act"              # confident end to end: tags may be written
    REVIEW = "review"        # partial or middling confidence: a human decides
    UNDECIDED = "undecided"  # nothing usable


@dataclass(frozen=True)
class Step:
    node: str
    label: str
    probability: float
    basis: str  # "text" | "filename"
    probabilities: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class Decision:
    ref: FileRef
    steps: tuple[Step, ...]
    outcome: Outcome
    tags: tuple[str, ...] = ()
    suggested: tuple[str, ...] = ()
    reason: str = ""
    scores: Mapping[str, float] = field(default_factory=dict)  # vocabulary tag -> best chunk probability
