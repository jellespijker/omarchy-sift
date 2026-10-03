"""Tag discovery: cluster proposed tag names and measure whether a candidate separates files. Pure logic; I/O lives in the CLI."""
from __future__ import annotations

import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Iterable, Mapping

from .core.model import Choice
from .core.runner import Classifier

_SUFFIXES = ("ing", "es", "s")


def _stem(tok: str) -> str:
    for suf in _SUFFIXES:
        if tok.endswith(suf) and len(tok) - len(suf) >= 4:
            return tok[: -len(suf)]
    return tok


def canon(tag: str) -> frozenset[str]:
    """Order- and plural-insensitive key: 'build-scripts' and 'script-build' map to the same key. Any script works; only plain
    ASCII words are stemmed."""
    words = ("".join(c if unicodedata.category(c)[0] in "LNM" else " " for c in tag.lower())).split()
    return frozenset(_stem(w) if w.isascii() else w for w in words)


def describe(tag: str) -> str:
    return f"The text is about {tag.replace('-', ' ')}."


@dataclass
class Candidate:
    tag: str
    support: int
    files: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)


def cluster(proposals: Iterable[Mapping], existing: Iterable[str] = (), rejected: Iterable[str] = (), min_support: int = 3) -> list[Candidate]:
    """Merge near-duplicate tag names, count the files behind each, and rank by support."""
    blocked = {canon(t) for t in [*existing, *rejected]}
    names: dict[frozenset[str], Counter] = defaultdict(Counter)
    files: dict[frozenset[str], set[str]] = defaultdict(set)
    for r in proposals:
        for t in set(r.get("tags", [])):
            k = canon(t)
            if not k or k in blocked:
                continue
            names[k][t] += 1
            files[k].add(r["path"])
    out = []
    for k, fs in files.items():
        if len(fs) < min_support:
            continue
        best = sorted(names[k].items(), key=lambda kv: (-kv[1], len(kv[0]), kv[0]))
        out.append(Candidate(best[0][0], len(fs), sorted(fs), [n for n, _ in best[1:]]))
    return sorted(out, key=lambda c: (-c.support, c.tag))


@dataclass
class Verdict:
    tag: str
    recall: float        # share of the candidate's own files that score at or above `hit`
    prevalence: float    # share of other files that also score at or above `hit` (high = generic noise)
    good: bool


def evaluate(cands: list[Candidate], texts: Mapping[str, str], clf: Classifier, hit: float = 0.6, batch: int = 8,
             per_candidate: int = 5, min_recall: float = 0.5, max_prevalence: float = 0.25) -> list[Verdict]:
    """Score each candidate as a yes/no tag on its own files plus everyone else's. `texts` maps path -> first chunk of text."""
    verdicts: list[Verdict] = []
    for i in range(0, len(cands), batch):
        group = cands[i:i + batch]
        members = {c.tag: [p for p in c.files if p in texts][:per_candidate] for c in group}
        test = sorted(set(texts) if len(texts) <= 60 else {p for ps in members.values() for p in ps} | set(sorted(texts)[:20]))
        hits: dict[tuple[str, str], bool] = {}
        for path in test:
            qs = {c.tag: Choice(f"Does this text have the tag '{c.tag}'? {describe(c.tag)}", {"yes": "the tag applies", "no": "the tag does not apply"})
                  for c in group}
            for tag, ans in clf.ask(texts[path], qs).items():
                hits[(tag, path)] = ans.probabilities.get("yes", 0.0) >= hit
        for c in group:
            own = members[c.tag]
            others = [p for p in test if p not in set(c.files)]
            recall = sum(hits[(c.tag, p)] for p in own) / len(own) if own else 0.0
            prev = sum(hits[(c.tag, p)] for p in others) / len(others) if others else 0.0
            verdicts.append(Verdict(c.tag, recall, prev, recall >= min_recall and prev <= max_prevalence))
    return verdicts
