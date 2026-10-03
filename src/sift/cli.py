"""The `sift` command line: parser and dispatch only. The commands live in `sift.commands.*`:
scanning (scan, index, prune, watch), review (status, review, accept, reject, apply, untag), research (discover, vocab, propose, survey,
eval, sample), and the registry modules for the rest."""
from __future__ import annotations

import argparse
import os
import sys

from . import config as cfgmod
from .adapters.http import BackendError
from .adapters.stores import StoreError
from .commands.research import cmd_discover, cmd_eval, cmd_propose, cmd_sample, cmd_survey, cmd_vocab
from .commands.review import cmd_accept, cmd_apply, cmd_reject, cmd_review, cmd_status, cmd_untag
from .commands.scanning import cmd_index, cmd_prune, cmd_scan, cmd_watch
from .validate import ValidationError


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="sift")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan"); s.add_argument("dir"); s.add_argument("--report")
    s.add_argument("--apply", action="store_true", help="write xattr tags (needs write_xattrs=true)")
    s.add_argument("--json", action="store_true"); s.add_argument("--rescan", action="store_true", help="reclassify unchanged files")
    w = sub.add_parser("watch"); w.add_argument("--once", action="store_true"); w.add_argument("--json", action="store_true")
    ix = sub.add_parser("index", help="classify eligible files under index_dirs within a budget (for timers)")
    ix.add_argument("--budget", type=int); ix.add_argument("--max-seconds", type=int); ix.add_argument("--dry-run", action="store_true")
    pr_ = sub.add_parser("prune", help="re-check written tags against the deterministic gates (dry run unless --apply)")
    pr_.add_argument("--apply", action="store_true")
    st = sub.add_parser("status"); st.add_argument("--json", action="store_true")
    r = sub.add_parser("review"); r.add_argument("--json", action="store_true"); r.add_argument("--limit", type=int, default=50)
    r.add_argument("--all", action="store_true", help="include generic low-value guesses (log, code, other, ...)")
    ac = sub.add_parser("accept"); ac.add_argument("path"); ac.add_argument("--tags")
    ap_ = sub.add_parser("apply", help="tag files whose report outcome is act"); ap_.add_argument("--json", action="store_true")
    vo = sub.add_parser("vocab"); vo.add_argument("action", choices=["list", "add"]); vo.add_argument("tag", nargs="?")
    vo.add_argument("description", nargs="?"); vo.add_argument("--act", type=float)
    pr = sub.add_parser("propose", help="ask the configured local LLM for new tag names"); pr.add_argument("path")
    pr.add_argument("-n", type=int, default=5); pr.add_argument("--json", action="store_true")
    dc = sub.add_parser("discover", help="find new tags: collect | run | list | accept TAG | reject TAG")
    dc.add_argument("action", choices=["collect", "run", "list", "accept", "reject"]); dc.add_argument("tag", nargs="?")
    dc.add_argument("-n", type=int, default=100, help="collect: files to ask the proposer about")
    dc.add_argument("--top", type=int, default=24); dc.add_argument("--min-support", type=int, default=3)
    dc.add_argument("--desc"); dc.add_argument("--act", type=float); dc.add_argument("--json", action="store_true")
    sv = sub.add_parser("survey", help="sample folders and collect proposed tag names (state dir only)")
    sv.add_argument("dirs", nargs="+"); sv.add_argument("-n", type=int, default=5000)
    sv.add_argument("--depth", type=int, default=3); sv.add_argument("--seed", type=int, default=1)
    sv.add_argument("--max-per-folder", type=int, default=60, help="cap per folder so large trees do not dominate (0 = no cap)")
    sv.add_argument("--propose", type=int, default=0, help="stage B: ask the proposer for tags on this many text files")
    rj = sub.add_parser("reject"); rj.add_argument("path")
    u = sub.add_parser("untag"); u.add_argument("--last", type=int); u.add_argument("--path"); u.add_argument("--all", action="store_true")
    e = sub.add_parser("eval"); e.add_argument("labels", nargs="?"); e.add_argument("--builtin", action="store_true")
    e.add_argument("--seed", type=int, default=7); e.add_argument("--out")
    e.add_argument("--sweep", help="comma-separated ACT thresholds, e.g. 0.5,0.6,0.7,0.8")
    sm = sub.add_parser("sample"); sm.add_argument("dir"); sm.add_argument("-n", type=int, default=100)
    sm.add_argument("--seed", type=int, default=1); sm.add_argument("--out", default="eval/labels.csv")
    from .commands import register_all
    register_all(sub)
    a = ap.parse_args(argv)
    if a.cmd == "eval" and not (a.labels or a.builtin):
        ap.error("give a labels CSV or --builtin")
    try:
        cfg = cfgmod.load()
    except cfgmod.ConfigError as ex:
        print(f"error: {ex}", file=sys.stderr)
        if a.cmd not in ("doctor", "setup", "config"):
            print("fix the setting, or run `sift doctor` / `sift setup`", file=sys.stderr)
            return 1
        cfg = {}                                            # these commands exist to repair a broken configuration
    from . import demo, i18n
    i18n.init(cfg)
    if demo.active():
        from .commands.demo_cmd import blocked
        why = blocked(a)
        if why:
            print(f"error: {why}", file=sys.stderr)
            return 2
    fn = getattr(a, "run", None) or {"scan": cmd_scan, "prune": cmd_prune, "index": cmd_index, "watch": cmd_watch, "status": cmd_status, "review": cmd_review, "accept": cmd_accept,
          "apply": cmd_apply, "vocab": cmd_vocab, "discover": cmd_discover, "survey": cmd_survey, "propose": cmd_propose, "reject": cmd_reject, "untag": cmd_untag, "eval": cmd_eval, "sample": cmd_sample}[a.cmd]
    try:
        return fn(a, cfg)
    except (cfgmod.ConfigError, BackendError, StoreError, ValidationError) as ex:
        print(f"error: {ex}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    except (OSError, KeyError, ValueError, TypeError, AttributeError) as ex:      # a bug or damaged data: a short message, never a stack trace
        if os.environ.get("SIFT_DEBUG"):
            raise
        print(f"error: unexpected {type(ex).__name__}: {str(ex)[:200]} (run with SIFT_DEBUG=1 for the details)", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
