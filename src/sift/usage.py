"""Token usage ledger: one JSON line per request, plus a marker per classified file. Numbers come from the service when it reports
them (`usage` in the response) and are estimated (about 4 characters per token) when it does not; estimates are flagged so a total is
never presented as more exact than it is. Never stores file text."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Mapping

from . import state

CHARS_PER_TOKEN = 4.0


def estimate_tokens(chars: int) -> int:
    return max(int(chars / CHARS_PER_TOKEN), 1)


def record(backend: str, kind: str, tokens_in: int, tokens_out: int, estimated: bool = False, cost: float | None = None) -> None:
    line: dict[str, Any] = {"b": backend, "k": kind, "in": int(tokens_in), "out": int(tokens_out)}
    if estimated:
        line["est"] = 1
    if cost is not None:
        line["cost"] = float(cost)
    state.append(state.state_dir() / "usage.jsonl", line)


def mark_file(backend: str) -> None:
    state.append(state.state_dir() / "usage.jsonl", {"b": backend, "k": "file"})


def make_hook(backend: str, kind: str):
    """Callback handed to an adapter: `hook(tokens_in, tokens_out, estimated, cost)`."""
    def hook(tokens_in: int, tokens_out: int, estimated: bool = False, cost: float | None = None) -> None:
        try:
            record(backend, kind, tokens_in, tokens_out, estimated, cost)
        except OSError:
            pass                                    # usage accounting must never break a scan
    return hook


def _int(x: Any) -> int:
    try:
        return int(x)
    except (TypeError, ValueError):
        return 0                                    # a damaged ledger line counts as nothing instead of stopping the panel


def roll_up(path: Path, keep_days: int = 30, now: float | None = None) -> tuple[int, int]:
    """Replace requests older than `keep_days` by one line per (day, backend, kind) with a count `n`, so the ledger stays small and
    totals stay exact. Returns (lines before, lines after)."""
    now = now or time.time()
    cutoff = now - keep_days * 86400
    with state.lock(path.with_name(path.name + ".flock")):
        rows = state.read(path)
        recent: list[dict] = []
        agg: dict[tuple, dict] = {}
        for e in rows:
            if e.get("ts", 0) >= cutoff:
                recent.append(e)
                continue
            day = int(e.get("ts", 0)) // 86400 * 86400
            a = agg.setdefault((day, e.get("b", "?"), e.get("k", "?")), {"b": e.get("b", "?"), "k": e.get("k", "?"), "ts": day, "n": 0, "in": 0, "out": 0,
                                                                         "est": 0, "_priced": True, "cost": 0.0})
            a["n"] += _int(e.get("n", 1))
            a["in"] += _int(e.get("in")); a["out"] += _int(e.get("out"))
            a["est"] += _int(e.get("est", 0))
            if "cost" in e:
                a["cost"] += float(e["cost"])
            elif e.get("k") != "file":
                a["_priced"] = False
        out = []
        for a in agg.values():
            priced = a.pop("_priced")
            if not priced or a["k"] == "file" or not a["cost"]:
                a.pop("cost")
            if a["k"] == "file":
                a.pop("in"); a.pop("out"); a.pop("est")
            out.append(a)
        if len(out) + len(recent) < len(rows):
            state.write_atomic(path, "".join(json.dumps(r) + "\n" for r in [*out, *recent]))
        return len(rows), min(len(rows), len(out) + len(recent))


def _price(cfg: Mapping[str, Any], backend: str) -> tuple[float, float] | None:
    p = ((cfg.get("backends") or {}).get(backend) or {}).get("price")
    if isinstance(p, dict) and (p.get("input_per_million") is not None or p.get("output_per_million") is not None):
        return float(p.get("input_per_million") or 0), float(p.get("output_per_million") or 0)
    return None


def summarize(cfg: Mapping[str, Any], remaining_files: int | None = None, now: float | None = None) -> dict[str, Any]:
    """Totals for today, the last 7 days and overall, per backend, plus an estimate of what finishing the current scan will use."""
    now = now or time.time()
    lt = time.localtime(now)
    midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))      # the user's own midnight
    entries = state.read(state.state_dir() / "usage.jsonl")
    periods = {"today": midnight, "week": now - 7 * 86400, "total": 0.0}
    out: dict[str, Any] = {"periods": {k: {"requests": 0, "tokens_in": 0, "tokens_out": 0, "estimated": 0, "cost": 0.0, "priced": False} for k in periods},
                           "backends": {}, "files": 0}
    per: dict[str, dict[str, Any]] = {}
    files = 0
    for e in entries:
        b = e.get("b", "?")
        n = _int(e.get("n", 1))
        if e.get("k") == "file":
            files += n
            continue
        tin, tout = _int(e.get("in")), _int(e.get("out"))
        pr = _price(cfg, b)
        cost = e["cost"] if "cost" in e else ((tin * pr[0] + tout * pr[1]) / 1e6 if pr else None)
        for name, since in periods.items():
            if e.get("ts", 0) >= since:
                bucket = out["periods"][name]
                bucket["requests"] += n
                bucket["tokens_in"] += tin; bucket["tokens_out"] += tout
                bucket["estimated"] += _int(e.get("est", 0))
                if cost is not None:
                    bucket["cost"] += cost; bucket["priced"] = True
        d = per.setdefault(b, {"requests": 0, "tokens": 0, "estimated": 0, "cost": 0.0, "priced": False})
        d["requests"] += n; d["tokens"] += tin + tout; d["estimated"] += _int(e.get("est", 0))
        if cost is not None:
            d["cost"] += cost; d["priced"] = True
    out["backends"] = per
    out["files"] = files
    total = out["periods"]["total"]
    tokens = total["tokens_in"] + total["tokens_out"]
    if files >= 5 and remaining_files is not None:                 # need a few files before an average means anything
        per_file = tokens / files
        out["per_file_tokens"] = round(per_file)
        out["remaining_tokens"] = int(per_file * remaining_files)
        if total["priced"] and tokens:
            out["remaining_cost"] = (total["cost"] / files) * remaining_files
    return out


def fmt_tokens(n: int) -> str:
    return f"{n / 1e6:.1f}M" if n >= 1_000_000 else f"{n / 1e3:.1f}k" if n >= 1000 else str(n)
