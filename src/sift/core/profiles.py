"""Prompt profiles: different instructions, tags or thresholds for specific directories, file types or name patterns. Pure.

A profile matches when ALL of its given criteria match (dirs, extensions, globs). When several match, the most specific wins:
the longest matching directory, then the most criteria, then the first in the list. A profile derives a changed copy of the
tree; the base tree is never mutated.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from fnmatch import fnmatchcase
from typing import Any, Mapping

from .validation import (ValidationError, clean_text, valid_glob, valid_prompt, valid_tag, valid_unit_interval)
from . import prompts as P
from .tree import Node, TagDef, Tree, parse_tagdef


@dataclass(frozen=True)
class Profile:
    name: str
    dirs: tuple[str, ...] = ()          # absolute directory prefixes (the caller expands "~")
    extensions: frozenset[str] = frozenset()   # lowercase with dot
    globs: tuple[str, ...] = ()         # matched against the file name, case-insensitive
    skip: bool = False                  # leave these files alone entirely
    tags: tuple[str, ...] | None = None  # allow only these descriptive tags (None = all, () = none)
    vocabulary: Mapping[str, Any] = field(default_factory=dict)   # extra tags that exist only here
    prompts: Mapping[str, str] = field(default_factory=dict)
    node_prompts: Mapping[str, str] = field(default_factory=dict)  # node id -> replacement question text
    exclude_labels: tuple[str, ...] = ()  # labels removed from the root question (for example "code" for PDFs)
    act: float | None = None
    review: float | None = None
    skip_vocabulary: tuple[str, ...] | None = None   # first-question labels that get no general tags (None = packaged default)


def _under(path: str, d: str) -> bool:
    d = d.rstrip("/") or "/"
    return path == d or path.startswith(d + "/") or d == "/"


def specificity(p: Profile, path: str) -> tuple[int, int] | None:
    """None when the profile does not match this file; otherwise a sortable score."""
    name = path.rsplit("/", 1)[-1]
    dot = name.rfind(".")
    ext = name[dot:].lower() if dot > 0 else ""
    best_dir = 0
    if p.dirs:
        hits = [len(d.rstrip("/")) for d in p.dirs if _under(path, d)]
        if not hits:
            return None
        best_dir = max(hits)
    if p.extensions and ext not in p.extensions:
        return None
    if p.globs and not any(fnmatchcase(name.lower(), g.lower()) for g in p.globs):
        return None
    return best_dir, bool(p.dirs) + bool(p.extensions) + bool(p.globs)


def select(profiles: tuple[Profile, ...], path: str) -> Profile | None:
    scored = [(s, -i, p) for i, p in enumerate(profiles) if (s := specificity(p, path)) is not None]
    return max(scored, key=lambda x: (x[0], x[1]))[2] if scored else None


def derive(tree: Tree, p: Profile) -> Tree:
    """The tree this profile scans with."""
    vocab = {t: d for t, d in tree.vocabulary.items() if p.tags is None or t in p.tags}
    vocab.update(p.vocabulary)
    if p.act is not None or p.review is not None:
        vocab = {t: replace(d, act=p.act if p.act is not None else d.act, review=p.review if p.review is not None else d.review)
                 for t, d in vocab.items()}
    nodes = dict(tree.nodes)
    for nid, text in p.node_prompts.items():
        if nid in nodes:
            nodes[nid] = replace(nodes[nid], instructions=text)
    if p.exclude_labels and tree.root in nodes:
        r = nodes[tree.root]
        keep = {k: v for k, v in r.labels.items() if k not in p.exclude_labels or k == "other"}
        nodes[tree.root] = replace(r, labels=keep, children={k: v for k, v in r.children.items() if k in keep})
    skipv = tree.vocabulary_skip if p.skip_vocabulary is None else frozenset(p.skip_vocabulary)
    return replace(tree, nodes=nodes, vocabulary=vocab, vocabulary_skip=skipv, prompts={**tree.prompts, **p.prompts})


def parse_prompts(raw: Any, where: str) -> tuple[dict[str, str], dict[str, str], list[str]]:
    """(template prompts, node prompts, problems) from a user-edited {"tag_question": ..., "nodes": {...}} object."""
    out: dict[str, str] = {}
    nodes: dict[str, str] = {}
    problems: list[str] = []
    for k, v in (raw if isinstance(raw, dict) else {}).items():
        try:
            if k == "nodes" and isinstance(v, dict):
                for nid, text in v.items():
                    nodes[str(nid)] = valid_prompt(text)
            elif k in P.ALLOWED:
                out[k] = valid_prompt(v, P.ALLOWED[k])
            else:
                problems.append(f"{where}: unknown prompt {k!r} ignored")
        except ValidationError as e:
            problems.append(f"{where}: prompt {k!r} ignored: {e}")
    return out, nodes, problems


def parse_profile(raw: Mapping[str, Any]) -> Profile:
    """One profile from user data; raises ValidationError when unusable (the caller skips it and reports)."""
    if not isinstance(raw, dict):
        raise ValidationError("a profile must be an object")
    name = clean_text(raw.get("name", ""), 60, what="name")
    m = raw.get("match") or {}
    dirs = tuple(str(d) for d in m.get("dirs", []) if str(d).strip())
    exts = frozenset(("." + e.lstrip(".")).lower() for e in map(str, m.get("extensions", [])) if e.strip(". "))
    globs = tuple(valid_glob(g) for g in m.get("globs", []))
    if not (dirs or exts or globs):
        raise ValidationError("a profile needs at least one match: dirs, extensions or globs")
    tags = tuple(valid_tag(t) for t in raw["tags"]) if raw.get("tags") is not None else None
    vocab: dict[str, TagDef] = {}
    for tn, tv in (raw.get("vocabulary") or {}).items():
        tag, td = parse_tagdef(tn, tv)
        vocab[tag] = td
    pr, nodes, problems = parse_prompts(raw.get("prompts"), f"profile {name!r}")
    if problems and not (pr or nodes):
        raise ValidationError(problems[0])
    return Profile(name, dirs, exts, globs, bool(raw.get("skip")), tags, vocab, pr, nodes,
                   tuple(str(x) for x in raw.get("exclude_labels", [])),
                   valid_unit_interval(raw["act"]) if raw.get("act") is not None else None,
                   valid_unit_interval(raw["review"]) if raw.get("review") is not None else None,
                   tuple(str(x) for x in raw["skip_vocabulary"]) if raw.get("skip_vocabulary") is not None else None)


def load_profiles(packaged: list, user: list) -> tuple[tuple[Profile, ...], list[str]]:
    """Packaged defaults first, then the user's; a user profile with the same name replaces the packaged one."""
    by_name: dict[str, Profile] = {}
    problems: list[str] = []
    for src in (packaged or [], user or []):
        for raw in src:
            try:
                p = parse_profile(raw)
            except (ValidationError, KeyError, TypeError, AttributeError) as e:
                problems.append(f"profile {raw.get('name', '?') if isinstance(raw, dict) else '?'!s} skipped: {e}")
                continue
            by_name[p.name] = p
    return tuple(by_name.values()), problems
