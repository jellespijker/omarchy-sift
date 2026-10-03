"""Evaluation: per-node accuracy, calibration, end-to-end precision, injection flips."""
from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path
from typing import Iterable

from .api import Sift
from .core.model import Outcome

BINS = [(0.0, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 0.9), (0.9, 1.01)]


def read_labels(csv_path: Path) -> list[tuple[str, str, str]]:
    """CSV columns: path,label[,group]. `label` is a path like 'document/invoice'. Blank labels skipped."""
    rows = []
    with csv_path.open(newline="") as f:
        for r in csv.DictReader(f):
            if (r.get("label") or "").strip():
                rows.append((r["path"], r["label"].strip(), (r.get("group") or "").strip()))
    return rows


class Memo:
    """Caches classifier answers so threshold sweeps replay without new requests."""
    def __init__(self, inner):
        self.inner, self.capabilities, self.cache = inner, inner.capabilities, {}

    def ask(self, text, questions):
        key = text + "\x00" + repr(questions)
        if key not in self.cache:
            self.cache[key] = self.inner.ask(text, questions)
        return self.cache[key]


def evaluate(sift: Sift, rows: Iterable[tuple[str, str, str]]) -> dict:
    node_hits: dict[str, list[bool]] = defaultdict(list)
    cal: dict[tuple[float, float], list[bool]] = defaultdict(list)
    act_total = act_right = n = 0
    outcomes: dict[str, int] = defaultdict(int)
    preds: dict[str, str] = {}
    acts: dict[str, bool] = {}
    errors = 0
    for path, truth, _ in rows:
        try:
            d = sift.classify(Path(path))
        except Exception:
            errors += 1
            continue
        n += 1
        outcomes[d.outcome.value] += 1
        tpath = truth.split("/")
        on_path = True
        for depth, s in enumerate(d.steps):
            if depth >= len(tpath) or not on_path:
                break
            ok = s.label == tpath[depth]
            node_hits[s.node].append(ok)
            for lo, hi in BINS:
                if lo <= s.probability < hi:
                    cal[(lo, hi)].append(ok)
            on_path = ok
        got = "/".join(s.label for s in d.steps)
        preds[path] = got
        acts[path] = d.outcome is Outcome.ACT
        if d.outcome is Outcome.ACT:
            act_total += 1
            act_right += int(got == truth)
    return {"n": n, "errors": errors, "outcomes": dict(outcomes),
            "node_accuracy": {k: (sum(v), len(v)) for k, v in node_hits.items()},
            "calibration": {f"{lo:.1f}-{min(hi, 1.0):.1f}": (sum(v), len(v)) for (lo, hi), v in sorted(cal.items())},
            "act_precision": (act_right, act_total), "preds": preds, "acts": acts}


def flip_rate(rows: list[tuple[str, str, str]], res: dict) -> tuple[int, int, int]:
    """Rows with group 'inj:<id>' are injected twins of 'clean:<id>'.
    Returns (label flips, flips where either side would ACT, pairs)."""
    preds, acts = res["preds"], res["acts"]
    clean = {g.split(":", 1)[1]: p for p, _, g in rows if g.startswith("clean:")}
    flips = act_flips = total = 0
    for p, _, g in rows:
        gid = g.split(":", 1)[1] if g.startswith("inj:") else None
        if gid in clean and p in preds and clean[gid] in preds:
            total += 1
            changed = preds[p] != preds[clean[gid]]
            flips += int(changed)
            act_flips += int(changed and (acts[p] or acts[clean[gid]]))
    return flips, act_flips, total


def sweep(sift: Sift, rows: list[tuple[str, str, str]], acts: list[float]) -> list[tuple[float, int, int, int]]:
    """For each ACT threshold: (threshold, files ACTed, ACT correct, total). Uses cached answers."""
    from .core.model import Profile
    out = []
    for a in acts:
        sift.profiles = {"default": Profile(a, min(0.4, a))}
        r = evaluate(sift, rows)
        out.append((a, r["act_precision"][1], r["act_precision"][0], r["n"]))
    return out


def format_report(res: dict, flips: tuple[int, int, int] | None = None, sw: list | None = None) -> str:
    out = [f"files evaluated: {res['n']} (errors: {res['errors']})", f"outcomes: {res['outcomes']}"]
    a, t = res["act_precision"]
    out.append(f"ACT precision: {a}/{t}" + (f" = {a/t:.0%}" if t else ""))
    out.append("per-node accuracy (conditional on correct parents):")
    for k, (h, tot) in res["node_accuracy"].items():
        out.append(f"  {k:<10} {h}/{tot} = {h/tot:.0%}")
    out.append("calibration (step probability bin -> accuracy):")
    for k, (h, tot) in res["calibration"].items():
        out.append(f"  {k:<9} {h}/{tot} = {h/tot:.0%}")
    if flips and flips[2]:
        out.append(f"injection pairs: {flips[2]}; label flips: {flips[0]}; flips involving an ACT: {flips[1]}")
    if sw:
        out.append("threshold sweep (act threshold -> files acted, correct, precision, coverage):")
        for a, n_act, ok, n in sw:
            out.append(f"  {a:.2f}  acted {n_act:>3}/{n}  correct {ok:>3}  precision {ok/n_act:.0%}  coverage {n_act/n:.0%}" if n_act else f"  {a:.2f}  acted 0/{n}")
    return "\n".join(out)
