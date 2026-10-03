"""Look at the real precision of the tags: `sift open` (preview in the default viewer) and `sift audit` (judge Sift's tags).

`audit next` picks files carrying descriptive tags Sift wrote, balanced across tags and skipping what you already judged. For each
tag you look at the file (`sift open`) and mark it ok or wrong; a wrong tag is removed from the file (logged for undo). `audit report`
turns the verdicts into a per-tag precision with an honest lower bound, and `audit export` writes them as a labels sheet for
`sift eval-tags`.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Iterable, Mapping

from .. import config as cfgmod
from .. import state
from ..adapters.stores import StoreError, XattrStore, remove_tags
from ..api import load_tree_for
from ..core.model import Decision, FileRef, Outcome
from ..validate import ValidationError, valid_tag

VERDICTS = ("ok", "wrong", "missed")


def flat(t: str) -> str:
    return t.replace("/", "-")


def wilson_lower(k: int, n: int, z: float = 1.96) -> float:
    """Lower bound of the 95% Wilson interval for k successes out of n: what the true precision is at least, with confidence."""
    if n == 0:
        return 0.0
    p = k / n
    d = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (centre - margin) / d)


def latest_verdicts(entries: Iterable[Mapping]) -> dict[tuple[str, str], str]:
    """The newest verdict per (path, tag)."""
    out: dict[tuple[str, str], str] = {}
    for e in entries:
        out[(e["path"], e["tag"])] = e["verdict"]
    return out


def precision_table(verdicts: Mapping[tuple[str, str], str]) -> list[dict]:
    per: dict[str, dict[str, int]] = defaultdict(lambda: {"ok": 0, "wrong": 0, "missed": 0})
    for (_, tag), v in verdicts.items():
        if v in per[tag]:                       # review rejections are not audit judgements of written tags
            per[tag][v] += 1
    rows = []
    for tag, c in per.items():
        n = c["ok"] + c["wrong"]
        rows.append({"tag": tag, "judged": n, "ok": c["ok"], "wrong": c["wrong"], "missed": c["missed"],
                     "precision": c["ok"] / n if n else None, "at_least": wilson_lower(c["ok"], n) if n else None,
                     "recall": c["ok"] / (c["ok"] + c["missed"]) if c["ok"] + c["missed"] else None})
    return sorted(rows, key=lambda r: (-r["judged"], r["tag"]))


def pick(cfg: dict, sd: Path, n: int, only_tag: str | None = None, seed: int | None = None) -> list[dict]:
    """Files to audit: those with unjudged descriptive tags, favouring the tags that have the fewest judgements so far."""
    vocab = set(load_tree_for(cfg).vocabulary)
    done = latest_verdicts(state.read(sd / "verdicts.jsonl"))
    judged_per_tag: dict[str, int] = defaultdict(int)
    for (_, tag), v in done.items():
        judged_per_tag[tag] += 1
    pool: dict[str, list[dict]] = defaultdict(list)
    for p, e in state.latest_by_path(state.read(sd / "report.jsonl")).items():
        if not state.usable(e):
            continue
        todo = [flat(t) for t in e.get("tags", []) if flat(t) in vocab and (p, flat(t)) not in done and (not only_tag or flat(t) == only_tag)]
        if todo and Path(p).is_file():
            for t in todo:
                pool[t].append({"path": p, "tags": todo, "all_tags": [flat(t) for t in e.get("tags", [])]})
    salt = str(seed if seed is not None else 0)
    for v in pool.values():                       # a stable pseudo-random order: judging a file only removes it, nothing reshuffles
        v.sort(key=lambda it: hashlib.sha1((salt + it["path"]).encode()).hexdigest(), reverse=True)
    chosen: dict[str, dict] = {}
    order = sorted(pool, key=lambda t: (judged_per_tag[t], t))
    while len(chosen) < n and any(pool.values()):
        for t in order:
            while pool[t] and pool[t][-1]["path"] in chosen:
                pool[t].pop()
            if pool[t]:
                item = pool[t].pop()
                chosen[item["path"]] = item
                if len(chosen) >= n:
                    break
    return [{"path": p, "name": Path(p).name, "tags": it["tags"], "also": [t for t in it["all_tags"] if t not in it["tags"]]}
            for p, it in chosen.items()]


def mark(cfg: dict, sd: Path, path: str, tag: str, verdict: str, keep: bool = False) -> str:
    """Record a verdict. `wrong` removes the tag from the file; `missed` adds it. Both are logged for `sift untag`."""
    tag = valid_tag(tag)
    real = Path(path).resolve(strict=True)
    st = os.stat(real, follow_symlinks=False)
    ref = FileRef(str(real), st.st_ino, st.st_mtime_ns, st.st_size)
    store = XattrStore(cfgmod.dirs(cfg), sd / "undo.jsonl")
    note = ""
    if verdict == "wrong" and not keep:
        note = remove_tags(store, Decision(ref, (), Outcome.ACT), [tag])
    elif verdict == "missed":
        store.apply(Decision(ref, (), Outcome.ACT, tags=(tag,)), [tag])
        note = "added"
    state.append(sd / "verdicts.jsonl", {"path": str(real), "tag": tag, "verdict": verdict, "ref": [st.st_ino, st.st_mtime_ns, st.st_size]})
    return note


def export_labels(cfg: dict, sd: Path, out: Path) -> int:
    """A labels sheet for `sift eval-tags run`: for each judged file, the tags that were confirmed or added."""
    tree = load_tree_for(cfg)
    verdicts = latest_verdicts(state.read(sd / "verdicts.jsonl"))
    files: dict[str, set[str]] = defaultdict(set)
    judged: set[str] = set()
    for (p, tag), v in verdicts.items():
        judged.add(p)
        if v in ("ok", "missed"):
            files[p].add(tag)
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["path", "predicted", "truth", "note"])
        for p in sorted(judged):
            w.writerow([p, "", ";".join(sorted(files[p])), "audited"])
    return len(judged)


def run(a, cfg) -> int:
    sd = state.state_dir()
    try:
        if a.action == "next":
            items = pick(cfg, sd, a.n, a.tag)
            print(json.dumps(items) if a.json else "\n".join(f"{i['name']}: {', '.join(i['tags'])}" for i in items) or "(nothing left to audit)")
            return 0
        if a.action == "mark":
            if not (a.path and a.tag and a.verdict in VERDICTS):
                print(f"usage: sift audit mark PATH TAG {'|'.join(VERDICTS)} [--keep]", file=sys.stderr)
                return 2
            print(mark(cfg, sd, a.path, a.tag, a.verdict, a.keep) or "recorded")
            return 0
        if a.action == "report":
            rows = precision_table(latest_verdicts(state.read(sd / "verdicts.jsonl")))
            if a.json:
                print(json.dumps(rows))
            else:
                for r in rows:
                    prec = "n/a" if r["precision"] is None else f"{r['precision']:.0%} ({r['ok']}/{r['judged']}, at least {r['at_least']:.0%})"
                    print(f"{r['tag']:<24} judged {r['judged']:>3}  precision {prec}" + (f"  missed {r['missed']}" if r["missed"] else ""))
                if not rows:
                    print("(no verdicts yet: run `sift audit next` and mark what you see)")
            return 0
        if a.action == "export":
            n = export_labels(cfg, sd, Path(a.out))
            print(f"wrote {n} files to {a.out}; run `sift eval-tags run {a.out}`")
            return 0
    except (StoreError, ValidationError, OSError, cfgmod.ConfigError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 2


def register(sub) -> None:
    p = sub.add_parser("audit", help="judge Sift's tags to measure real precision: next | mark | report | export")
    p.add_argument("action", choices=["next", "mark", "report", "export"])
    p.add_argument("path", nargs="?"); p.add_argument("tag", nargs="?"); p.add_argument("verdict", nargs="?", choices=[*VERDICTS, None])
    p.add_argument("-n", type=int, default=8); p.add_argument("--json", action="store_true")
    p.add_argument("--keep", action="store_true", help="mark wrong: keep the tag on the file")
    p.add_argument("--out", default="eval/tag-labels.csv")
    p.set_defaults(run=run)
