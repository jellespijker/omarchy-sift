"""How often Sift scans: `sift schedule status | set INTERVAL | off | install`. Writes systemd user timers."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from .. import config as cfgmod
from .. import state

PRESETS = {"15m": 900, "30m": 1800, "1h": 3600, "2h": 7200, "6h": 21600, "12h": 43200, "daily": 86400, "weekly": 604800}
MIN_SECONDS = 300


def parse_interval(text: str) -> int | None:
    """Seconds for a preset ('30m', 'daily') or a number with unit ('45m', '3h', '2d'); None for 'off'."""
    t = text.strip().lower()
    if t in ("off", "never", "manual"):
        return None
    if t in PRESETS:
        return PRESETS[t]
    m = re.fullmatch(r"(\d+)([mhd])", t)
    if not m:
        raise cfgmod.ConfigError(f"unknown interval {text!r}: use off, daily, weekly, or a number with m/h/d such as 45m or 2h")
    secs = int(m.group(1)) * {"m": 60, "h": 3600, "d": 86400}[m.group(2)]
    if secs < MIN_SECONDS:
        raise cfgmod.ConfigError("the shortest interval is 5m: every run asks the classifier about many files")
    return secs


def systemd_dir() -> Path:
    return Path(os.environ.get("SIFT_SYSTEMD_DIR") or Path.home() / ".config/systemd/user")


def _exec(path: str) -> str:
    """The program path as systemd wants it: quoted, with % doubled (it is a specifier character)."""
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def _systemctl(*args: str) -> bool:
    try:
        return subprocess.run(["systemctl", "--user", *args], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False                                       # no systemd here (or it hung): the caller reports it


def unit_files(bin_path: str, index_secs: int | None, discover_secs: int | None, max_seconds: int = 1500) -> dict[str, str]:
    """Contents of the units to install. A missing interval means that timer is not installed. The service timeout always outlasts the
    longest allowed index run, so systemd never kills a run in the middle of a batch."""
    files: dict[str, str] = {}
    bin_path = _exec(bin_path)
    timeout = max(45 * 60, int(max_seconds) + 600)
    common = "Nice=15\nIOSchedulingClass=idle\nNoNewPrivileges=yes\nPrivateTmp=yes\nEnvironment=SIFT_DEMO=0\n"
    if index_secs:
        files["sift-index.service"] = ("[Unit]\nDescription=Sift: classify and tag files in the scanned folders (budgeted run)\n\n"
                                       f"[Service]\nType=oneshot\nExecStart={bin_path} index\nTimeoutStartSec={timeout}\n{common}")
        files["sift-index.timer"] = ("[Unit]\nDescription=Run the Sift indexer\n\n[Timer]\nOnBootSec=3min\n"
                                     f"OnUnitInactiveSec={index_secs}s\nRandomizedDelaySec=2min\n\n[Install]\nWantedBy=timers.target\n")
    if discover_secs:
        files["sift-discover.service"] = ("[Unit]\nDescription=Sift: collect tag proposals and test new tag candidates\n\n"
                                          f"[Service]\nType=oneshot\nExecStart={bin_path} discover collect -n 150\n"
                                          f"ExecStart={bin_path} discover run\nTimeoutStartSec=90min\n{common}")
        files["sift-discover.timer"] = ("[Unit]\nDescription=Sift tag discovery\n\n[Timer]\nOnBootSec=20min\n"
                                        f"OnUnitInactiveSec={discover_secs}s\nRandomizedDelaySec=30min\n\n[Install]\nWantedBy=timers.target\n")
    return files


def bin_path() -> str:
    return str(cfgmod.ROOT / "bin" / "sift")


def install(index: str, discover: str, systemctl: bool = True) -> dict[str, str]:
    i, d = parse_interval(index), parse_interval(discover)
    sd = systemd_dir()
    sd.mkdir(parents=True, exist_ok=True)
    wanted = unit_files(bin_path(), i, d, int(cfgmod.load().get("index_max_seconds", 1500)))
    for name in ("sift-index.service", "sift-index.timer", "sift-discover.service", "sift-discover.timer"):
        f = sd / name
        if name in wanted:
            state.write_atomic(f, wanted[name], 0o644)
        elif f.exists():
            if systemctl and name.endswith(".timer"):
                _systemctl("disable", "--now", name)
            f.unlink()
    started = True
    if systemctl:
        started = _systemctl("daemon-reload")
        for t in ("sift-index.timer", "sift-discover.timer"):
            if t in wanted:
                started = _systemctl("enable", "--now", t) and started
    cfgmod.save_user({"schedule": {"index": index, "discover": discover}})
    return {"index": index, "discover": discover, "systemd": "ok" if started else "unavailable"}


def status() -> dict:
    cfg = cfgmod.load()
    sched = cfg.get("schedule") or {"index": "off", "discover": "off"}
    timers = {}
    try:
        r = subprocess.run(["systemctl", "--user", "list-timers", "--all", "--no-pager", "--output=json"], capture_output=True, text=True, timeout=10)
        for t in json.loads(r.stdout or "[]"):
            if str(t.get("unit", "")).startswith("sift-"):
                nxt = t.get("next")
                timers[t["unit"]] = {"next_in_s": max(int(nxt / 1e6 - time.time()), 0) if nxt else None}
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    idx = state.read_json(state.state_dir() / "index_status.json", {})
    if not isinstance(idx, dict):
        idx = {}
    return {"schedule": sched, "timers": timers, "last_run": idx.get("last_run"), "remaining": idx.get("remaining")}


def run(a, cfg) -> int:
    try:
        if a.action == "status":
            s = status()
            print(json.dumps(s) if a.json else f"index: {s['schedule'].get('index', 'off')}, discovery: {s['schedule'].get('discover', 'off')}"
                  + (f", {s['remaining']} files left" if s.get("remaining") is not None else ""))
            return 0
        cur = cfg.get("schedule") or {}
        if a.action == "off":
            install("off", "off", not a.no_systemctl)
        elif a.action == "set":
            if not a.value:
                print("usage: sift schedule set INTERVAL [--discover INTERVAL]", file=sys.stderr)
                return 2
            install(a.value, a.discover or cur.get("discover", "weekly"), not a.no_systemctl)
        else:   # install: re-write units from the saved schedule
            install(cur.get("index", "1h"), cur.get("discover", "weekly"), not a.no_systemctl)
        print(json.dumps(status()["schedule"]))
        return 0
    except cfgmod.ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


def register(sub) -> None:
    p = sub.add_parser("schedule", help="how often Sift scans: status | set INTERVAL | off | install")
    p.add_argument("action", choices=["status", "set", "off", "install"])
    p.add_argument("value", nargs="?", help="off, 15m, 30m, 1h, 6h, daily, weekly, or a number with m/h/d")
    p.add_argument("--discover", help="interval for tag discovery (default weekly, or off)")
    p.add_argument("--no-systemctl", action="store_true"); p.add_argument("--json", action="store_true")
    p.set_defaults(run=run)
