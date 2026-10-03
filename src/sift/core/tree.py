"""Decision tree loaded from data. Pure."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .validation import ValidationError, clean_text, valid_count, valid_pattern, valid_tag, valid_unit_interval


class TreeError(ValueError):
    pass


@dataclass(frozen=True)
class Node:
    id: str
    kind: str                          # "choice" | "multi"
    instructions: str
    labels: Mapping[str, str | None]   # label (or tag, for multi) -> description
    children: Mapping[str, str]        # label -> child node id
    input: str = "text"                # "text" | "filename"
    backend: str = "default"
    tag_prefix: str = ""               # multi nodes: prefix for emitted tags


@dataclass(frozen=True)
class Rule:
    """Deterministic early exit by file extension. No model call."""
    exts: frozenset[str]
    tag: str


@dataclass(frozen=True)
class TagDef:
    """A descriptive tag scored yes/no per text chunk. None thresholds fall back to the backend profile."""
    description: str
    act: float | None = None
    review: float | None = None
    detector: str | None = None     # e.g. "language:nl": decided by code, never by the classifier
    require: str | None = None      # regex that must match at least `require_min` times in the text, or the tag is dropped
    require_min: int = 2
    labels: tuple[str, ...] = ()    # only for files whose first-question label is one of these (empty = general tag)


@dataclass(frozen=True)
class Tree:
    root: str
    nodes: Mapping[str, Node]
    rules: tuple[Rule, ...] = ()
    vocabulary: Mapping[str, TagDef] = field(default_factory=dict)
    vocabulary_skip: frozenset[str] = frozenset()  # root labels (when confident) that skip descriptive tagging
    tag_map: Mapping[str, str] = field(default_factory=dict)  # user renames and merges: emitted tag -> final tag
    problems: tuple[str, ...] = ()                             # user data that was skipped, for `sift doctor`
    prompts: Mapping[str, str] = field(default_factory=dict)  # editable prompt templates (see core/prompts.py)
    profiles: tuple[Any, ...] = ()                             # prompt profiles (core/profiles.py), by directory / type / name

    def rule_for(self, filename: str) -> Rule | None:
        dot = filename.rfind(".")
        ext = filename[dot:].lower() if dot > 0 else ""
        return next((r for r in self.rules if ext in r.exts), None)

    def node(self, node_id: str) -> Node:
        return self.nodes[node_id]

    def child(self, node: Node, label: str) -> Node | None:
        cid = node.children.get(label)
        return self.nodes[cid] if cid else None


def parse_tagdef(name: str, raw: Any) -> tuple[str, TagDef]:
    """One vocabulary entry from user or packaged data; raises ValidationError (or KeyError/TypeError) when it is unusable."""
    d = raw if isinstance(raw, dict) else {"description": raw}
    req = valid_pattern(d["require"]) if d.get("require") else None
    return valid_tag(name), TagDef(clean_text(d.get("description", ""), what="description"),
                                   valid_unit_interval(d["act"]) if d.get("act") is not None else None,
                                   valid_unit_interval(d["review"]) if d.get("review") is not None else None,
                                   d.get("detector"), req, valid_count(d.get("require_min", 2), 1, 100, "require_min"),
                                   tuple(str(x) for x in d.get("labels", [])))


def load_tree(data: Mapping[str, Any], extra_vocabulary: Mapping[str, Any] | None = None,
              tag_map: Mapping[str, str] | None = None, prompts: Mapping[str, str] | None = None) -> Tree:
    nodes: dict[str, Node] = {}
    for nid, raw in data.get("nodes", {}).items():
        kind = raw.get("kind", "choice")
        if kind not in ("choice", "multi"):
            raise TreeError(f"node {nid}: unknown kind {kind!r}")
        labels = raw.get("labels") or raw.get("tags") or {}
        if kind == "choice" and len(labels) < 2:
            raise TreeError(f"node {nid}: a choice needs at least 2 labels")
        if kind == "choice" and "other" not in labels:
            raise TreeError(f"node {nid}: every choice node needs an 'other' label")
        children = dict(raw.get("children", {}))
        for lab, cid in children.items():
            if lab not in labels:
                raise TreeError(f"node {nid}: child label {lab!r} is not a label")
            if cid not in data["nodes"]:
                raise TreeError(f"node {nid}: unknown child node {cid!r}")
        nodes[nid] = Node(nid, kind, raw["instructions"], dict(labels), children,
                          raw.get("input", "text"), raw.get("backend", "default"),
                          raw.get("tag_prefix", ""))
    root = data.get("root", "root")
    if root not in nodes:
        raise TreeError(f"root node {root!r} missing")
    rules = tuple(Rule(frozenset(e.lower() for e in r["ext"]), r["tag"]) for r in data.get("rules", []))
    vocab: dict[str, TagDef] = {}
    problems: list[str] = []
    for name, raw in {**data.get("vocabulary", {}), **(extra_vocabulary or {})}.items():
        # User-edited data must never stop a scan: a bad entry is skipped and reported (see `sift doctor`).
        try:
            tag, td = parse_tagdef(name, raw)
            vocab[tag] = td
        except (ValidationError, KeyError, TypeError) as e:
            problems.append(f"vocabulary tag {name!r} skipped: {e}")
    return Tree(root, nodes, rules, vocab, frozenset(data.get("vocabulary_skip", [])), dict(tag_map or {}), tuple(problems), dict(prompts or {}))
