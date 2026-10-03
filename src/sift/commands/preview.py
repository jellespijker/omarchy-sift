"""Preview a file: `sift open PATH`, and choose how with `sift preview show|set|reset|float`.

The default application comes from the desktop (xdg-open). Because the desktop default is sometimes a surprise (Markdown may open a
web app), each file type can be given its own application, and the window can be floated and centred under Hyprland so it does not
disturb the tiled layout. Only regular files inside the scanned folders are ever opened.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Mapping, Sequence

from .. import config as cfgmod
from ..adapters.stores import StoreError
from ..i18n import t

DEFAULTS = {"float": True, "size": [0.7, 0.8], "wait": 6.0, "follow": 25.0, "apps": {}}


def settings(cfg: Mapping) -> dict:
    return {**DEFAULTS, **(cfg.get("preview") or {})}


def command_for(cfg: Mapping, path: Path) -> list[str]:
    """argv that opens the file: a per-type override, else the desktop default (xdg-open)."""
    ext = path.suffix.lower().lstrip(".")
    app = settings(cfg)["apps"].get(ext) or settings(cfg)["apps"].get("*")
    if not app or app == "default":
        return ["xdg-open", str(path)]
    if isinstance(app, list):                                   # explicit argv; "{path}" is replaced, never passed through a shell
        return [str(path) if x == "{path}" else str(x) for x in app]
    if shutil.which("gtk-launch"):
        return ["gtk-launch", str(app).removesuffix(".desktop"), str(path)]
    return ["xdg-open", str(path)]


def plan_float(before: Sequence[Mapping], after: Sequence[Mapping], monitors: Sequence[Mapping], size: Sequence[float]) -> list[dict]:
    """Steps that float, size and centre the window that appeared between two `clients` snapshots. Pure.
    Each step has the Lua form used by current Hyprland (0.5x+) and the classic form used by older releases."""
    seen = {c["address"] for c in before}
    new = [c for c in after if c["address"] not in seen and c.get("mapped", True)]
    if not new:
        return []
    c = new[-1]
    mon = next((m for m in monitors if m.get("id") == c.get("monitor")), monitors[0] if monitors else None)
    addr = c["address"]
    sel = f'window = "address:{addr}"'
    steps: list[dict] = []
    if not c.get("floating"):
        steps.append({"lua": f'hl.dsp.window.float({{ action = "enable", {sel} }})', "classic": ["setfloating", f"address:{addr}"]})
    if mon:
        scale = mon.get("scale") or 1
        w, h = int(mon["width"] / scale * size[0]), int(mon["height"] / scale * size[1])
        steps.append({"lua": f"hl.dsp.window.resize({{ x = {w}, y = {h}, {sel} }})", "classic": ["resizewindowpixel", f"exact {w} {h},address:{addr}"]})
    steps.append({"lua": f"hl.dsp.window.center({{ {sel} }})", "classic": ["focuswindow", f"address:{addr}"]})
    return steps


def _dispatch(step: Mapping) -> None:
    """Run a step with the Lua syntax first; an older Hyprland rejects it, so fall back to the classic dispatcher."""
    r = subprocess.run(["hyprctl", "dispatch", step["lua"]], capture_output=True, text=True, timeout=3)
    if r.returncode != 0 or "error" in (r.stdout + r.stderr).lower():
        subprocess.run(["hyprctl", "dispatch", *step["classic"]], capture_output=True, timeout=3)
        if step["classic"][0] == "focuswindow":
            subprocess.run(["hyprctl", "dispatch", "centerwindow"], capture_output=True, timeout=3)


def _hypr(*args: str) -> list | dict | None:
    try:
        r = subprocess.run(["hyprctl", *args, "-j"], capture_output=True, text=True, timeout=3)
        return json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip() else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def float_new(known: set, st: Mapping, seconds: float, stop_after_first: bool = False, poll: float = 0.25,
              sleep=time.sleep, clock=time.monotonic) -> set:
    """Float every window that appears and was not in `known`, for up to `seconds`. Apps often open a splash screen first and the
    real window later, so one window is not enough. Returns the set of addresses that are now known."""
    known = set(known)
    deadline = clock() + seconds
    while clock() < deadline:
        sleep(poll)
        after = _hypr("clients") or []
        steps = plan_float([{"address": a} for a in known], after, _hypr("monitors") or [], st["size"])
        if steps:
            for step in steps:
                _dispatch(step)
            known |= {c["address"] for c in after if c["address"] not in known and c.get("mapped", True)}
            if stop_after_first:
                break
    return known


def launch(cfg: Mapping, path: str) -> str:
    """Open the file. Returns as soon as the first window has been handled, so a caller's spinner is short; a background follower keeps
    floating later windows (the real document window that follows a splash screen)."""
    p = Path(path).expanduser()
    if p.is_symlink() or not p.is_file():
        raise StoreError(t("cli.store.notRegular"))
    real = p.resolve()
    if not any(real == r or r in real.parents for r in cfgmod.dirs(dict(cfg))):
        raise StoreError(t("cli.store.outsideRoots"))
    st = settings(cfg)
    can_float = bool(st["float"]) and shutil.which("hyprctl") is not None
    before = _hypr("clients") if can_float else None
    subprocess.Popen(command_for(cfg, real), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    if can_float and before is not None:
        known = float_new({c["address"] for c in before}, st, float(st["wait"]), stop_after_first=True)
        subprocess.Popen([str(cfgmod.ROOT / "bin" / "sift"), "preview", "follow", ",".join(sorted(known)), str(st["follow"])],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    return f"opened {real.name}"


def _default_app(ext: str) -> str:
    try:
        mime = subprocess.run(["xdg-mime", "query", "filetype", f"x.{ext}"], capture_output=True, text=True, timeout=5).stdout.strip()
        app = subprocess.run(["xdg-mime", "query", "default", mime], capture_output=True, text=True, timeout=5).stdout.strip()
        return f"{app or 'none'} ({mime})"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def run(a, cfg) -> int:
    try:
        if a.action == "open":
            print(launch(cfg, a.path))
            return 0
        st = settings(cfg)
        if a.action == "show":
            exts = a.value.split(",") if a.value else ["md", "txt", "pdf", "docx", "xlsx", "csv", "json", "py", "png"]
            for e in exts:
                over = st["apps"].get(e)
                print(f".{e:<6} {'override: ' + json.dumps(over) if over else _default_app(e)}")
            print(f"floating window: {'on' if st['float'] else 'off'}, size {int(st['size'][0] * 100)}% x {int(st['size'][1] * 100)}%")
            return 0
        if a.action in ("set", "reset"):
            ext = (a.value or "").lower().lstrip(".")
            if not ext or (a.action == "set" and not a.app):
                print("usage: sift preview set EXT APP   (APP: a desktop file id such as code, or `default`)", file=sys.stderr)
                return 2
            apps = dict(st["apps"])
            if a.action == "set":
                apps[ext] = a.app
            else:
                apps.pop(ext, None)
            cfgmod.save_user({"preview": {**(cfgmod.user_value("preview", {}) or {}), "apps": apps}})
            print(f".{ext} -> {apps.get(ext, 'desktop default')}")
            return 0
        if a.action == "follow":                      # internal: keeps floating windows that open after the first one
            float_new(set((a.value or "").split(",")), st, float(a.app or st["follow"]))
            return 0
        if a.action == "float":
            on = (a.value or "").lower() in ("on", "true", "1", "yes")
            cfgmod.save_user({"preview": {**(cfgmod.user_value("preview", {}) or {}), "float": on}})
            print(f"floating preview: {'on' if on else 'off'}")
            return 0
    except (StoreError, cfgmod.ConfigError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 2


def register(sub) -> None:
    po = sub.add_parser("open", help="preview a file (floating window, default or chosen application)")
    po.add_argument("path")
    po.set_defaults(run=lambda a, cfg: run(_a(a, "open"), cfg))
    p = sub.add_parser("preview", help="how previews open: show [EXTS] | set EXT APP | reset EXT | float on|off")
    p.add_argument("action", choices=["show", "set", "reset", "float", "follow"]); p.add_argument("value", nargs="?"); p.add_argument("app", nargs="?")
    p.set_defaults(run=run)


def _a(a, action):
    a.action = action
    return a
