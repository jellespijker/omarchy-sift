"""`sift doctor`: check that a setup works and say what to do when it does not."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from .. import config as cfgmod
from ..adapters.http import safe_url, BackendError


from ..adapters.stores import xattr_supported as _xattr_ok


def checks(cfg: dict) -> list[tuple[str, str, str]]:
    """(level, name, detail) with level in ok / warn / fail."""
    out: list[tuple[str, str, str]] = []
    add = lambda lvl, name, detail="": out.append((lvl, name, detail))
    add("ok" if cfgmod.user_config_path().is_file() else "warn", "user config", str(cfgmod.user_config_path()) +
        ("" if cfgmod.user_config_path().is_file() else " (missing: run `sift setup`)"))
    try:
        table = cfgmod.backends(cfg)
    except cfgmod.ConfigError as e:
        add("fail", "backends", str(e))
        table = {}
    if "default" not in table:
        add("fail", "classifier", "no default backend: run `sift setup`")
    for name, b in table.items():
        try:
            from ..api import make_classifier
            clf = make_classifier(name, b, cfg)
            h = clf.health()
            lvl = "ok" if h.get("status") == "ready" else "warn"
            add(lvl, f"backend {name}", f"{b['type']} at {safe_url(b['endpoint'])}: {h.get('status')} {h.get('model', '')} {h.get('note', '')}".strip())
            if not clf.capabilities.calibrated and "act" not in b:
                add("warn", f"backend {name} tagging", "uncalibrated model: Sift will only suggest tags. Add thresholds with `sift setup --trust` to allow automatic tagging")
            if b.get("api_key_keyring"):
                from . import secret
                add("ok" if secret.has_key(b["api_key_keyring"]) else "fail", f"backend {name} key",
                    "stored in the keyring" if secret.has_key(b["api_key_keyring"]) else "not found in the keyring (locked, or never stored): run `sift secret set " + name + "`")
            if b.get("api_key_env"):
                add("warn", f"backend {name} key", f"read from ${b['api_key_env']}, which scheduled scans (systemd) will not see unless you import it; "
                    "use `sift setup --api-key-file` to store the key in a private file instead")
            if b.get("api_key_file"):
                try:
                    if oct(Path(b["api_key_file"]).expanduser().stat().st_mode & 0o077) != "0o0":
                        add("warn", f"backend {name} key file", "is readable by other users: chmod 600 it")
                except OSError:
                    add("fail", f"backend {name} key file", f"{b['api_key_file']} cannot be read: store the key again with `sift secret set {name}`")
        except (cfgmod.ConfigError, BackendError) as e:
            add("fail", f"backend {name}", str(e))
    try:
        from ..api import load_tree_for
        for problem in load_tree_for(cfg).problems:
            add("warn", "vocabulary", problem)
    except Exception as e:                      # a broken tree file must be reported, not crash the doctor
        add("fail", "decision tree", f"{type(e).__name__}: {e}")
    esc = (cfg.get("escalate") or {}).get("backend")
    if esc:
        b = table.get(esc)
        ok = bool(b) and esc != "default" and (b.get("type", "jev") != "chat" or "act" in b)
        add("ok" if ok else "warn", "escalation", f"suggestions are re-judged by {esc}" if ok else f"backend {esc!r} is missing, is the default, or has no thresholds: escalation is off")
    pc = cfg.get("proposer")
    add("ok" if pc else "warn", "tag proposer", f"{pc.get('type', 'ollama')} {pc.get('model', '')}" if pc else "not configured: `sift discover` will not find new tags")
    for d in cfgmod.dirs(cfg):
        if not d.is_dir():
            add("fail", f"folder {d}", "does not exist")
        elif os.path.ismount(d):
            add("warn", f"folder {d}", "is a mount point; Sift never walks network or cloud drives")
        else:
            add("ok" if _xattr_ok(d) else "fail", f"folder {d}", "tags can be stored" if _xattr_ok(d) else "this filesystem does not support extended attributes, so tags cannot be written")
    try:
        from .. import state
        sd = state.state_dir()
        sizes = {f.name: f.stat().st_size for f in sd.glob("*.jsonl")}
        total = sum(sizes.values())
        big = [f"{n} ({sz // 1_000_000} MB)" for n, sz in sizes.items() if sz > 50_000_000]
        add("warn" if big else "ok", "state files", (f"large: {', '.join(big)}; they are compacted after each index run" if big else f"{total // 1000} KB in {len(sizes)} files"))
    except OSError as e:
        add("fail", "state folder", f"cannot read the state folder: {e}")
    add("ok" if cfg.get("write_xattrs") else "warn", "automatic tagging", "on" if cfg.get("write_xattrs") else "off: confident files are reported, not tagged (`sift config set write_xattrs true`)")
    sched = cfg.get("schedule") or {}
    add("ok" if sched.get("index", "off") != "off" else "warn", "scan schedule", f"index {sched.get('index', 'off')}, discovery {sched.get('discover', 'off')}")
    if shutil.which("systemctl") and sched.get("index", "off") != "off":
        r = subprocess.run(["systemctl", "--user", "is-active", "sift-index.timer"], capture_output=True, text=True)
        add("ok" if r.stdout.strip() == "active" else "fail", "systemd timer", r.stdout.strip() or "not installed: `sift schedule install`")
    add("ok" if shutil.which("notify-send") else "warn", "notifications", "notify-send found" if shutil.which("notify-send") else "notify-send missing: no desktop notifications")
    sched_dir = Path(os.environ.get("SIFT_SYSTEMD_DIR") or Path.home() / ".config/systemd/user")
    for unit in ("sift-index.service", "sift-discover.service"):
        f = sched_dir / unit
        if f.is_file():
            import re as _re
            m = _re.search(r'^ExecStart="?([^"\n]+?)"? ', f.read_text(), _re.M)
            if m and not Path(m.group(1).replace("%%", "%")).exists():
                add("fail", f"timer {unit}", f"points at {m.group(1)}, which no longer exists (the plugin moved or was updated): run `sift schedule install`")
    add("ok" if shutil.which("pdftotext") else "warn", "PDF text", "pdftotext found" if shutil.which("pdftotext") else "install poppler to classify PDFs")
    return out


def run(a, cfg) -> int:
    rows = checks(cfg)
    if a.json:
        print(json.dumps([{"level": l, "name": n, "detail": d} for l, n, d in rows]))
    else:
        mark = {"ok": "[ok]  ", "warn": "[warn]", "fail": "[FAIL]"}
        for lvl, name, detail in rows:
            print(f"{mark[lvl]} {name}: {detail}")
    return 1 if any(l == "fail" for l, _, _ in rows) else 0


def register(sub) -> None:
    p = sub.add_parser("doctor", help="check the setup and explain what is wrong")
    p.add_argument("--json", action="store_true")
    p.set_defaults(run=run)
