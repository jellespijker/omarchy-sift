"""Review commands: `sift status`, `review`, `accept`, `reject`, `apply` and `untag`."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from .. import config as cfgmod
from .. import state
from ..adapters.http import BackendError
from ..adapters.stores import StoreError, XattrStore, undo
from ..api import load_tree_for, make_classifier
from .scanning import _watch_dirs, demo_on, running_scan

def cmd_status(a, cfg) -> int:
    sd = state.state_dir()
    rep, rev = state.read(sd / "report.jsonl"), state.read(sd / "reviews.jsonl")
    latest = state.latest_by_path(rep)
    counts: dict[str, int] = {}
    for e in latest.values():
        k = str(e.get("outcome", "unknown"))
        counts[k] = counts.get(k, 0) + 1
    health: dict = {"state": "unconfigured"}
    try:
        table = cfgmod.backends(cfg)
        if "default" in table:
            h = make_classifier("default", table["default"], cfg).health()
            health = {"state": "ok" if h.get("status") == "ready" else "degraded", "model": h.get("model", ""), "note": h.get("note", "")}
    except (cfgmod.ConfigError, BackendError) as e:
        health = {"state": "unreachable", "error": "".join(c for c in str(e) if c.isprintable())[:120]}
    idx = state.read_json(sd / "index_status.json", {})
    idx = idx if isinstance(idx, dict) else {}
    shown, hidden = _review_groups(cfg, False, rep, rev)
    decided = {e["tag"] for e in state.read(sd / "discover_decisions.jsonl") if "tag" in e}
    found = state.read_json(sd / "discover.json", [])
    ideas = sum(1 for c in found if isinstance(c, dict) and c.get("good") and c.get("tag") not in decided) if isinstance(found, list) else 0
    from ..api import describe_backends
    from ..commands.info import usage_summary
    try:
        backends = describe_backends(cfg, health.get("model", ""))
    except cfgmod.ConfigError:
        backends = []
    u = usage_summary(cfg)
    out = {"demo": demo_on(), "scan": running_scan(sd), "backends": backends, "usage": {"today": u["periods"]["today"]["tokens_in"] + u["periods"]["today"]["tokens_out"],
                                           "week": u["periods"]["week"]["tokens_in"] + u["periods"]["week"]["tokens_out"],
                                           "estimated": bool(u["periods"]["week"]["estimated"]), "remaining": u.get("remaining_tokens"),
                                           "remaining_cost": u.get("remaining_cost"), "cost_week": u["periods"]["week"]["cost"] if u["periods"]["week"]["priced"] else None},
           "backend": health, "index": idx, "counts": counts, "files": len(latest), "pending": len(shown), "hidden": hidden, "ideas": ideas,
           "write_xattrs": bool(cfg.get("write_xattrs")), "dirs": [str(d) for d in _watch_dirs(cfg)]}
    print(json.dumps(out) if a.json else "\n".join(f"{k}: {v}" for k, v in out.items()))
    return 0


def _is_relevant(e: dict, vocab: set[str]) -> bool:
    """Worth the user's attention: a possible secret, or a suggestion for one of their own descriptive tags.
    Generic guesses (`log`, `code`, `other`, ...) are noise and stay hidden unless asked for."""
    return e.get("reason", "").startswith("possible secret") or any(t in vocab for t in e.get("suggested", []))


def _effective(entry: dict, cfg) -> list[str]:
    """The entry's suggested tags as they stand now: merged or renamed tags show as their destination, deleted ones and tags the
    file already carries disappear."""
    from ..adapters.stores import read_tags
    tmap = {k.replace("/", "-"): v.replace("/", "-") for k, v in (cfg.get("tag_map") or {}).items()}
    try:
        have = set(read_tags(entry["path"]))
    except OSError:
        have = set()
    out: list[str] = []
    for t in entry.get("suggested", []):
        f = t.replace("/", "-")
        if f in tmap:
            t = tmap[f]                                    # merged or renamed: show the destination
        if t and t.replace("/", "-") not in have and t not in out:
            out.append(t)
    return out


def _review_groups(cfg, show_all: bool, report: list[dict] | None = None, reviews: list[dict] | None = None):
    """(groups to show, number hidden). Relevance is decided before grouping, because grouping reads the start of every file."""
    sd = state.state_dir()
    vocab = set(load_tree_for(cfg).vocabulary) | set((cfg.get("tag_map") or {}).values())
    pending = state.pending_review(report if report is not None else state.read(sd / "report.jsonl"),
                                   reviews if reviews is not None else state.read(sd / "reviews.jsonl"))
    pending = [e for e in pending if not (e.get("suggested") and not _effective(e, cfg))]     # everything it suggested was merged away or is done
    keep = pending if show_all else [e for e in pending if _is_relevant(e, vocab)]
    return state.group_identical(keep), len(pending) - len(keep)


def cmd_review(a, cfg) -> int:
    sd = state.state_dir()
    groups, _hidden = _review_groups(cfg, a.all)
    groups = groups[: a.limit]
    items = [g[0] for g in groups]
    counts = {g[0]["path"]: len(g[1]) for g in groups}
    out = [{"path": e["path"], "name": Path(e["path"]).name, "count": counts[e["path"]], "suggested": _effective(e, cfg), "reason": e.get("reason", ""),
            "confidence": e["steps"][-1][2] if e.get("steps") else 0.0, "steps": e.get("steps", []),
            "chips": [{"tag": t, "score": e.get("scores", {}).get(t, 0.0)} for t in _effective(e, cfg)]} for e in items]
    if a.json:
        print(json.dumps(out))
    else:
        for o in out:
            print(f"{o['confidence']:.2f} {o['path']}  {','.join(o['suggested']) or '-'}  {o['reason']}".rstrip())
    return 0


def _find(path: str, sd: Path) -> dict:
    e = state.latest_by_path(state.read(sd / "report.jsonl")).get(str(Path(path).expanduser().resolve()))
    if not e:
        raise StoreError("path not in report; run `sift scan` first")
    return e


def _members(e: dict, sd: Path) -> list[dict]:
    """The entry plus every other pending entry whose small file is byte-identical (so one click handles a whole group)."""
    pending = state.pending_review(state.read(sd / "report.jsonl"), state.read(sd / "reviews.jsonl"))
    for rep, members in state.group_identical(pending):
        if any(m["path"] == e["path"] for m in members):
            return members if any(m is rep or m["path"] == e["path"] for m in members) else [e]
    return [e]


def _learn(sd: Path, entry: dict, pairs: list[tuple[str, str]]) -> None:
    """Record what the user decided about a suggested tag, so audit, eval-tags and tune can learn from it."""
    for tag, verdict in pairs:
        state.append(sd / "verdicts.jsonl", {"path": entry["path"], "tag": tag, "verdict": verdict, "ref": entry.get("ref"), "source": "review"})


def cmd_accept(a, cfg) -> int:
    from ..core.model import Decision, FileRef, Outcome
    sd = state.state_dir()
    e = _find(a.path, sd)
    tags = [t for t in (a.tags.split(",") if a.tags else _effective(e, cfg)) if t]
    tmap = cfg.get("tag_map") or {}
    tags = [tmap.get(t, t) for t in tags]                   # a merged-away name is never written again
    from ..core.runner import sanitize_tag
    tags = [x for x in map(sanitize_tag, tags) if x]
    if not tags:
        print("nothing to apply (possible secrets and undecided files carry no suggested tags; pass --tags)", file=sys.stderr)
        return 1
    if e.get("reason", "").startswith("possible secret"):
        print("refusing: file flagged as possible secret; sensitive files are never tagged", file=sys.stderr)
        return 1
    store = XattrStore(_watch_dirs(cfg), sd / "undo.jsonl")
    vocab = set(load_tree_for(cfg).vocabulary)
    applied = 0
    for m in _members(e, sd):
        d = Decision(FileRef(m["path"], *m["ref"]), (), Outcome.ACT, tags=tuple(tags))
        try:
            store.apply(d, tags)
        except (StoreError, OSError) as ex:
            if m is e:
                raise
            print(f"{m['path']}: {ex}", file=sys.stderr)
            continue
        state.append(sd / "reviews.jsonl", {"path": m["path"], "action": "accept", "tags": tags})
        _learn(sd, m, [(t, "ok") for t in tags if t in vocab])
        applied += 1
    print(f"tagged {applied} file(s):", ",".join(tags))
    return 0


def cmd_apply(a, cfg) -> int:
    """Write tags for confident (ACT) results already in the report. Re-verifies each file's identity first."""
    from ..core.model import Decision, FileRef, Outcome
    sd = state.state_dir()
    store = XattrStore(_watch_dirs(cfg), sd / "undo.jsonl")
    counts: dict[str, int] = {}
    for p, e in state.latest_by_path(state.read(sd / "report.jsonl")).items():
        if e.get("outcome") != "act" or not e.get("tags"):
            continue
        try:
            status = store.apply(Decision(FileRef(p, *e["ref"]), (), Outcome.ACT, tags=tuple(e["tags"])), e["tags"])
        except (StoreError, OSError) as ex:
            status = "skipped: " + str(ex)[:40]
        counts[status] = counts.get(status, 0) + 1
    print(json.dumps(counts) if a.json else counts)
    return 0


def cmd_reject(a, cfg) -> int:
    sd = state.state_dir()
    e = _find(a.path, sd)
    ms = _members(e, sd)
    vocab = set(load_tree_for(cfg).vocabulary)
    for m in ms:
        state.append(sd / "reviews.jsonl", {"path": m["path"], "action": "reject", "suggested": m.get("suggested", [])})
        _learn(sd, m, [(t, "rejected") for t in m.get("suggested", []) if t in vocab])
    print(f"rejected {len(ms)} file(s)")
    return 0


def cmd_untag(a, cfg) -> int:
    """Undo tag changes. Always say how much: `--last N` (the newest N changes), `--path FILE` (that file's newest change, or all of its
    changes with `--last`), or `--all` (everything in the undo log). With none of them nothing is reverted."""
    path, everything = getattr(a, "path", None), getattr(a, "all", False)
    if not (a.last or path or everything):
        print("say what to undo: --last N, --path FILE or --all (this reverts every recorded change)", file=sys.stderr)
        return 2
    done, skipped = undo(state.state_dir() / "undo.jsonl", _watch_dirs(cfg), a.last or (1 if path else None), path)
    print(f"restored {done}, skipped {skipped}")
    return 0
