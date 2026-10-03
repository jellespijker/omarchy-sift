"""Tag manager: list, add, edit, rename, merge, delete and find similar tags.

Tags come from three places: the user's vocabulary (descriptive tags scored by the classifier), built-in labels (the decision tree and
extension rules) and anything already written to files. Renames and merges are stored as `tag_map` entries in the user config so
future classification emits the new name, and existing files are rewritten through the undo log.
"""
from __future__ import annotations

import difflib
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

from .. import config as cfgmod
from .. import discover as dsc
from .. import state
from ..adapters.stores import StoreError, XattrStore, read_tags, replace_tags, write_tags
from ..walk import walk_files
from ..api import load_tree_for
from ..core.model import Decision, FileRef, Outcome
from ..i18n import t
from ..validate import ValidationError, clean_text, valid_count, valid_pattern, valid_tag, valid_unit_interval


def flat(tag: str) -> str:
    return tag.replace("/", "-")


def _reports(sd: Path) -> dict[str, dict]:
    return {p: e for p, e in state.latest_by_path(state.read(sd / "report.jsonl")).items() if state.usable(e)}


def _builtin_names(tree) -> dict[str, str]:
    """name -> kind for labels emitted by the tree and the extension rules."""
    out = {r.tag: "rule" for r in tree.rules}
    for node in tree.nodes.values():
        if node.kind == "choice":
            for lab in node.labels:
                if lab != "other":
                    out.setdefault(flat(lab), "builtin")
    return out


def scan_disk(cfg: dict, sd: Path) -> dict:
    """Read the tags that are actually on files in the scanned folders (whoever wrote them: Sift, Dolphin, other tools).
    Read-only. The result is cached in the state folder so the tag manager can show and change tags Sift did not write."""
    out_path = sd / "disk_tags.jsonl"
    scanned = tagged = 0
    lines: list[str] = []
    for root in cfgmod.dirs(cfg):
        for p, _ in walk_files(root, cfgmod.ignore(cfg)):
            scanned += 1
            tags = read_tags(p)
            if tags:
                tagged += 1
                lines.append(json.dumps({"path": str(p), "tags": tags}) + "\n")
    state.write_atomic(out_path, "".join(lines))
    meta = {"ts": int(time.time()), "scanned": scanned, "tagged": tagged}
    state.write_json(sd / "disk_tags_meta.json", meta)
    return meta


def disk_entries(sd: Path) -> list[dict]:
    return state.read(sd / "disk_tags.jsonl")


def inventory(cfg: dict, sd: Path) -> list[dict]:
    tree = load_tree_for(cfg)
    reports = _reports(sd)
    sift_wrote = {flat(t) for e in reports.values() for t in e.get("tags", [])}
    disk = disk_entries(sd)
    # Prefer what is really on the files when a scan exists; otherwise fall back to what Sift recorded.
    counts = Counter(t for e in disk for t in set(e["tags"])) if disk else Counter(flat(t) for e in reports.values() for t in e.get("tags", []))
    builtin = _builtin_names(tree)
    tmap = dict(cfg.get("tag_map") or {})
    rows = []
    merged_away = {flat(k) for k in tmap}                  # merged, renamed or deleted tags: shown as aliases of their destination, not as tags
    for tag in sorted((set(counts) | set(tree.vocabulary) | set(builtin)) - merged_away):
        d = tree.vocabulary.get(tag)
        kind = "vocabulary" if d else builtin.get(tag) or ("sift" if tag in sift_wrote else "yours")
        rows.append({"tag": tag, "files": counts.get(tag, 0), "kind": kind,
                     "description": d.description if d else "", "act": d.act if d else None,
                     "aliases": sorted(s for s, t in tmap.items() if t == tag), "has_detector": bool(d and d.detector)})
    return sorted(rows, key=lambda r: (-r["files"], r["tag"]))


def similar_groups(names: dict[str, int], threshold: float = 0.86, ignored: list[list[str]] | None = None) -> list[dict]:
    """Groups of tags that probably mean the same thing: same words in any order or plural, or near-identical spelling.
    Pairs the user chose to keep separate (`ignored`: sets of tags) are never joined, so a dismissed proposal stays dismissed even when
    other tags later join the group."""
    apart = [set(g) for g in ignored or []]
    keys = {n: " ".join(sorted(dsc.canon(n))) for n in names}
    parent = {n: n for n in names}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    items = sorted(names)
    blocks: dict[str, list[str]] = {}                  # only tags that start alike can be near-duplicates: avoids comparing every pair
    for n in items:
        blocks.setdefault(keys[n][:2], []).append(n)
        blocks.setdefault("w:" + (keys[n].split()[0] if keys[n] else ""), []).append(n)
    pairs = {(a, b) for blk in blocks.values() for i, a in enumerate(blk) for b in blk[i + 1:]}
    for a, b in sorted(pairs):
        if abs(len(keys[a]) - len(keys[b])) <= max(3, len(keys[a]) // 4):
            if keys[a] == keys[b] or difflib.SequenceMatcher(None, keys[a], keys[b]).ratio() >= threshold:
                parent[find(a)] = find(b)
    groups: dict[str, list[str]] = {}
    for n in items:
        groups.setdefault(find(n), []).append(n)
    def conflicts(x: str, y: str) -> bool:
        return any(x in g and y in g for g in apart)

    def similar(x: str, y: str) -> bool:
        return keys[x] == keys[y] or difflib.SequenceMatcher(None, keys[x], keys[y]).ratio() >= threshold
    clusters: list[list[str]] = []
    for g in groups.values():
        if not any(conflicts(x, y) for i, x in enumerate(g) for y in g[i + 1:]):
            clusters.append(g)
            continue
        split: list[list[str]] = []                  # a dismissed pair is inside: regroup around the most used tag instead
        for tg in sorted(g, key=lambda t: (-names[t], len(t), t)):
            home = next((c for c in split if similar(tg, c[0]) and not any(conflicts(tg, m) for m in c)), None)
            (home.append(tg) if home else split.append([tg]))
        clusters += split
    out = []
    for g in clusters:
        if len(g) > 1:
            keep = sorted(g, key=lambda t: (-names[t], len(t), t))[0]
            out.append({"keep": keep, "merge": [t for t in g if t != keep], "files": sum(names[t] for t in g)})
    return sorted(out, key=lambda x: -x["files"])


# ---- applying a change -------------------------------------------------------------------------------------------------

def _map_with(cfg: dict, src: str, dst: str) -> dict[str, str]:
    """tag_map with src -> dst added; chains collapse so lookups need one step, and cycles are refused."""
    m = dict(cfg.get("tag_map") or {})
    if dst and (dst == src or m.get(dst) == src):
        raise cfgmod.ConfigError(f"merging {src!r} into {dst!r} would create a cycle")
    final = m.get(dst, dst) if dst else ""
    for k, v in list(m.items()):
        if v == src:
            m[k] = final
    m[src] = final
    return m


def _candidate_paths(sd: Path) -> list[str]:
    seen: dict[str, None] = {}
    for e in disk_entries(sd):
        seen[e["path"]] = None
    for p in _reports(sd):
        seen[p] = None
    return list(seen)


def rewrite_files(cfg: dict, sd: Path, mapping: dict[str, str], apply: bool, journal: bool = False) -> dict[str, int]:
    """Rename or drop tags on every known file that carries them (Sift's own and, after `sift tags scan`, ones written by other tools).
    Uses each file's current state, so files edited since they were classified are still handled. With apply=False nothing changes."""
    store = XattrStore(cfgmod.dirs(cfg), sd / "undo.jsonl")
    flat_map = {flat(k): flat(v) for k, v in mapping.items()}
    reports = _reports(sd)
    stats = {"files": 0, "changed": 0, "skipped": 0}
    for p in _candidate_paths(sd):
        try:
            current = read_tags(p)
            st = os.stat(p, follow_symlinks=False)
        except OSError:
            continue
        if not any(t in flat_map for t in current):
            continue
        stats["files"] += 1
        if not apply:
            continue
        dec = Decision(FileRef(p, st.st_ino, st.st_mtime_ns, st.st_size), (), Outcome.ACT)
        try:
            replace_tags(store, dec, flat_map)
        except (StoreError, OSError):
            stats["skipped"] += 1
            continue
        if journal:                       # what each file had before, so a merge can be undone one source tag at a time
            for src, dst in flat_map.items():
                if src in current and dst:
                    state.append(sd / "merges.jsonl", {"path": p, "inode": st.st_ino, "src": src, "dst": dst, "had_dst": dst in current})
        e = reports.get(p)
        if e:
            new_tags = sorted({flat_map.get(flat(t), flat(t)) for t in e.get("tags", [])} - {""})
            state.append(sd / "report.jsonl", {**{k: v for k, v in e.items() if k != "ts"}, "tags": new_tags})
        stats["changed"] += 1
    return stats


def _user_vocab(cfg: dict, tree, tag: str) -> dict:
    """The user-config vocabulary entry for tag, copied from the packaged default when the user has none yet."""
    vocab = dict(cfgmod.user_value("vocabulary", {}))
    if tag not in vocab and tag in tree.vocabulary:
        d = tree.vocabulary[tag]
        vocab[tag] = {k: v for k, v in {"description": d.description, "act": d.act, "review": d.review, "detector": d.detector,
                                        "require": d.require, "require_min": d.require_min}.items() if v is not None}
    return vocab


def _checked_options(a) -> dict:
    """Validated optional settings from the command line."""
    out: dict = {}
    if a.act is not None:
        out["act"] = valid_unit_interval(a.act, "act")
    if a.require is not None:
        out["require"] = valid_pattern(a.require)
    if a.require_min is not None:
        out["require_min"] = valid_count(a.require_min, 1, 100, "require-min")
    if getattr(a, "detector", None):
        if a.detector not in ("language:nl", "language:en"):
            raise ValidationError("detector must be language:nl or language:en")
        out["detector"] = a.detector
    return out


def _confirm(a, message: str) -> bool:
    """Ask before changing files. `--yes` skips the question; a script without `--yes` is refused rather than guessed at."""
    if getattr(a, "yes", False) or getattr(a, "dry_run", False):
        return True
    if sys.stdin.isatty():
        try:
            return input(message + "\n" + t("cli.tags.continue") + " ").strip().lower()[:1] in t("cli.yesLetters")
        except EOFError:
            return False
    print(message + "\n" + t("cli.tags.nothingChanged"), file=sys.stderr)
    return False


def _impact(cfg, sd, mapping: dict[str, str]) -> int:
    return rewrite_files(cfg, sd, mapping, False)["files"]


def add_vocabulary(tag: str, description: str, options: dict | None = None, enable: bool = True) -> str:
    """The one place a descriptive tag is added to the user's vocabulary (validated). `options` are validated extra settings such as
    act, require, require_min or detector. `enable` also removes the tag from the disabled list. Returns the normalised tag name."""
    tag = valid_tag(tag)
    vocab = dict(cfgmod.user_value("vocabulary", {}))
    vocab[tag] = {"description": clean_text(description, what="description"), **(options or {})}
    update: dict = {"vocabulary": vocab}
    if enable:
        update["disabled_tags"] = [x for x in (cfgmod.user_value("disabled_tags", []) or []) if x != tag]
    cfgmod.save_user(update)
    return tag


def op_add(cfg, a, sd) -> int:
    if not a.tag or not a.desc:
        print("usage: sift tags add TAG --desc DESCRIPTION [--act N] [--require REGEX]", file=sys.stderr)
        return 2
    tag = add_vocabulary(a.tag, a.desc, _checked_options(a))
    print(f"added {tag}")
    return 0


def op_edit(cfg, a, sd) -> int:
    """Change a tag's description or settings, and optionally rename it (`--to NEW`), all in one step."""
    tree, tag = load_tree_for(cfg), a.tag
    if tag not in tree.vocabulary:
        if a.desc is not None or a.act is not None or a.require:
            print(t("cli.tags.builtin", tag=tag), file=sys.stderr)
            return 1
        return op_rename(cfg, a, sd) if a.into else 0
    vocab = _user_vocab(cfg, tree, tag)
    if a.desc is not None:
        vocab[tag]["description"] = clean_text(a.desc, what="description")
    vocab[tag].update(_checked_options(a))
    if not a.into:
        cfgmod.save_user({"vocabulary": vocab})
        print(f"updated {tag}")
        return 0
    return _rename(cfg, tree, a, sd, tag, valid_tag(a.into), vocab)


def op_rename(cfg, a, sd) -> int:
    tree, old = load_tree_for(cfg), a.tag
    if not a.into or not old:
        print("usage: sift tags rename OLD --to NEW", file=sys.stderr)
        return 2
    return _rename(cfg, tree, a, sd, old, valid_tag(a.into), _user_vocab(cfg, tree, old) if old in tree.vocabulary else None)


def _rename(cfg, tree, a, sd, old: str, new: str, vocab: dict | None) -> int:
    if new == old:
        print(t("cli.tags.same"), file=sys.stderr)
        return 1
    if new in {r["tag"] for r in inventory(cfg, sd)}:
        print(t("cli.tags.exists", new=new, old=old), file=sys.stderr)
        return 1
    if vocab is None:       # built-in label or tag on files: the map renames what gets emitted
        update = {"tag_map": _map_with(cfg, old, new)}
    else:
        vocab[new] = vocab.pop(old)
        disabled = set(cfg.get("disabled_tags", []))
        if old in _default_vocab():
            disabled.add(old)
        update = {"vocabulary": vocab, "tag_map": _map_with(cfg, old, new), "disabled_tags": sorted(disabled)}
    n = _impact(cfg, sd, {old: new})
    if n and not _confirm(a, f"Rename the tag {old!r} to {new!r}? This rewrites the tags of {n} files (their other tags stay). "
                             "You can undo it with `sift untag`."):
        return 3
    stats = rewrite_files(cfg, sd, {old: new}, not a.dry_run)
    if not a.dry_run:
        cfgmod.save_user(update)
    print(json.dumps(stats) if a.json else f"{'would rename' if a.dry_run else 'renamed'} {old} -> {new}: {stats['files']} files" +
          ("" if a.dry_run else f", {stats['changed']} rewritten, {stats['skipped']} skipped"))
    return 0


def _default_vocab() -> set[str]:
    return set(json.loads((cfgmod.ROOT / "tree.json").read_text()).get("vocabulary", {}))


def op_merge(cfg, a, sd) -> int:
    dest = valid_tag(a.into) if a.into else ""
    sources = [t for t in (a.tags or []) if t != dest]
    if not sources or not dest:
        print("usage: sift tags merge TAG [TAG...] --to DEST", file=sys.stderr)
        return 2
    tree = load_tree_for(cfg)
    known = {r["tag"] for r in inventory(cfg, sd)}
    m = dict(cfg.get("tag_map") or {})
    update: dict = {}
    if dest not in known:                                  # a brand new destination needs a definition
        src_desc = next((tree.vocabulary[s].description for s in sources if s in tree.vocabulary), None)
        desc = clean_text(a.desc, what="description") if a.desc else src_desc
        if not desc:
            print(f"{dest!r} is new: give it a description with --desc", file=sys.stderr)
            return 2
        vocab = dict(cfgmod.user_value("vocabulary", {})); vocab[dest] = {"description": desc}
        update["vocabulary"] = vocab
    for s in sources:
        m = _map_with({**cfg, "tag_map": m}, s, dest)
    update["tag_map"] = m
    n = _impact(cfg, sd, {s: dest for s in sources})
    if n and not _confirm(a, f"Merge {', '.join(sources)} into {dest!r}? {n} files carrying {'one of them' if len(sources) > 1 else 'it'} will get "
                             f"{dest!r} instead (once). The merged tags are still recognised but only {dest!r} is written. "
                             "You can undo it with `sift untag`."):
        return 3
    stats = rewrite_files(cfg, sd, {s: dest for s in sources}, not a.dry_run, journal=True)
    if not a.dry_run:
        cfgmod.save_user(update)
    print(json.dumps(stats) if a.json else f"{'would merge' if a.dry_run else 'merged'} {', '.join(sources)} -> {dest}: {stats['files']} files" +
          ("" if a.dry_run else f", {stats['changed']} rewritten, {stats['skipped']} skipped. Both phrasings are still detected; files get {dest}."))
    return 0


def plan_unmerge(cfg: dict, sd: Path, source: str) -> dict:
    """Which files get `source` back when it is unmerged. Journaled merges restore exactly (and drop the destination again when the file did
    not have it before); merges from before the journal are restored best-effort from the tags Sift once recorded for each file."""
    src = flat(source)
    tmap = dict(cfg.get("tag_map") or {})
    dest = flat(tmap.get(source, tmap.get(src, "")))
    if not dest:
        return {"dest": "", "files": [], "exact": True}
    remaining = {flat(k): flat(v) for k, v in tmap.items() if flat(k) != src and v}
    last: dict[str, dict] = {}
    for e in state.read(sd / "merges.jsonl"):
        if e["src"] == src:
            last[e["path"]] = e
    files: list[dict] = []
    for p, e in last.items():
        others = {o["src"] for o in state.read(sd / "merges.jsonl") if o["path"] == p and o["dst"] == e["dst"] and o["src"] != src and remaining.get(o["src"]) == e["dst"]}
        files.append({"path": p, "inode": e["inode"], "add": src, "drop": e["dst"] if not e["had_dst"] and not others else ""})
    exact = bool(files)
    if not files:                                                 # best effort for older merges
        seen = {p for e in state.read(sd / "report.jsonl") if src in map(flat, e.get("tags", [])) for p in [e["path"]]}
        for p in seen:
            try:
                st = os.stat(p, follow_symlinks=False)
                if dest in read_tags(p) and src not in read_tags(p):
                    files.append({"path": p, "inode": st.st_ino, "add": src, "drop": ""})
            except OSError:
                continue
    return {"dest": dest, "files": files, "exact": exact}


def op_unmerge(cfg, a, sd) -> int:
    """Undo one merge: `sift tags unmerge SOURCE`. The source is recognised and written on its own again, and files that carried it get it back."""
    source = a.tag
    plan = plan_unmerge(cfg, sd, source)
    if not plan["dest"]:
        print(f"{source!r} is not merged into another tag", file=sys.stderr)
        return 1
    n = len(plan["files"])
    if not _confirm(a, f"Unmerge {source!r} from {plan['dest']!r}? {n} files will get {source!r} back"
                       + (" (and lose " + repr(plan["dest"]) + " where it only came from this merge)" if any(f["drop"] for f in plan["files"]) else "")
                       + ("" if plan["exact"] else ". This merge was done before changes were recorded, so the list comes from earlier scan results and may be incomplete")
                       + f"; {source!r} will be written on its own again. You can undo the file changes with `sift untag`."):
        return 3
    changed = skipped = 0
    if not a.dry_run:
        store = XattrStore(cfgmod.dirs(cfg), sd / "undo.jsonl")
        for f in plan["files"]:
            try:
                st = os.stat(f["path"], follow_symlinks=False)
                if st.st_ino != f["inode"]:
                    raise StoreError("identity")
                cur = read_tags(f["path"])
                new = sorted((set(cur) | {f["add"]}) - ({f["drop"]} if f["drop"] else set()))
                if new != sorted(cur):
                    dec = Decision(FileRef(f["path"], st.st_ino, st.st_mtime_ns, st.st_size), (), Outcome.ACT)
                    store._check(dec)
                    state.append(sd / "undo.jsonl", {"path": f["path"], "inode": st.st_ino, "old": ",".join(cur), "new": ",".join(new)})
                    write_tags(f["path"], new)
                    changed += 1
            except (StoreError, OSError):
                skipped += 1
        tmap = {k: v for k, v in dict(cfg.get("tag_map") or {}).items() if flat(k) != flat(source)}
        cfgmod.save_user({"tag_map": tmap})
    out = {"source": source, "dest": plan["dest"], "files": n, "changed": changed, "skipped": skipped, "exact": plan["exact"]}
    print(json.dumps(out) if a.json else f"{'would unmerge' if a.dry_run else 'unmerged'} {source} from {plan['dest']}: {n} files, {changed} restored, {skipped} skipped")
    return 0


MAX_IGNORED = 500


def op_ignore_merge(cfg, a) -> int:
    """`sift tags ignore-merge TAG TAG...` keeps these tags separate: Sift stops proposing to merge them. `--reset` forgets all of these choices."""
    if getattr(a, "reset", False):
        cfgmod.save_user({"ignored_merges": []})
        print("merge proposals you dismissed will be shown again")
        return 0
    group = sorted({clean_text(x, 60, what="tag") for x in [a.tag, *(a.tags or [])] if x})
    if len(group) < 2:
        print("usage: sift tags ignore-merge TAG TAG [TAG...]", file=sys.stderr)
        return 2
    cur = [g for g in (cfg.get("ignored_merges") or []) if isinstance(g, list)]
    if group not in cur:
        cur.append(group)
    cfgmod.save_user({"ignored_merges": cur[-MAX_IGNORED:]})
    print(f"keeping separate: {', '.join(group)}")
    return 0


def op_delete(cfg, a, sd) -> int:
    tree, tag = load_tree_for(cfg), a.tag
    update: dict = {"tag_map": _map_with(cfg, tag, "")}
    vocab = dict(cfgmod.user_value("vocabulary", {}))
    if tag in vocab:
        vocab.pop(tag); update["vocabulary"] = vocab
    if tag in _default_vocab():
        update["disabled_tags"] = sorted({*cfg.get("disabled_tags", []), tag})
    n = _impact(cfg, sd, {tag: ""})
    if not _confirm(a, f"Delete the tag {tag!r}? This removes it from {n} files (their other tags stay; no file is deleted) and "
                       "stops Sift from adding it again. You can undo the file changes with `sift untag`."):
        return 3
    stats = rewrite_files(cfg, sd, {tag: ""}, not a.dry_run)
    if not a.dry_run:
        cfgmod.save_user(update)
    print(json.dumps(stats) if a.json else f"{'would delete' if a.dry_run else 'deleted'} {tag}: {stats['files']} files" +
          ("" if a.dry_run else f", {stats['changed']} rewritten; it will not be added again"))
    return 0


def run(a, cfg) -> int:
    sd = state.state_dir()
    if a.action == "merge" and a.tag and a.tag not in (a.tags or []):
        a.tags = [a.tag, *(a.tags or [])]
    try:
        if a.action == "list":
            rows = inventory(cfg, sd)
            print(json.dumps(rows) if a.json else "\n".join(
                f"{r['tag']:<26} {r['files']:>6} files  {r['kind']:<10} {('aliases: ' + ', '.join(r['aliases'])) if r['aliases'] else ''}" for r in rows))
            return 0
        if a.action == "scan":
            meta_path = sd / "disk_tags_meta.json"
            if getattr(a, "if_older", None):
                try:
                    meta = state.read_json(meta_path, {})
                    if isinstance(meta, dict) and time.time() - float(meta.get("ts", 0)) < a.if_older:
                        print(json.dumps(meta)); return 0
                except (OSError, ValueError, KeyError):
                    pass
            print(json.dumps(scan_disk(cfg, sd)))
            return 0
        if a.action == "similar":
            rows = inventory(cfg, sd)
            groups = similar_groups({r["tag"]: r["files"] for r in rows if r["files"] or r["kind"] == "vocabulary"}, ignored=cfg.get("ignored_merges"))
            print(json.dumps(groups) if a.json else "\n".join(f"merge {', '.join(g['merge'])} -> {g['keep']}  ({g['files']} files)" for g in groups) or "(no similar tags)")
            return 0
        if a.action == "ignore-merge":
            return op_ignore_merge(cfg, a)
        if not a.tag and a.action not in ("merge", "scan"):
            print(f"usage: sift tags {a.action} TAG ...", file=sys.stderr)
            return 2
        return {"add": op_add, "edit": op_edit, "rename": op_rename, "merge": op_merge, "unmerge": op_unmerge, "delete": op_delete}[a.action](cfg, a, sd)
    except (cfgmod.ConfigError, ValidationError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


def register(sub) -> None:
    p = sub.add_parser("tags", help="tag manager: list | scan | similar | add | edit | rename | merge | unmerge | ignore-merge | delete")
    p.add_argument("action", choices=["list", "scan", "similar", "add", "edit", "rename", "merge", "unmerge", "ignore-merge", "delete"])
    p.add_argument("--if-older", type=int, help="scan: skip when the last scan is newer than this many seconds")
    p.add_argument("tag", nargs="?")
    p.add_argument("tags", nargs="*", help="merge: further tags to merge into --to")
    p.add_argument("--to", dest="into", help="edit/rename/merge: the new or destination tag name")
    p.add_argument("--desc"); p.add_argument("--act", type=float); p.add_argument("--require"); p.add_argument("--require-min", type=int)
    p.add_argument("--detector", help="decide by code instead of the model: language:nl or language:en (for example a `dutch` tag)")
    p.add_argument("--dry-run", action="store_true"); p.add_argument("--json", action="store_true")
    p.add_argument("--reset", action="store_true", help="ignore-merge: show every dismissed merge proposal again")
    p.add_argument("--yes", action="store_true", help="do not ask before changing files")
    p.set_defaults(run=run)
