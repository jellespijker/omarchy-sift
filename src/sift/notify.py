"""Desktop notifications, kept deliberately rare: a few events, each throttled, never one per file."""
from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Mapping

from . import state

DEFAULTS = {"errors": True, "milestones": True, "ideas": True, "review": False}
COOLDOWN = {"first_pass": 10 * 365 * 86400, "backend_down": 86400, "ideas": 7 * 86400, "review": 7 * 86400}


def enabled(cfg: Mapping[str, Any], kind: str) -> bool:
    return bool({**DEFAULTS, **(cfg.get("notifications") or {})}.get(kind, False))


def _load(sd: Path) -> dict[str, int]:
    data = state.read_json(sd / "notified.json", {})
    return data if isinstance(data, dict) else {}


def due(sd: Path, key: str, now: float | None = None) -> bool:
    last = _load(sd).get(key)
    return last is None or (now or time.time()) - last >= COOLDOWN.get(key, 0)


def mark(sd: Path, key: str, now: float | None = None) -> None:
    data = _load(sd)
    data[key] = int(now or time.time())
    state.write_json(sd / "notified.json", data)


def clear(sd: Path, key: str) -> None:
    data = _load(sd)
    if data.pop(key, None) is not None:
        state.write_json(sd / "notified.json", data)


def send(title: str, body: str, urgency: str = "normal") -> bool:
    exe = shutil.which("notify-send")
    if not exe:
        return False
    try:
        r = subprocess.run([exe, "-a", "Sift", "-u", urgency, "-t", "10000", "--", title, body], capture_output=True, timeout=10)
    except (subprocess.TimeoutExpired, OSError):
        return False
    return r.returncode == 0


def after_index(cfg: Mapping[str, Any], sd: Path, status: Mapping[str, Any], aborted: bool, first_pass_seen: bool) -> list[str]:
    """Decide what, if anything, to tell the user after an index run. Returns the keys that fired (for tests and logs)."""
    fired: list[str] = []
    if aborted:
        if enabled(cfg, "errors") and due(sd, "backend_down"):
            if send("Sift cannot reach its classifier", "Scanning is paused. Run `sift doctor` to see why.", "critical"):
                mark(sd, "backend_down")                 # only a delivered notification starts the cooldown
                fired.append("backend_down")
        return fired
    clear(sd, "backend_down")                        # recovered: the next outage may notify again
    if status.get("remaining") == 0 and first_pass_seen is False and enabled(cfg, "milestones") and due(sd, "first_pass"):
        if send("Sift finished its first full scan", "Open the Sift panel to review what it found."):
            mark(sd, "first_pass")
            fired.append("first_pass")
    return fired


def after_discover(cfg: Mapping[str, Any], sd: Path, good: int) -> list[str]:
    if good >= 3 and enabled(cfg, "ideas") and due(sd, "ideas"):
        if send("Sift found new tag ideas", f"{good} tags look useful. Open the Sift panel to add or dismiss them."):
            mark(sd, "ideas")
            return ["ideas"]
    return []
