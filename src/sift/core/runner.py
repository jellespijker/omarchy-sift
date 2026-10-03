"""Run evidence through the tree. Pure: all I/O happens behind the Classifier port."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import replace
from typing import Mapping, Protocol

from .lang import matches_detector
from .prompts import TAG_QUESTION, render
from .model import (Answer, Capabilities, Choice, Decision, Evidence, Multi, Outcome,
                    Profile, Question, Step)
from .tree import Node, Tree

MAX_DEPTH = 6
MAX_CHUNKS = 3
VOCAB_BATCH = 8
MIN_VOCAB_CHARS = 200
UNINFORMATIVE = {"other"}


class Classifier(Protocol):
    capabilities: Capabilities

    def ask(self, text: str, questions: Mapping[str, Question]) -> Mapping[str, Answer]: ...


def sanitize_tag(tag: str) -> str:
    """Segments joined by '/'. Letters, digits and marks in any script are kept; everything else is replaced. Empty or traversal-like
    segments are dropped. (Written tags later flatten '/' to '-'.)"""
    segs = []
    for seg in tag.split("/"):
        seg = "".join(c if (unicodedata.category(c)[0] in "LNM" or c in "_-") else "-" for c in unicodedata.normalize("NFC", seg).lower())
        seg = re.sub(r"-{2,}", "-", seg).strip("-")[:40]
        if seg and seg not in (".", ".."):
            segs.append(seg)
    return "/".join(segs)


def view_for(ev: Evidence, node: Node, max_chars: int) -> tuple[str, str]:
    """Return (state_text, basis). Filename-only nodes and text-less files use the filename."""
    if node.input == "text" and ev.text and ev.text.strip():
        return f"filename: {ev.filename}\n\n{ev.text[:max_chars]}", "text"
    return f"filename: {ev.filename}", "filename"


def chunks(text: str, width: int, limit: int = MAX_CHUNKS) -> list[str]:
    """Up to `limit` evenly spaced windows covering the text (topic words can appear anywhere)."""
    if len(text) <= width:
        return [text]
    n = min(limit, -(-len(text) // width))
    return [text[int(i * (len(text) - width) / max(n - 1, 1)):][:width] for i in range(n)]


def tag_gate(d, text: str) -> bool:
    """Deterministic evidence check for a vocabulary tag: its `require` regex must match often enough."""
    if not d.require:
        return True
    try:
        return len(re.findall(d.require, text.lower())) >= d.require_min
    except re.error:                      # a hand-edited bad pattern must never stop a scan; the tag simply does not apply
        return False


def applicable(tree: Tree, label: str | None) -> Tree:
    """The tree restricted to the tags that apply to a file with this confident first-question label (None = not confident).
    General tags apply unless the label is in `vocabulary_skip`; label-specific tags apply only to their labels."""
    skipped = label in tree.vocabulary_skip
    keep = {t: d for t, d in tree.vocabulary.items() if (label in d.labels if d.labels else not skipped)}
    return tree if len(keep) == len(tree.vocabulary) else replace(tree, vocabulary=keep)


def score_vocabulary(ev: Evidence, clf: Classifier, tree: Tree, profile: Profile, gated: dict[str, float] | None = None) -> dict[str, float]:
    """Best yes-probability per vocabulary tag over text chunks, all tags batched in one request per chunk.
    Tags the evidence gate drops score 0; their model score goes into `gated` (when given) so they can be offered as suggestions."""
    if not tree.vocabulary or not ev.text or len(ev.text.strip()) < MIN_VOCAB_CHARS or "choice" not in clf.capabilities.kinds:
        return {}
    width = int(clf.capabilities.max_chars * 0.75)
    best = {t: 0.0 for t in tree.vocabulary}
    for t, d in tree.vocabulary.items():                      # detector tags are decided by code
        if d.detector:
            best[t] = 1.0 if matches_detector(d.detector, ev.text) else 0.0
    model_tags = [t for t, d in tree.vocabulary.items() if not d.detector]
    for part in chunks(ev.text, width) if model_tags else []:
        names = model_tags
        for i in range(0, len(names), VOCAB_BATCH):   # small batches keep server memory use bounded
            qs: dict[str, Question] = {
                t: Choice(render(tree.prompts.get("tag_question") or TAG_QUESTION, tag=t, description=tree.vocabulary[t].description),
                          {"yes": "the tag applies", "no": "the tag does not apply"})
                for t in names[i:i + VOCAB_BATCH]}
            for t, a in clf.ask(f"filename: {ev.filename}\n\n{part}", qs).items():
                best[t] = max(best[t], a.probabilities.get("yes", 0.0))
    for t, d in tree.vocabulary.items():                      # evidence gate: drop tags the text gives no keywords for
        if best[t] and not tag_gate(d, ev.text):
            if gated is not None:
                gated[t] = best[t]
            best[t] = 0.0
    return best


def run(ev: Evidence, classifiers: Mapping[str, Classifier], tree: Tree,
        profiles: Mapping[str, Profile] | None = None) -> Decision:
    profiles = profiles or {}
    if ev.metadata.get("sensitive"):
        return Decision(ev.ref, (), Outcome.REVIEW, reason="possible secret: not sent to any classifier, never tagged")
    rule = tree.rule_for(ev.filename)
    if rule:
        return Decision(ev.ref, (Step("rule", rule.tag, 1.0, "rule"),), Outcome.ACT, tags=(sanitize_tag(rule.tag),))
    steps: list[Step] = []
    path: list[str] = []
    extra: list[str] = []
    node: Node | None = tree.node(tree.root)
    outcome = Outcome.ACT
    reason = ""

    while node is not None and len(steps) < MAX_DEPTH:
        clf = classifiers.get(node.backend) or classifiers["default"]
        caps = clf.capabilities
        profile = profiles.get(node.backend) or profiles.get("default") or Profile()
        text, basis = view_for(ev, node, caps.max_chars)
        if basis == "text" and "text" not in caps.modalities:
            text, basis = f"filename: {ev.filename}", "filename"
        if node.kind not in caps.kinds:
            return Decision(ev.ref, tuple(steps), Outcome.REVIEW, suggested=tuple(path),
                            reason=f"backend lacks {node.kind} questions")
        if node.kind == "choice" and len(node.labels) > caps.max_labels:
            return Decision(ev.ref, tuple(steps), Outcome.REVIEW, suggested=tuple(path),
                            reason=f"node {node.id} exceeds backend max_labels")

        if node.kind == "multi":
            qs: dict[str, Question] = {
                t: Choice(f"{node.instructions} Tag: {t}", {"yes": d, "no": "does not apply"})
                for t, d in node.labels.items()}
            answers = clf.ask(text, qs)
            for t, a in answers.items():
                p = a.probabilities.get("yes", 0.0)
                if a.choice == "yes" and p >= profile.act and basis == "text":
                    extra.append(f"{node.tag_prefix}{t}")
            steps.append(Step(node.id, ",".join(sorted(extra)) or "-", 1.0, basis))
            break

        ans = clf.ask(text, {node.id: Choice(node.instructions, node.labels)})[node.id]
        p = ans.probabilities.get(ans.choice, 0.0)
        steps.append(Step(node.id, ans.choice, p, basis, dict(ans.probabilities)))
        if p < profile.review:
            outcome, reason = Outcome.UNDECIDED if not path else Outcome.REVIEW, f"low confidence at {node.id}"
            break
        if p < profile.act:
            outcome, reason = Outcome.REVIEW, f"middling confidence at {node.id}"
            path.append(ans.choice)
            break
        if ans.choice == "other":
            path.append(ans.choice)
            break
        path.append(ans.choice)
        node = tree.child(node, ans.choice)

    if outcome is Outcome.ACT and not caps_ok(classifiers, profiles, steps, tree):
        outcome, reason = Outcome.REVIEW, "backend not calibrated"
    if outcome is Outcome.ACT and any(s.basis == "filename" for s in steps):
        outcome, reason = Outcome.REVIEW, "filename-only evidence never acts" + (
            f" ({ev.metadata['missing_tool']} is not installed, so the text could not be read)" if ev.metadata.get("missing_tool") else "")
    dprofile = profiles.get("default") or Profile()
    label = path[0] if outcome is Outcome.ACT and path else None
    vtree = applicable(tree, label)
    gated: dict[str, float] = {}
    scores = score_vocabulary(ev, classifiers["default"], vtree, dprofile, gated)
    vact: list[str] = []
    vsug: list[str] = []
    for t, sc in scores.items():
        d = tree.vocabulary[t]
        act_at = d.act if d.act is not None else dprofile.act
        rev_at = d.review if d.review is not None else dprofile.review
        if sc >= act_at and clf_calibrated(classifiers, profiles):
            vact.append(sanitize_tag(t))
        elif sc >= rev_at:
            vsug.append(sanitize_tag(t))
    gate_missed = []
    for t, raw in gated.items():                            # the model said yes but the text lacks the keywords: a human decides
        d = tree.vocabulary[t]
        if raw >= (d.review if d.review is not None else dprofile.review):
            vsug.append(sanitize_tag(t))
            gate_missed.append(t)
    if gate_missed and not reason:
        reason = "suggested without keyword evidence: " + ", ".join(gate_missed)
    tag = sanitize_tag("/".join(path))
    informative = bool(tag) and tag.split("/")[-1] not in UNINFORMATIVE
    path_tags = (tag,) if outcome is Outcome.ACT and informative else ()
    incomplete = bool(steps) and steps[-1].node in tree.nodes and steps[-1].label in tree.node(steps[-1].node).children
    path_sugg = (tag,) if outcome is not Outcome.ACT and tag and not incomplete else ()
    tags = tuple(dict.fromkeys([*path_tags, *vact]))
    suggested = tuple(dict.fromkeys([*path_sugg, *vsug]))
    extra_tags = tuple(t for t in map(sanitize_tag, extra) if t)
    tags = tuple(dict.fromkeys([*tags, *extra_tags])) if outcome is Outcome.ACT else tags
    tags = tuple(t for t in dict.fromkeys(tree.tag_map.get(t, t) for t in tags) if t)          # user renames, merges, deletions
    suggested = tuple(t for t in dict.fromkeys(tree.tag_map.get(t, t) for t in suggested) if t and t not in tags)
    if tags:
        return Decision(ev.ref, tuple(steps), Outcome.ACT, tags=tags, suggested=suggested, reason=reason if suggested else "", scores=scores)
    if suggested or outcome is Outcome.REVIEW:
        return Decision(ev.ref, tuple(steps), Outcome.REVIEW, suggested=suggested, reason=reason, scores=scores)
    return Decision(ev.ref, tuple(steps), outcome, reason=reason, scores=scores)


def clf_calibrated(classifiers: Mapping[str, Classifier], profiles: Mapping[str, Profile]) -> bool:
    c = classifiers["default"]
    return c.capabilities.calibrated or "default" in profiles


def caps_ok(classifiers: Mapping[str, Classifier], profiles: Mapping[str, Profile],
            steps: list[Step], tree: Tree) -> bool:
    for s in steps:
        name = tree.node(s.node).backend
        clf = classifiers.get(name) or classifiers["default"]
        if not clf.capabilities.calibrated and name not in profiles and "default" not in profiles:
            return False
    return True
