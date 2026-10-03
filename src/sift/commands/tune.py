"""`sift tune`: let a language model propose better descriptions, keyword gates and thresholds for your tags, and keep only the proposals that
beat the current settings when replayed on your own verdicts (`sift audit`). `sift escalate`: have a stronger backend re-judge suggestions."""
from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path

from .. import config as cfgmod
from .. import state
from ..adapters.extract import FileExtractor
from ..adapters.http import BackendError
from ..api import load_tree_for, make_classifier, make_tuner
from ..core import tune as T
from ..core.model import Profile
from ..core.runner import score_vocabulary
from ..core.tree import TagDef, Tree
from .audit import latest_verdicts

EXCERPT = 300
MAX_EXAMPLES = 3


def _fail(msg: str) -> int:
    print(f"error: {msg}", file=sys.stderr)
    return 1


def _entry(d: TagDef) -> dict:
    out = {"description": d.description, "require": d.require, "require_min": d.require_min, "act": d.act, "review": d.review,
           "detector": d.detector, "labels": list(d.labels)}
    return {k: v for k, v in out.items() if v not in (None, [], "")}


def _excerpt(text: str) -> str:
    return " ".join(text.split())[:EXCERPT]


def _save(path: Path, data: list) -> None:
    state.write_json(path, data)


def gather(sd: Path, tag: str) -> dict[str, bool]:
    done = latest_verdicts(state.read(sd / "verdicts.jsonl"))
    return {p: v in ("ok", "missed") for (p, t), v in done.items() if t == tag and v in ("ok", "wrong", "missed", "rejected")}


def tune_tag(tag: str, td: TagDef, labels: dict[str, bool], clf, tuner, ex: FileExtractor) -> dict | None:
    """Proposals for one tag, replayed on its judged files. Returns the best proposal that is strictly better, or None."""
    evs = {}
    for p in labels:
        try:
            ev = ex.extract(Path(p))
        except OSError:
            continue
        if ev.text and not ev.metadata.get("sensitive"):
            evs[p] = ev
    labels = {p: v for p, v in labels.items() if p in evs}
    if len(labels) < T.MIN_JUDGED:
        return None
    prof = Profile(0.0, 0.0)

    def scores(d: TagDef) -> dict[str, float]:
        sub = Tree("r", {}, (), {tag: d})
        return {p: score_vocabulary(ev, clf, sub, prof).get(tag, 0.0) for p, ev in evs.items()}

    act = td.act if td.act is not None else 0.8
    base = scores(td)
    before = T.measure(labels, base, act)
    fps = [_excerpt(evs[p].text or "") for p, v in labels.items() if not v and base.get(p, 0) >= act][:MAX_EXAMPLES]
    miss = [_excerpt(evs[p].text or "") for p, v in labels.items() if v and base.get(p, 0) < act][:MAX_EXAMPLES]
    if not fps and not miss:
        return None
    best = None
    for e in T.parse_edits(tuner.complete(T.build_prompt(tag, td, before, fps, miss)), tag):
        nd = T.apply_edit(td, e)
        after = T.measure(labels, scores(nd), nd.act if nd.act is not None else 0.8)
        if T.better(before, after) and (best is None or (after["precision"], after["recall"] or 0) > (best["after"]["precision"], best["after"]["recall"] or 0)):
            best = {"tag": tag, "edit": {k: v for k, v in asdict(e).items() if v not in (None, "") and k != "tag"}, "before": before, "after": after, "judged": len(labels)}
    return best


def run_tune(a, cfg) -> int:
    sd = state.state_dir()
    path = sd / "tune.json"
    props = state.read_json(path, [])
    props = [p for p in props if isinstance(p, dict) and {"tag", "edit", "before", "after", "judged"} <= set(p)] if isinstance(props, list) else []
    if a.action == "list":
        if a.json:
            print(json.dumps(props))
        for r in ([] if a.json else props):
            b, f = r["before"], r["after"]
            fmt = lambda m: f"precision {m['precision']:.2f} recall {(m['recall'] or 0):.2f}"
            print(f"{r['tag']} [{r.get('status', 'proposed')}] on {r['judged']} judged files: {fmt(b)} -> {fmt(f)}\n  change: {json.dumps(r['edit'], ensure_ascii=False)}")
        if not props and not a.json:
            print("no proposals; run `sift tune run` after judging files with `sift audit`")
        return 0
    if a.action == "clear":
        path.unlink(missing_ok=True)
        print("proposals cleared")
        return 0
    if a.action == "apply":
        if not a.tag:
            return _fail("usage: sift tune apply TAG")
        r = next((x for x in props if x["tag"] == a.tag and x.get("status", "proposed") == "proposed"), None)
        if not r:
            return _fail(f"no open proposal for {a.tag!r}")
        tree = load_tree_for(cfg)
        if a.tag not in tree.vocabulary:
            return _fail(f"tag {a.tag!r} no longer exists")
        edit = T.Edit(a.tag, **{k: v for k, v in r["edit"].items() if k != "why"})
        user = dict(cfg.get("vocabulary") or {})
        user[a.tag] = _entry(T.apply_edit(tree.vocabulary[a.tag], edit))
        cfgmod.save_user({"vocabulary": user})
        r["status"] = "applied"
        _save(path, props)
        print(f"{a.tag}: applied. Files are re-scored when they change.")
        return 0
    # run
    tuner = make_tuner(cfg)
    if tuner is None:
        return _fail("no model to propose edits: configure `proposer` (sift setup) or add a `tuner` entry to the config")
    tree = load_tree_for(cfg)
    clf = make_classifier("default", cfgmod.backends(cfg)["default"], cfg)
    ex = FileExtractor()
    tags = [a.tag] if a.tag else [t for t, d in tree.vocabulary.items() if not d.detector]
    out = [r for r in props if r.get("status") == "applied"]
    tried = 0
    for t in tags:
        if t not in tree.vocabulary:
            return _fail(f"unknown tag {t!r}")
        labels = gather(sd, t)
        if len(labels) < T.MIN_JUDGED:
            print(f"{t}: {len(labels)} judged, need {T.MIN_JUDGED}; run `sift audit next`", file=sys.stderr)
            continue
        tried += 1
        try:
            r = tune_tag(t, tree.vocabulary[t], labels, clf, tuner, ex)
        except (BackendError, OSError) as e:
            print(f"{t}: stopped: {e}", file=sys.stderr)
            break
        print(f"{t}: " + ("better settings found" if r else "nothing better found"))
        if r:
            out.append(r)
    _save(path, out)
    print(f"{tried} tags tuned; `sift tune list` shows proposals, `sift tune apply TAG` keeps one" if tried else "nothing to tune yet")
    return 0


def run_escalate(a, cfg) -> int:
    cur = (cfg.get("escalate") or {}).get("backend")
    table = cfgmod.backends(cfg)
    if a.action == "status":
        print(json.dumps({"backend": cur}) if a.json else (f"escalating suggestions to backend {cur!r}" if cur else "off"))
        return 0
    if a.action == "off":
        cfgmod.save_user({"escalate": {}})
        print("escalation off")
        return 0
    if not a.backend or a.backend not in table or a.backend == "default":
        return _fail(f"choose a configured backend other than the default (have: {', '.join(n for n in table if n != 'default') or 'none'})")
    b = table[a.backend]
    if b.get("type", "jev") == "chat" and "act" not in b:
        return _fail(f"backend {a.backend!r} is uncalibrated: give it thresholds first (`sift eval-tags run` then `sift setup --trust`), "
                     "otherwise it could only suggest and escalation would change nothing")
    cfgmod.save_user({"escalate": {"backend": a.backend}})
    print(f"suggestions will be re-judged by {a.backend!r}; confirmed tags are written")
    return 0


def register(sub) -> None:
    p = sub.add_parser("tune", help="propose better tag settings and keep only those that beat the current ones on your verdicts")
    p.add_argument("action", choices=["run", "list", "apply", "clear"])
    p.add_argument("tag", nargs="?")
    p.add_argument("--json", action="store_true")
    p.set_defaults(run=run_tune)
    q = sub.add_parser("escalate", help="have a stronger backend re-judge tags the first pass could only suggest")
    q.add_argument("action", choices=["status", "set", "off"])
    q.add_argument("backend", nargs="?")
    q.add_argument("--json", action="store_true")
    q.set_defaults(run=run_escalate)
