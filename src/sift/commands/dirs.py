"""Watched and ignored folders: `sift dirs list | add | remove | ignore | unignore`."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from .. import config as cfgmod
from ..i18n import t
from ..validate import ValidationError, valid_glob
from ..walk import NEVER_GLOBS, SKIP_DIRS


def run(a, cfg) -> int:
    c = cfgmod._migrate(cfg)
    dirs = list(c.get("dirs", ["~/Downloads"]))
    ignore = list(c.get("ignore", []))
    if a.action == "list":
        out = {"dirs": dirs, "ignore": ignore, "always_skipped": [*NEVER_GLOBS, *sorted(SKIP_DIRS), "hidden folders", "mount points"]}
        if a.json:
            print(json.dumps(out))
        else:
            print("scanned folders:", *(f"  {d}" for d in dirs), "ignored patterns:", *(f"  {g}" for g in ignore or ["  (none)"]), sep="\n")
        return 0
    if not a.value:
        print(f"usage: sift dirs {a.action} VALUE", file=sys.stderr)
        return 2
    if a.action in ("add", "remove"):
        shown = a.value if a.value.startswith("~") else str(Path(a.value).expanduser().resolve())
        if a.action == "add":
            p = Path(shown).expanduser()
            if not p.is_dir():
                print("error: " + t("cli.dirs.notFolder", path=p), file=sys.stderr)
                return 1
            if os.path.ismount(p) and not a.force:
                print("error: " + t("cli.dirs.mount", path=p), file=sys.stderr)
                return 1
            if p.resolve() == Path("/"):
                print("error: " + t("cli.dirs.root"), file=sys.stderr)
                return 1
            if shown not in dirs:
                dirs.append(shown)
        else:
            dirs = [d for d in dirs if Path(d).expanduser().resolve() != Path(shown).expanduser().resolve()]
        cfgmod.save_user({"dirs": dirs})
        print("scanned folders:", ", ".join(dirs) or "(none)")
        return 0
    if a.action == "ignore":
        try:
            g = valid_glob(a.value)
        except ValidationError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        if g not in ignore:
            ignore.append(g)
    if a.action == "unignore":
        ignore = [g for g in ignore if g != a.value]
    cfgmod.save_user({"ignore": ignore})
    print("ignored patterns:", ", ".join(ignore) or "(none)")
    return 0


def register(sub) -> None:
    p = sub.add_parser("dirs", help="choose which folders are scanned and which names or paths are ignored")
    p.add_argument("action", choices=["list", "add", "remove", "ignore", "unignore"])
    p.add_argument("value", nargs="?", help="a folder, or an ignore pattern such as node_modules, *.bak or ~/Documents/private/*")
    p.add_argument("--json", action="store_true"); p.add_argument("--force", action="store_true")
    p.set_defaults(run=run)
