"""`sift backends` (what is being used) and `sift usage` (token estimates)."""
from __future__ import annotations

import json

from .. import config as cfgmod
from .. import state, usage
from ..api import describe_backends


def run_backends(a, cfg) -> int:
    try:
        model = ""
        try:                                      # the default backend's real model name, when reachable
            from ..api import make_classifier
            model = make_classifier("default", cfgmod.backends(cfg)["default"], cfg).health().get("model", "")
        except Exception:
            pass
        rows = describe_backends(cfg, model)
    except cfgmod.ConfigError as e:
        print(f"error: {e}")
        return 1
    if a.json:
        print(json.dumps(rows))
        return 0
    if not rows:
        print("no classifier configured: run `sift setup`")
    for r in rows:
        where = "this machine or your network" if r["local"] else f"remote service {r['host']} (file text is sent there)"
        print(f"{r['name']}: {r['type']} {r['model']}".rstrip())
        print(f"    {r['role']}; {r['mode']}")
        print(f"    runs on: {where}; key: {r['key']}")
    return 0


def usage_summary(cfg) -> dict:
    remaining = None
    idx = state.read_json(state.state_dir() / "index_status.json", {})
    if isinstance(idx, dict):
        remaining = idx.get("remaining")
    return usage.summarize(cfg, remaining)


def run_usage(a, cfg) -> int:
    s = usage_summary(cfg)
    if a.json:
        print(json.dumps(s))
        return 0
    for name in ("today", "week", "total"):
        p = s["periods"][name]
        t = p["tokens_in"] + p["tokens_out"]
        est = " (partly estimated)" if p["estimated"] else ""
        cost = f", about ${p['cost']:.2f}" if p["priced"] else ""
        print(f"{name:<6} {usage.fmt_tokens(t):>8} tokens in {p['requests']} requests{est}{cost}")
    for b, d in s["backends"].items():
        print(f"  {b}: {usage.fmt_tokens(d['tokens'])} tokens, {d['requests']} requests")
    if "remaining_tokens" in s:
        cost = f" (about ${s['remaining_cost']:.2f})" if "remaining_cost" in s else ""
        print(f"finishing the current scan: about {usage.fmt_tokens(s['remaining_tokens'])} tokens{cost}, from {s['per_file_tokens']} tokens per file on average")
    else:
        print("not enough scanned files yet to estimate the cost of finishing the scan")
    print("Numbers come from the service when it reports them; otherwise they are estimated at about 4 characters per token.")
    return 0


def register(sub) -> None:
    b = sub.add_parser("backends", help="which classifier and models are in use, and where they run")
    b.add_argument("--json", action="store_true"); b.set_defaults(run=run_backends)
    u = sub.add_parser("usage", help="token usage and the estimated cost of finishing the scan")
    u.add_argument("--json", action="store_true"); u.set_defaults(run=run_usage)
