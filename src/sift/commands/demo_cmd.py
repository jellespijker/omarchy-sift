"""`sift demo on|off|status|reset`: try Sift on invented data. Nothing real is read or changed while it is on."""
from __future__ import annotations

import json
import sys

from .. import demo
from ..i18n import t


def run(a, cfg) -> int:
    if a.action == "status":
        on = demo.active()
        print(json.dumps({"demo": on, "files": str(demo.paths()["files"])}) if a.json else f"demo mode: {'on' if on else 'off'}")
        return 0
    if a.action == "off":
        demo.set_marker(False)
        print("demo mode off: back to your real files and settings")
        return 0
    if a.action in ("on", "reset") or not demo.paths()["config"].exists():
        demo.build()
    if a.action == "on":
        demo.set_marker(True)
    elif a.action == "reset":
        print("demo data rebuilt", file=sys.stderr)
    print(f"demo mode on: sample files in {demo.paths()['files']}; nothing real is read or changed. `sift demo off` to leave.")
    return 0


def blocked(a) -> str | None:
    """Commands that would change the real system (systemd timers, the keyring, the setup wizard) are refused in demo mode."""
    cmd = getattr(a, "cmd", "")
    action = getattr(a, "action", "")
    if cmd == "setup" or (cmd == "secret" and action in ("set", "clear")) or (cmd == "schedule" and action in ("set", "off", "install")):
        return t("cli.demo.blocked")
    return None


def register(sub) -> None:
    p = sub.add_parser("demo", help="try Sift on sample data: on | off | status | reset")
    p.add_argument("action", choices=["on", "off", "status", "reset"]); p.add_argument("--json", action="store_true")
    p.set_defaults(run=run)
