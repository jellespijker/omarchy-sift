"""Measure how well the tags work: `sift eval-tags template` makes a labelling sheet pre-filled with Sift's own answers (correcting
beats labelling from scratch), `sift eval-tags run` scores any backend against your corrections.

Per tag it reports precision, recall and F1 at the current threshold and across a sweep, and can save thresholds that reach a
precision target. This is also how a chat model earns the right to tag automatically: evaluate it, then apply its thresholds."""
from __future__ import annotations

import csv
import os
import random
import sys
from pathlib import Path
from typing import Mapping, Sequence

from .. import config as cfgmod
from .. import state
from ..api import load_tree_for, make_classifier
from ..adapters.extract import FileExtractor
from ..adapters.http import BackendError
from ..core.model import Profile
from ..validate import ValidationError, normalize_tag, valid_unit_interval

SWEEP = (0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95)
MIN_POSITIVES = 5


def tag_metrics(scores: Mapping[str, Mapping[str, float]], truth: Mapping[str, set[str]], tags: Sequence[str],
                thresholds: Sequence[float]) -> dict[str, dict]:
    """Precision, recall and F1 for each tag at each threshold. Pure."""
    out: dict[str, dict] = {}
    for tag in tags:
        pos = {p for p, t in truth.items() if tag in t}
        rows = {}
        for th in thresholds:
            pred = {p for p, s in scores.items() if s.get(tag, 0.0) >= th}
            tp, fp, fn = len(pred & pos), len(pred - pos), len(pos - pred)
            prec = tp / (tp + fp) if tp + fp else None
            rec = tp / (tp + fn) if tp + fn else None
            f1 = 2 * prec * rec / (prec + rec) if prec and rec else 0.0
            rows[th] = {"tp": tp, "fp": fp, "fn": fn, "precision": prec, "recall": rec, "f1": f1}
        out[tag] = {"positives": len(pos), "by_threshold": rows}
    return out


def best_threshold(by_threshold: Mapping[float, dict], precision_target: float = 0.9) -> float | None:
    """Lowest threshold whose precision meets the target (so recall is highest); None when none does."""
    ok = [th for th, r in sorted(by_threshold.items()) if r["precision"] is not None and r["precision"] >= precision_target and r["tp"] > 0]
    return ok[0] if ok else None


def read_truth(path: Path) -> dict[str, set[str]]:
    truth: dict[str, set[str]] = {}
    with path.open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r.get("path"):
                truth[r["path"]] = {normalize_tag(t) for t in (r.get("truth") or "").replace(",", ";").split(";") if normalize_tag(t)}
    return truth


def make_template(cfg: dict, n: int, out: Path, seed: int = 1) -> int:
    sd = state.state_dir()
    tree = load_tree_for(cfg)
    vocab = set(tree.vocabulary)
    ex = FileExtractor()
    entries = [e for e in state.latest_by_path(state.read(sd / "report.jsonl")).values() if state.usable(e)]
    rnd = random.Random(seed)
    rnd.shuffle(entries)
    # a mix: files Sift tagged, files it only suggested for, and files with nothing (to catch missed tags)
    groups = {"tagged": [], "suggested": [], "none": []}
    for e in entries:
        key = "tagged" if set(map(normalize_tag, e.get("tags", []))) & vocab else "suggested" if set(e.get("suggested", [])) & vocab else "none"
        groups[key].append(e)
    picks: list[dict] = []
    for key, share in (("tagged", 0.4), ("suggested", 0.3), ("none", 0.3)):
        for e in groups[key]:
            if len([p for p in picks if p["_k"] == key]) >= int(n * share):
                break
            try:
                st = os.stat(e["path"], follow_symlinks=False)
            except OSError:
                continue
            if [st.st_ino, st.st_mtime_ns, st.st_size] != e["ref"]:
                continue
            ev = ex.extract(Path(e["path"]))
            if not ev.text or len(ev.text.strip()) < 200:
                continue
            picks.append({**e, "_k": key})
    out.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["path", "predicted", "truth", "note"])
        for e in picks:
            written = [t for t in map(normalize_tag, e.get("tags", [])) if t in vocab]
            suggested = [t for t in map(normalize_tag, e.get("suggested", [])) if t in vocab]
            w.writerow([e["path"], ";".join(written + [f"?{t}" for t in suggested]), ";".join(written), ""])
    return len(picks)


def run(a, cfg) -> int:
    try:
        if a.action == "template":
            n = make_template(cfg, a.n, Path(a.out))
            print(f"wrote {n} files to {a.out}. Open it and fix the `truth` column: list the tags that truly apply, separated by ';'. "
                  "Entries marked ? in `predicted` were only suggestions. Then run `sift eval-tags run {0}`.".format(a.out))
            return 0
        truth = read_truth(Path(a.labels))
        if not truth:
            print("error: no rows with a path in the labels file", file=sys.stderr)
            return 2
        table = cfgmod.backends(cfg)
        name = a.backend or "default"
        if name not in table:
            print(f"error: no backend named {name!r} (have: {', '.join(table) or 'none'})", file=sys.stderr)
            return 2
        from ..api import Sift
        tree = load_tree_for(cfg)
        clf = make_classifier(name, table[name], cfg)
        sift = Sift(tree, {"default": clf}, FileExtractor(), {"default": Profile(0.0, 0.0)})   # thresholds are applied below, from raw scores
        scores: dict[str, dict[str, float]] = {}
        for i, p in enumerate(truth, 1):
            try:
                scores[p] = dict(sift.classify(Path(p)).scores)
            except (BackendError, OSError) as e:
                print(f"skipped {Path(p).name}: {e}", file=sys.stderr)
            if i % 10 == 0:
                print(f"  {i}/{len(truth)}", file=sys.stderr)
        tags = sorted({t for ts in truth.values() for t in ts if t in tree.vocabulary} | {t for s in scores.values() for t in s})
        sweep = tuple(float(x) for x in a.sweep.split(",")) if a.sweep else SWEEP
        for th in sweep:
            valid_unit_interval(th, "sweep value")
        res = tag_metrics(scores, {p: t for p, t in truth.items() if p in scores}, tags, sweep)
        current = {t: (tree.vocabulary[t].act if tree.vocabulary[t].act is not None else cfg.get("threshold_act", 0.8)) for t in tags if t in tree.vocabulary}
        print(f"backend {name} ({table[name]['type']}), {len(scores)} files; precision/recall by threshold\n")
        print(f"{'tag':<22}{'files':>6}  " + "  ".join(f"{th:>9.2f}" for th in sweep) + "   suggested act")
        saved = {}
        for t in tags:
            r = res[t]
            if not r["positives"] and not any(x["tp"] + x["fp"] for x in r["by_threshold"].values()):
                continue
            cells = "  ".join(("   -/-    " if x["precision"] is None else f"{x['precision']:.0%}/{(x['recall'] or 0):.0%}".rjust(9)) for x in r["by_threshold"].values())
            bt = best_threshold(r["by_threshold"], a.precision) if r["positives"] >= MIN_POSITIVES else None
            if bt is not None:
                saved[t] = bt
            print(f"{t:<22}{r['positives']:>6}  {cells}   " + (f"{bt:.2f}" if bt is not None else ("need ≥%d examples" % MIN_POSITIVES if r["positives"] < MIN_POSITIVES else "none reaches %.0f%% precision" % (a.precision * 100))))
        if a.save and saved:
            vocab = dict(cfgmod.user_value("vocabulary", {}))
            for t, th in saved.items():
                vocab.setdefault(t, {"description": tree.vocabulary[t].description})["act"] = th
            cfgmod.save_user({"vocabulary": vocab})
            print(f"\nsaved act thresholds for {len(saved)} tags (precision target {a.precision:.0%})")
        elif saved:
            print(f"\nrun again with --save to store these thresholds (precision target {a.precision:.0%})")
        return 0
    except (cfgmod.ConfigError, ValidationError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


def register(sub) -> None:
    p = sub.add_parser("eval-tags", help="measure tag quality: template | run LABELS.csv [--backend NAME] [--save]")
    p.add_argument("action", choices=["template", "run"])
    p.add_argument("labels", nargs="?", help="run: the corrected labels CSV")
    p.add_argument("-n", type=int, default=100); p.add_argument("--out", default="eval/tag-labels.csv")
    p.add_argument("--backend", help="evaluate this backend instead of `default`")
    p.add_argument("--sweep", help="comma-separated thresholds"); p.add_argument("--precision", type=float, default=0.9)
    p.add_argument("--save", action="store_true", help="store per-tag thresholds that reach the precision target")
    p.set_defaults(run=lambda a, cfg: run(a, cfg) if a.action == "template" or a.labels else (print("usage: sift eval-tags run LABELS.csv", file=sys.stderr) or 2))
