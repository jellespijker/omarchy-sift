"""Tag research and evaluation: `sift discover`, `vocab`, `propose`, `survey`, `eval` and `sample`."""
from __future__ import annotations

import csv
import json
import os
import random
import sys
import tempfile
import time
from pathlib import Path

from .. import config as cfgmod
from .. import corpus, evalkit, state
from ..adapters.http import BackendError
from ..api import from_config, load_tree_for, make_proposer
from ..validate import valid_unit_interval
from ..walk import walk_files
from .scanning import _files

DEFAULT_EXCLUDE = {"node_modules", "__pycache__", "venv", ".venv"}

SURVEY_DOC_EXT = {".md", ".txt", ".rst", ".pdf", ".docx", ".odt", ".pptx", ".tex", ".xlsx", ".csv"}
FAMILY = {
    "document": {".pdf", ".docx", ".doc", ".odt", ".txt", ".md", ".rst", ".tex", ".rtf"},
    "spreadsheet": {".xlsx", ".xls", ".ods", ".csv"}, "presentation": {".pptx", ".ppt", ".odp", ".key"},
    "image": {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".heic", ".tiff", ".psd"},
    "media": {".mp4", ".mkv", ".mov", ".webm", ".mp3", ".wav", ".flac", ".ogg", ".m4a"},
    "3d-model": {".stl", ".3mf", ".obj", ".step", ".stp", ".ufp", ".gcode", ".blend", ".f3d"},
    "code": {".py", ".c", ".cpp", ".h", ".hpp", ".cc", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".sh", ".lua", ".qml", ".gd", ".cs", ".sql", ".cmake", ".ino"},
    "config": {".json", ".yaml", ".yml", ".toml", ".ini", ".conf", ".xml", ".cfg", ".lock"},
    "web": {".html", ".css", ".htm"}, "archive": {".zip", ".tar", ".gz", ".tgz", ".7z", ".rar", ".xz", ".bz2"},
    "installer": {".exe", ".msi", ".deb", ".rpm", ".appimage", ".iso", ".dmg"}, "log": {".log"},
    "electronics": {".schdoc", ".pcbdoc", ".kicad_sch", ".kicad_pcb", ".brd", ".sch", ".gbr"}}
EXT2FAM = {e: f for f, es in FAMILY.items() for e in es}
STOP = set("the and for with from this that into file files copy new old final draft version test tmp temp data untitled image img screenshot screen shot scan page doc docs document readme index main src lib out png jpg pdf txt csv json xml md zip".split())


def _add_vocab(tag: str, description: str, act: float | None = None) -> str:
    from .tags import add_vocabulary
    return add_vocabulary(tag, description, {"act": valid_unit_interval(act, "act")} if act is not None else None, enable=False)


def cmd_discover(a, cfg) -> int:
    """collect: ask the proposer for tags on unseen prose files. run: cluster proposals and test candidates with the classifier.
    list/accept/reject: review the verdicts. Nothing enters the vocabulary without `accept`."""
    from .. import discover as dsc
    from ..adapters.extract import FileExtractor
    from ..core.runner import chunks
    sd = state.state_dir()
    props_path, out_path, dec_path = sd / "survey_proposals.jsonl", sd / "discover.json", sd / "discover_decisions.jsonl"
    tree = load_tree_for(cfg)
    decisions = {e["tag"]: e["action"] for e in state.read(dec_path)}
    if a.action == "list":
        data = state.read_json(out_path, [])
        data = [c for c in data if isinstance(c, dict) and {"tag", "good", "support", "recall", "prevalence"} <= set(c)] if isinstance(data, list) else []
        merged = {k.replace("/", "-") for k in (cfg.get("tag_map") or {})}
        data = [c for c in data if c["good"] and c["tag"] not in decisions and c["tag"] not in merged]
        print(json.dumps(data) if a.json else "\n".join(f"{c['tag']:<28} files={c['support']:<4} recall={c['recall']:.0%} noise={c['prevalence']:.0%}" for c in data) or "(no tested candidates)")
        return 0
    if a.action in ("accept", "reject"):
        if not a.tag:
            print("usage: sift discover accept|reject TAG", file=sys.stderr)
            return 2
        if a.action == "accept":
            tag = _add_vocab(a.tag, a.desc or dsc.describe(a.tag), a.act)
            state.append(dec_path, {"tag": a.tag, "action": "accept"})
            print(f"added {tag} to the vocabulary; new files will be tagged with it, `sift index` picks it up on its next pass")
        else:
            state.append(dec_path, {"tag": a.tag, "action": "reject"})
            print(f"rejected {a.tag}")
        return 0
    proposals = state.read(props_path)
    if a.action == "collect":
        prop = make_proposer(cfg, keep_alive="10m")
        if prop is None:
            print('error: no proposer configured (set "proposer" in the config or run `sift setup`)', file=sys.stderr)
            return 2
        seen = {r["path"] for r in proposals}
        rep = [e for e in state.latest_by_path(state.read(sd / "report.jsonl")).values()
               if e["path"] not in seen and Path(e["path"]).suffix.lower() in SURVEY_DOC_EXT and e["ref"][2] > 400]
        rep.sort(key=lambda e: -e["ts"])
        ex, n = FileExtractor(), 0
        try:
            for e in rep:
                if n >= a.n:
                    break
                ev = ex.extract(Path(e["path"]))
                if not ev.text or len(ev.text.strip()) < 200:
                    continue
                try:
                    tags = prop.propose(ev.filename, chunks(ev.text, 1500, 2), list(tree.vocabulary), 6)
                except BackendError as ex_:
                    print(f"{e['path']}: {ex_}", file=sys.stderr)
                    continue
                state.append(props_path, {"path": e["path"], "family": "document", "tags": tags})
                n += 1
        finally:
            prop.unload()
        print(f"collected proposals for {n} files")
        return 0
    # run
    sift = from_config(cfg)
    rejected = [t for t, act in decisions.items() if act == "reject"]
    cands = dsc.cluster(proposals, existing=tree.vocabulary, rejected=rejected, min_support=a.min_support)[: a.top]
    ex = FileExtractor()
    texts: dict[str, str] = {}
    pool = {p for c in cands for p in c.files[:5]} | {r["path"] for r in proposals[::max(len(proposals) // 25, 1)]}
    for p in sorted(pool):
        ev = ex.extract(Path(p))
        if ev.text and len(ev.text.strip()) >= 200:
            texts[p] = f"filename: {ev.filename}\n\n{ev.text[:1500]}"
    verdicts = {v.tag: v for v in dsc.evaluate(cands, texts, sift.classifiers["default"])}
    out = [{"tag": c.tag, "support": c.support, "aliases": c.aliases[:4], "examples": [Path(p).name for p in c.files[:3]],
            "recall": verdicts[c.tag].recall, "prevalence": verdicts[c.tag].prevalence, "good": verdicts[c.tag].good,
            "description": dsc.describe(c.tag)} for c in cands]
    state.write_json(out_path, out)
    print(f"{len(cands)} candidates tested, {sum(c['good'] for c in out)} look useful (see `sift discover list`)")
    from .. import notify
    notify.after_discover(cfg, sd, sum(c["good"] for c in out))
    return 0


def cmd_vocab(a, cfg) -> int:
    tree = load_tree_for(cfg)
    if a.action == "list":
        for t, d in tree.vocabulary.items():
            print(f"{t:<16} {d.description}")
        return 0
    if not a.tag or not a.description:
        print("usage: sift vocab add TAG DESCRIPTION [--act 0.7]", file=sys.stderr)
        return 2
    tag = _add_vocab(a.tag, a.description, a.act)
    print(f"added {tag}; new files and `sift scan --rescan` will use it")
    return 0


def cmd_propose(a, cfg) -> int:
    from ..adapters.extract import FileExtractor
    from ..core.runner import chunks
    prop = make_proposer(cfg)
    if prop is None:
        print('error: no proposer configured (set "proposer" in the config or run `sift setup`)', file=sys.stderr)
        return 2
    ev = FileExtractor().extract(Path(a.path).expanduser().resolve())
    if not ev.text:
        print("no extractable text (or file flagged as possible secret): nothing is sent", file=sys.stderr)
        return 1
    tree = load_tree_for(cfg)
    parts = chunks(ev.text, 1500, 3)
    tags = prop.propose(ev.filename, parts, list(tree.vocabulary), a.n)
    print(json.dumps(tags) if a.json else "\n".join(tags) or "(no proposals)")
    return 0


def _tokens(*parts: str) -> list[str]:
    import re
    out = []
    for part in parts:
        for w in re.findall(r"[a-zA-Z]{3,}", part.replace("_", " ").replace("-", " ").replace(".", " ")):
            for piece in re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])", w):   # split camelCase
                piece = piece.lower()
                if len(piece) >= 3 and piece not in STOP:
                    out.append(piece)
    return out


def _survey_walk(root: Path, depth: int, exclude: tuple[str, ...]):
    """Yield (stratum, path) for the files below root, using the same walker as every other command (hidden, vendored, cloud and
    mount-point folders are skipped there)."""
    for p, _ in walk_files(root, exclude):
        rel = p.parent.relative_to(root).parts
        yield f"{root.name}/{'/'.join(rel[:depth]) or '.'}", p


def _spread(strata: dict, n: int, rnd: random.Random, max_per_folder: int = 0) -> list[tuple[str, Path]]:
    """Round-robin across (folder, file family) cells so no folder or type dominates the sample.
    `max_per_folder` (0 = unlimited) caps how many files one folder contributes."""
    cells: dict[tuple[str, str], list[Path]] = {}
    for st, files in strata.items():
        files = rnd.sample(files, max_per_folder) if max_per_folder and len(files) > max_per_folder else files
        for f in files:
            cells.setdefault((st, EXT2FAM.get(f.suffix.lower(), "other")), []).append(f)
    for v in cells.values():
        rnd.shuffle(v)
    keys = sorted(cells)
    rnd.shuffle(keys)
    out: list[tuple[str, Path]] = []
    while len(out) < n and keys:
        for k in list(keys):
            if not cells[k]:
                keys.remove(k)
                continue
            out.append((k[0], cells[k].pop()))
            if len(out) >= n:
                break
    return out


def cmd_survey(a, cfg) -> int:
    """Stage A: sample files across folders and types and derive tag signals from names and paths only (no model, nothing
    leaves the machine). Stage B (--propose M): ask the configured proposer for semantic tags on M text files."""
    import collections
    rnd = random.Random(a.seed)
    sd = state.state_dir()
    sample_path = sd / "survey_sample.jsonl"
    never = tuple(cfg.get("survey_exclude", [])) + cfgmod.ignore(cfg)
    strata: dict[str, list[Path]] = collections.defaultdict(list)
    total = 0
    seen: set[Path] = set()                      # roots can nest (~ and ~/dev): count each file once
    for d in a.dirs:
        for stratum, p in _survey_walk(Path(d).expanduser().resolve(), a.depth, never):
            if p in seen:
                continue
            seen.add(p)
            strata[stratum].append(p)
            total += 1
    picks = _spread(strata, a.n, rnd, a.max_per_folder)
    rows = []
    for st, p in picks:
        try:
            stt = os.stat(p, follow_symlinks=False)
        except OSError:
            continue
        ext = p.suffix.lower()
        rows.append({"path": str(p), "stratum": st, "ext": ext, "family": EXT2FAM.get(ext, "other"), "size": stt.st_size,
                     "year": time.gmtime(stt.st_mtime).tm_year, "tokens": _tokens(p.stem, *p.parent.parts[-2:])})
    with open(os.open(sample_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    fam = collections.Counter(r["family"] for r in rows)
    print(f"walked {total} files in {len(strata)} folders; sampled {len(rows)} -> {sample_path}", file=sys.stderr)
    print("by type:", dict(fam.most_common()))
    print("folders in sample:", len({r['stratum'] for r in rows}), "| years:", dict(sorted(collections.Counter(r['year'] for r in rows).items())))
    if not a.propose:
        return 0
    return _survey_propose(a, cfg, rows, sd)


def _survey_propose(a, cfg, rows, sd) -> int:
    from ..adapters.extract import FileExtractor
    from ..core.runner import chunks
    prop = make_proposer(cfg, keep_alive="10m")
    if prop is None:
        print('error: no proposer configured (set "proposer" in the config or run `sift setup`)', file=sys.stderr)
        return 2
    tree = load_tree_for(cfg)
    out = sd / "survey_proposals.jsonl"
    done = {e["path"] for e in state.read(out)}
    prose = ("document", "presentation", "spreadsheet", "web")
    docs = sorted((r for r in rows if r["family"] in prose + ("code", "config") and r["path"] not in done),
                  key=lambda r: r["family"] not in prose)          # prose first: it says more about topics than code does
    ex = FileExtractor()
    sent = 0
    try:
        for r in docs:
            if sent >= a.propose:
                break
            ev = ex.extract(Path(r["path"]))
            if not ev.text or len(ev.text.strip()) < 200:
                continue                       # secrets (text dropped), binaries and tiny files are never sent
            try:
                tags = prop.propose(ev.filename, chunks(ev.text, 1500, 2), list(tree.vocabulary), 6)
            except BackendError as e:
                print(f"{r['path']}: {e}", file=sys.stderr)
                continue
            sent += 1
            state.append(out, {"path": r["path"], "stratum": r["stratum"], "family": r["family"], "tags": tags})
            print(f"[{sent}/{a.propose}] {r['family']:<12} {r['stratum']}: {tags}", flush=True)
    finally:
        prop.unload()
    return 0


def cmd_eval(a, cfg) -> int:
    sift = from_config(cfg)
    memo = evalkit.Memo(sift.classifiers["default"])
    sift.classifiers = {"default": memo}
    csv_path = corpus.build(Path(tempfile.mkdtemp(prefix="sift-corpus-")), seed=a.seed) if a.builtin else Path(a.labels)
    rows = evalkit.read_labels(csv_path)
    res = evalkit.evaluate(sift, rows)
    sw = evalkit.sweep(sift, rows, [float(x) for x in a.sweep.split(",")]) if a.sweep else None
    print(evalkit.format_report(res, evalkit.flip_rate(rows, res), sw))
    if a.out:
        Path(a.out).write_text(json.dumps({k: v for k, v in res.items() if k not in ("preds", "acts")}, indent=2))
    return 0


def cmd_sample(a, cfg) -> int:
    files = list(_files(Path(a.dir).expanduser()))
    random.Random(a.seed).shuffle(files)
    out = Path(a.out)
    with open(os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["path", "label", "group"])
        for p in files[: a.n]:
            w.writerow([str(p), "", ""])
    print(f"wrote {min(a.n, len(files))} rows to {out}; fill in `label` (e.g. document/invoice, code, log, data, other)")
    return 0
