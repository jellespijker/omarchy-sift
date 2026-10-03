"""Editable prompts and prompt profiles: `sift prompts list|set|reset`, `sift profiles list|add|remove|test`."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from .. import config as cfgmod
from ..api import _expand, load_tree_for
from ..core import profiles as prof
from ..core import prompts as P
from ..validate import ValidationError, valid_prompt


def _fail(msg: str) -> int:
    print(f"error: {msg}", file=sys.stderr)
    return 1


def run_prompts(a, cfg) -> int:
    mine = dict(cfg.get("prompts") or {})
    tree = load_tree_for(cfg)
    if a.action == "list":
        eff = P.effective(prof.parse_prompts(mine, "prompts")[0])
        out = [{"name": k, "text": eff[k], "custom": k in mine, "placeholders": list(P.ALLOWED[k]), "about": P.DESCRIPTIONS[k]} for k in P.DEFAULTS]
        nodes = [{"name": f"node:{nid}", "text": n.instructions, "custom": nid in (mine.get("nodes") or {}), "placeholders": [],
                  "about": f"the question asked at tree node '{nid}'"} for nid, n in tree.nodes.items()]
        if a.json:
            print(json.dumps(out + nodes))
        else:
            for r in out + nodes:
                print(f"{r['name']}{' (custom)' if r['custom'] else ''}: {r['about']}\n  {r['text']}\n")
        return 0
    name = a.name or ""
    node = name[5:] if name.startswith("node:") else None
    if node is None and name not in P.DEFAULTS:
        return _fail(f"unknown prompt {name!r}; use one of: {', '.join([*P.DEFAULTS, 'node:<id>'])}")
    if node is not None and node not in tree.nodes:
        return _fail(f"unknown tree node {node!r}; nodes: {', '.join(tree.nodes)}")
    if a.action == "reset":
        if node is not None:
            nodes = dict(mine.get("nodes") or {}); nodes.pop(node, None); mine["nodes"] = nodes
        else:
            mine.pop(name, None)
        cfgmod.save_user({"prompts": mine})
        print(f"{name}: back to the default")
        return 0
    text = sys.stdin.read() if a.text == "-" else (a.text or "")
    try:
        clean = valid_prompt(text, () if node is not None else P.ALLOWED[name])
    except ValidationError as e:
        return _fail(str(e))
    if node is not None:
        mine["nodes"] = {**(mine.get("nodes") or {}), node: clean}
    else:
        mine[name] = clean
    cfgmod.save_user({"prompts": mine})
    print(f"{name}: saved. Cached results stay until files change; run `sift index` to re-score.")
    return 0


def run_profiles(a, cfg) -> int:
    user = [p for p in (cfg.get("profiles") or []) if isinstance(p, dict)]
    tree = load_tree_for(cfg)
    if a.action == "list":
        mine = {p.get("name") for p in user}
        rows = [{"name": p.name, "custom": p.name in mine, "dirs": list(p.dirs), "extensions": sorted(p.extensions), "globs": list(p.globs),
                 "skip": p.skip, "tags": None if p.tags is None else list(p.tags), "extra_tags": sorted(p.vocabulary),
                 "prompts": sorted([*p.prompts, *(f"node:{n}" for n in p.node_prompts)]), "exclude_labels": list(p.exclude_labels),
                 "act": p.act, "review": p.review, "skip_vocabulary": None if p.skip_vocabulary is None else list(p.skip_vocabulary)} for p in tree.profiles]
        if a.json:
            print(json.dumps(rows))
        else:
            for r in rows:
                where = ", ".join([*r["dirs"], *r["extensions"], *r["globs"]])
                what = "skipped" if r["skip"] else ", ".join(filter(None, [
                    "no descriptive tags" if r["tags"] == [] else ("only " + "/".join(r["tags"]) if r["tags"] else ""),
                    "+" + "/".join(r["extra_tags"]) if r["extra_tags"] else "", "own prompts" if r["prompts"] else "",
                    "without " + "/".join(r["exclude_labels"]) if r["exclude_labels"] else "", f"act {r['act']}" if r["act"] is not None else ""])) or "defaults"
                print(f"{r['name']}{' (custom)' if r['custom'] else ''}: {where} -> {what}")
            if not rows:
                print("no profiles")
        return 0
    if a.action == "test":
        p = prof.select(tree.profiles, str(Path(a.name).expanduser().resolve()))
        print(json.dumps({"profile": p.name if p else None, "skip": bool(p and p.skip)}) if a.json else (f"profile: {p.name}" if p else "no profile matches; defaults apply"))
        return 0
    if not a.name:
        return _fail(f"usage: sift profiles {a.action} NAME")
    if a.action == "remove":
        if not any(p.get("name") == a.name for p in user):
            return _fail(f"no custom profile {a.name!r}" + (" (packaged profiles cannot be removed; override them with the same name, or use --skip)" if any(p.name == a.name for p in tree.profiles) else ""))
        cfgmod.save_user({"profiles": [p for p in user if p.get("name") != a.name]})
        print(f"profile {a.name!r} removed")
        return 0
    raw: dict = {"name": a.name, "match": {"dirs": a.dir or [], "extensions": a.ext or [], "globs": a.glob or []}}
    if a.skip:
        raw["skip"] = True
    if a.no_tags:
        raw["tags"] = []
    elif a.tags is not None:
        raw["tags"] = [t for t in a.tags.split(",") if t.strip()]
    if a.act is not None:
        raw["act"] = a.act
    if a.skip_vocab is not None:
        raw["skip_vocabulary"] = [x for x in a.skip_vocab.split(",") if x.strip()]
    if a.exclude_label:
        raw["exclude_labels"] = a.exclude_label
    pr: dict = {}
    for item in a.prompt or []:
        k, _, v = item.partition("=")
        if k.startswith("node:"):
            pr.setdefault("nodes", {})[k[5:]] = v
        else:
            pr[k] = v
    if pr:
        raw["prompts"] = pr
    try:
        prof.parse_profile(_expand(raw))
    except (ValidationError, KeyError, TypeError) as e:
        return _fail(str(e))
    if raw["prompts"] if "prompts" in raw else False:
        _, _, problems = prof.parse_prompts(raw["prompts"], "profile")
        if problems:
            return _fail(problems[0])
    cfgmod.save_user({"profiles": [*(p for p in user if p.get("name") != a.name), raw]})
    print(f"profile {a.name!r} saved")
    return 0


def register(sub) -> None:
    p = sub.add_parser("prompts", help="show, change or reset the prompts sent to the classifier and tag proposer")
    p.add_argument("action", choices=["list", "set", "reset"])
    p.add_argument("name", nargs="?", help="tag_question | chat_system | propose | node:<id>")
    p.add_argument("text", nargs="?", help='the new prompt, or "-" to read it from standard input')
    p.add_argument("--json", action="store_true")
    p.set_defaults(run=run_prompts)
    q = sub.add_parser("profiles", help="use different prompts, tags or thresholds for certain folders, file types or names")
    q.add_argument("action", choices=["list", "add", "remove", "test"])
    q.add_argument("name", nargs="?", help="profile name (for test: a file path)")
    q.add_argument("--dir", action="append", help="folder the profile applies to (repeatable)")
    q.add_argument("--ext", action="append", help="file extension, for example .pdf (repeatable)")
    q.add_argument("--glob", action="append", help="file name pattern, for example '*invoice*' (repeatable)")
    q.add_argument("--skip", action="store_true", help="never scan these files")
    q.add_argument("--tags", help="comma-separated descriptive tags to allow (others are not scored)")
    q.add_argument("--no-tags", action="store_true", help="no descriptive tags at all")
    q.add_argument("--act", type=float, help="confidence needed to write a tag (0-1)")
    q.add_argument("--skip-vocabulary", dest="skip_vocab", help="first-question labels that get no general tags, comma-separated (empty = tag everything)")
    q.add_argument("--exclude-label", action="append", help="remove a label from the first question, for example code")
    q.add_argument("--prompt", action="append", help="NAME=TEXT, for example tag_question=... or node:root=...")
    q.add_argument("--json", action="store_true")
    q.set_defaults(run=run_profiles)
