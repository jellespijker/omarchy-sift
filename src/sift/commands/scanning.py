"""Scanning commands: `sift scan`, `index` (the timer job), `prune` and `watch`."""
from __future__ import annotations

import itertools
import json
import os
import sys
import time
from pathlib import Path

from .. import config as cfgmod
from .. import i18n
from .. import state
from ..adapters.extract import stat_ref
from ..adapters.http import BackendError
from ..adapters.stores import ReportStore, StoreError, XattrStore
from ..api import from_config, load_tree_for
from ..walk import walk_files

INDEX_BATCH = 25                         # files per batch between settings reloads


def _files(root: Path, exclude=()):
    """Files below root, excluding hidden entries, symlinks, mounts, cloud drives and vendored trees (see sift.walk)."""
    for p, _ in walk_files(root, tuple(exclude)):
        yield p


def _newest_first(files):
    def mtime(p):
        try:
            return os.stat(p, follow_symlinks=False).st_mtime_ns
        except OSError:
            return 0
    return sorted(files, key=mtime, reverse=True)


def _watch_dirs(cfg) -> list[Path]:
    return cfgmod.dirs(cfg)


def _store(cfg, apply: bool):
    sd = state.state_dir()
    return XattrStore(_watch_dirs(cfg), sd / "undo.jsonl") if apply else ReportStore(sd / "report.jsonl")


def _classify_all(sift, files, store, report_store, apply: bool, resume: bool, emit, limit: int | None = None,
                  deadline: float | None = None, known: dict | None = None) -> dict:
    """Classify files one by one. Tags are written before the report entry, so a failed write is reported as such (the file goes to
    review with the reason) instead of being recorded as done and silently skipped on the next run."""
    from dataclasses import replace
    from ..core.model import Outcome
    sd = state.state_dir()
    if known is None:
        known = state.latest_by_path(state.read(sd / "report.jsonl")) if resume else {}
    counts: dict[str, int] = {}
    failures = write_failures = 0
    done = 0
    for p in files:
        if (limit is not None and done >= limit) or (deadline is not None and time.monotonic() > deadline):
            counts["stopped"] = 1
            break
        try:
            ref = stat_ref(p)
            if state.is_current(known.get(str(p)), [ref.inode, ref.mtime_ns, ref.size]):
                counts["unchanged"] = counts.get("unchanged", 0) + 1
                continue
            d = sift.classify(p)
            status = "reported"
            if apply:
                try:
                    status = sift.apply(d, store)
                    write_failures = 0
                except (StoreError, OSError) as e:
                    write_failures += 1
                    status = "write_failed"
                    counts["write_failed"] = counts.get("write_failed", 0) + 1
                    print(f"{p}: could not write tags: {e}", file=sys.stderr)
                    d = replace(d, outcome=Outcome.REVIEW, tags=(), suggested=tuple(dict.fromkeys([*d.tags, *d.suggested])),
                                reason=("could not write tags: " + str(e))[:120])
            report_store.apply(d, d.tags)
            failures = 0
        except BackendError as e:
            failures += 1
            print(f"{p}: {e}", file=sys.stderr)
            if failures >= 5:
                print("aborting: backend failed 5 times in a row", file=sys.stderr)
                counts["aborted"] = 1
                return counts
            continue
        except (StoreError, OSError) as e:
            print(f"{p}: {e}", file=sys.stderr)
            continue
        if write_failures >= 5:
            print("aborting: tags could not be written 5 times in a row (does this filesystem support extended attributes? see `sift doctor`)", file=sys.stderr)
            counts["aborted"] = 1
            return counts
        counts[d.outcome.value] = counts.get(d.outcome.value, 0) + 1
        done += 1
        emit(p, d, status)
    return counts


def _emit(a):
    def emit(p, d, status):
        if getattr(a, "json", False):
            print(json.dumps({"path": str(p), "outcome": d.outcome.value, "tags": d.tags, "suggested": d.suggested,
                              "steps": [[s.node, s.label, round(s.probability, 3), s.basis] for s in d.steps], "status": status}), flush=True)
        else:
            path = "/".join(f"{s.label}({s.probability:.2f})" for s in d.steps) or "-"
            print(f"{d.outcome.value:<9} {p.name[:40]:<40} {path}  {d.reason}", flush=True)
    return emit


def cmd_scan(a, cfg) -> int:
    sift = from_config(cfg)
    apply = a.apply
    if apply and not cfg.get("write_xattrs"):
        print("refusing --apply: enable automatic tagging first (`sift config set write_xattrs true`)", file=sys.stderr)
        return 2
    root = Path(a.dir).expanduser().resolve()
    sd = state.state_dir()
    rs = ReportStore(Path(a.report) if a.report else sd / "report.jsonl")
    store = XattrStore([root], sd / "undo.jsonl") if apply else rs
    known = {} if a.rescan else state.latest_by_path(state.read(rs.report))          # resume from the report being written to
    counts = _classify_all(sift, _newest_first(_files(root, cfgmod.ignore(cfg))), store, rs, apply, not a.rescan, _emit(a), known=known)
    print(f"summary: {counts}", file=sys.stderr)
    return 3 if counts.get("aborted") else 0


def demo_on() -> bool:
    from .. import demo
    return demo.active()


def _start_ticks(pid: int) -> int | None:
    """Process start time from /proc, to tell a running scan from a recycled pid. None where /proc is not available."""
    try:
        return int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def running_scan(sd: Path) -> dict | None:
    """Progress of the index run that currently holds the lock, or None (a stale file from a crashed run is ignored)."""
    from .. import demo
    if demo.active() and os.environ.get("SIFT_DEMO_SCAN"):          # lets a demo (and screenshots) show the "scanning" state
        return {"pid": os.getpid(), "started": int(time.time()) - 90, "done": 112, "budget": 400, "trigger": "scheduled"}
    try:
        run = state.read_json(sd / "index_running.json", None)
        if not isinstance(run, dict):
            return None
        os.kill(int(run["pid"]), 0)
        if run.get("start_ticks") is not None and _start_ticks(int(run["pid"])) != run["start_ticks"]:
            return None                                     # the pid was reused by an unrelated process
        return run
    except (OSError, ValueError, KeyError, TypeError):
        return None


def cmd_index(a, cfg) -> int:
    """Background indexer: classify eligible files under `index_dirs`, newest first, within a budget. Safe to run from a timer."""
    import fcntl
    from ..indexer import candidates
    sd = state.state_dir()
    lock = open(sd / "index.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        run = running_scan(sd)
        print(json.dumps({"running": True, **(run or {})}))
        print(i18n.t("cli.scan.already", done=run["done"], budget=run["budget"]) if run else i18n.t("cli.scan.alreadyShort"), file=sys.stderr)
        return 4                                        # distinct from success: callers must not report this as a finished scan
    sift = None if a.dry_run else from_config(cfg)
    roots = cfgmod.dirs(cfg)
    tree = load_tree_for(cfg)
    rule_exts = frozenset(e for r in tree.rules for e in r.exts)
    known = state.latest_by_path(state.read(sd / "report.jsonl"))
    todo = candidates(roots, cfgmod.ignore(cfg), known, rule_exts)
    from ..core.profiles import select
    todo = [(m, p) for m, p in todo if not (lambda pr: pr and pr.skip)(select(tree.profiles, str(p)))]
    if a.dry_run:
        import collections
        print(f"{len(todo)} files to classify; by extension:", dict(collections.Counter(p.suffix.lower() for _, p in todo).most_common(12)))
        return 0
    budget = a.budget if a.budget is not None else int(cfg.get("index_budget", 400))
    seconds = a.max_seconds if a.max_seconds is not None else int(cfg.get("index_max_seconds", 1500))
    apply = bool(cfg.get("write_xattrs"))
    if apply:
        from ..adapters.stores import xattr_supported
        bad = [str(r) for r in roots if r.is_dir() and not xattr_supported(r)]
        if bad:
            print(f"tags cannot be stored in {', '.join(bad)} (no extended attribute support): results are reported but not written", file=sys.stderr)
            apply = False
    rs = ReportStore(sd / "report.jsonl")
    store = XattrStore(roots, sd / "undo.jsonl") if apply else rs
    progress = {"pid": os.getpid(), "start_ticks": _start_ticks(os.getpid()), "started": int(time.time()), "done": 0, "budget": budget,
                "trigger": "manual" if sys.stdin.isatty() or os.environ.get("SIFT_MANUAL") else "scheduled"}
    run_file = sd / "index_running.json"
    state.write_json(run_file, progress)
    deadline = time.monotonic() + seconds
    counts: dict[str, int] = {}
    paths = (p for _, p in todo)

    def tick(*_):
        progress["done"] += 1
        state.write_atomic(run_file, json.dumps(progress), durable=False)
    try:
        # Batches: between them the tree is reloaded, so a tag renamed, merged or deleted in the panel during a long run is respected.
        while True:
            batch = list(itertools.islice(paths, INDEX_BATCH))
            if not batch:
                break
            left = budget - progress["done"]
            if left <= 0:
                counts["stopped"] = 1
                break
            part = _classify_all(sift, batch, store, rs, apply, True, tick, limit=left, deadline=deadline, known=known)
            for k, v in part.items():
                counts[k] = counts.get(k, 0) + v
            if part.get("aborted") or part.get("stopped"):
                break
            try:
                sift.reload(cfgmod.load())
            except (cfgmod.ConfigError, OSError, ValueError):
                pass                                          # keep going with the settings this run started with
    finally:
        run_file.unlink(missing_ok=True)
    processed = sum(v for k, v in counts.items() if k in ("act", "review", "undecided"))
    status = {"last_run": int(time.time()), "classified": processed, "remaining": max(len(todo) - processed, 0), "counts": counts}
    state.write_json(sd / "index_status.json", status)
    state.maintain(sd)
    from .. import notify
    notify.after_index(cfg, sd, status, bool(counts.get("aborted")), first_pass_seen=False)
    print(json.dumps(status))
    return 3 if counts.get("aborted") else 0


def cmd_prune(a, cfg) -> int:
    """Re-check tags already recorded in the report against the deterministic gates (language detector, keyword evidence,
    code-file rule). Removes failing tags from files and the review queue; adds detector tags that apply. No model calls."""
    from ..adapters.extract import FileExtractor
    from ..adapters.stores import remove_tags
    from ..core.lang import matches_detector
    from ..core.model import Decision, FileRef, Outcome
    from ..core.runner import tag_gate
    from ..indexer import eligible
    sd = state.state_dir()
    tree = load_tree_for(cfg)
    rule_exts = frozenset(e for r in tree.rules for e in r.exts)
    roots = cfgmod.dirs(cfg)
    store = XattrStore(roots, sd / "undo.jsonl")
    ex = FileExtractor()
    stats = {"checked": 0, "tags_removed": 0, "tags_added": 0, "suggestions_dropped": 0, "files_changed": 0}
    removed_by: dict[str, int] = {}
    for p, e in state.latest_by_path(state.read(sd / "report.jsonl")).items():
        if not state.usable(e):
            continue
        path = Path(p)
        try:
            st = os.stat(path, follow_symlinks=False)
        except OSError:
            continue
        if [st.st_ino, st.st_mtime_ns, st.st_size] != e["ref"]:
            continue                                     # changed since classification: leave to the indexer
        in_repo = any((q / ".git").exists() for q in path.parents)
        ok_type = eligible(path.suffix.lower(), in_repo, rule_exts, path.name)
        text = ex.extract(path).text or "" if ok_type else ""
        stats["checked"] += 1

        def keep(t: str) -> bool:
            if not ok_type:
                return False
            d = tree.vocabulary.get(t)
            if d is None:
                return True
            if d.detector:
                return matches_detector(d.detector, text)
            return tag_gate(d, text)

        tags = [t for t in e.get("tags", []) if keep(t)]
        sugg = [t for t in e.get("suggested", []) if keep(t) and not (tree.vocabulary.get(t) and tree.vocabulary[t].detector)]
        add = [t for t, d in tree.vocabulary.items() if d.detector and ok_type and text and t not in tags and matches_detector(d.detector, text)]
        gone = [t for t in e.get("tags", []) if t not in tags]
        if not gone and not add and len(sugg) == len(e.get("suggested", [])):
            continue
        stats["tags_removed"] += len(gone)
        stats["tags_added"] += len(add)
        stats["suggestions_dropped"] += len(e.get("suggested", [])) - len(sugg)
        stats["files_changed"] += 1
        for t in gone:
            removed_by[t] = removed_by.get(t, 0) + 1
        if not a.apply:
            continue
        dec = Decision(FileRef(p, *e["ref"]), (), Outcome.ACT, tags=tuple(add))
        try:
            if gone:
                remove_tags(store, dec, gone)
            if add:
                store.apply(dec, add)
        except (StoreError, OSError) as ex_:
            print(f"{p}: {ex_}", file=sys.stderr)
            continue
        outcome = "act" if (tags or add) else ("review" if sugg else "undecided")
        state.append(sd / "report.jsonl", {**{k: v for k, v in e.items() if k != "ts"}, "tags": [*tags, *add], "suggested": sugg,
                                          "outcome": outcome, "reason": "" if outcome == "act" else e.get("reason", "")})
    print(json.dumps({**stats, "removed_by_tag": removed_by, "applied": a.apply}))
    return 0


def cmd_watch(a, cfg) -> int:
    sift = from_config(cfg)
    apply = bool(cfg.get("write_xattrs"))
    sd = state.state_dir()
    rs = ReportStore(sd / "report.jsonl")
    seen: dict[str, tuple[int, int]] = {}   # path -> (mtime_ns, size) seen on previous poll
    interval = float(cfg.get("watch_interval", 30))
    while True:
        known = state.latest_by_path(state.read(sd / "report.jsonl"))          # once per pass, not once per folder
        alive: dict[str, tuple[int, int]] = {}
        for root in _watch_dirs(cfg):
            stable = []
            for p in _files(root, cfgmod.ignore(cfg)):
                try:
                    st = os.stat(p, follow_symlinks=False)
                except OSError:
                    continue
                sig = (st.st_mtime_ns, st.st_size)
                if a.once or seen.get(str(p)) == sig:   # --once is an explicit scan; otherwise wait for two equal polls
                    stable.append(p)
                alive[str(p)] = sig
            _classify_all(sift, stable, XattrStore([root], sd / "undo.jsonl") if apply else rs, rs, apply, True, _emit(a), known=known)
        seen = alive                                     # forget files that are gone, so the memory cannot grow without bound
        if a.once:
            return 0
        time.sleep(interval)
        try:                                              # pick up settings and tag changes made while watching
            cfg = cfgmod.load()
            sift.reload(cfg)
        except (cfgmod.ConfigError, OSError, ValueError):
            pass
