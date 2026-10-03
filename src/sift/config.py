"""Configuration: layered, per user, with secrets kept out of the JSON.

Precedence (later wins): packaged defaults < legacy `config.json` next to the plugin < user file
(`$SIFT_CONFIG_DIR` or `$XDG_CONFIG_HOME/sift/config.json`) < environment.
"""
from __future__ import annotations

import ipaddress
import json
import os
import socket
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]


class ConfigError(ValueError):
    pass


def config_dir(env: dict[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    from . import demo
    if demo.active(env):                       # demo mode never reads or writes the real configuration
        return demo.paths(env)["config"]
    return Path(env.get("SIFT_CONFIG_DIR") or Path(env.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "sift")


def user_config_path(env: dict[str, str] | None = None) -> Path:
    return config_dir(env) / "config.json"


def _read(p: Path) -> dict[str, Any]:
    try:
        return json.loads(p.read_text()) if p.is_file() else {}
    except ValueError as e:
        raise ConfigError(f"{p}: not valid JSON ({e})") from e


CONFIG_VERSION = 1
LEGACY_KEYS = {"index_dirs": "dirs", "watch_dirs": "dirs", "exclude_dirs": "ignore"}       # older names, migrated per file when read


def _migrate(layer: dict[str, Any]) -> dict[str, Any]:
    """Rename legacy keys inside one config file, so a newer packaged default can never shadow an older user setting."""
    out = dict(layer)
    for old, new in LEGACY_KEYS.items():
        if old in out:
            val = out.pop(old)
            out.setdefault(new, val)
    return out


def _number(cfg: dict[str, Any], key: str, lo: float, hi: float, whole: bool = False) -> None:
    if key not in cfg:
        return
    v = cfg[key]
    ok = isinstance(v, (int, float)) and not isinstance(v, bool) and lo <= v <= hi and (not whole or float(v).is_integer())
    if not ok:
        raise ConfigError(f"setting {key!r} must be {'a whole ' if whole else 'a '}number between {lo:g} and {hi:g}, not {v!r}; fix it with `sift config set {key} VALUE`")
    cfg[key] = int(v) if whole else float(v)


def validate(cfg: dict[str, Any]) -> dict[str, Any]:
    """Check the settings a hand edit can break, with a message that says which one and how to fix it, never a traceback."""
    for key, lo, hi in (("index_budget", 1, 100000), ("index_max_seconds", 10, 86400)):
        _number(cfg, key, lo, hi, whole=True)
    for key, lo, hi in (("watch_interval", 1, 86400), ("threshold_act", 0, 1), ("threshold_review", 0, 1)):
        _number(cfg, key, lo, hi)
    for key in ("dirs", "ignore", "trusted_networks"):
        if key in cfg and not (isinstance(cfg[key], list) and all(isinstance(x, str) for x in cfg[key])):
            raise ConfigError(f"setting {key!r} must be a list of text values")
    for net in cfg.get("trusted_networks", []):
        try:
            ipaddress.ip_network(net, strict=False)
        except ValueError as e:
            raise ConfigError(f"trusted_networks: {net!r} is not an address range like 192.168.1.0/24") from e
    if "backends" in cfg and not isinstance(cfg["backends"], dict):
        raise ConfigError("setting 'backends' must be an object")
    return cfg


def load(root: Path = ROOT, env: dict[str, str] | None = None) -> dict[str, Any]:
    env = os.environ if env is None else env
    cfg: dict[str, Any] = {}
    from . import demo
    layers = (root / "config.json.example", user_config_path(env)) if demo.active(env) else \
        (root / "config.json.example", root / "config.json", user_config_path(env))
    for p in layers:
        cfg.update(_migrate(_read(p)))
    if env.get("SIFT_ENDPOINT"):                      # one-off override of the default backend
        cfg["endpoint"] = env["SIFT_ENDPOINT"]
        if "default" in (cfg.get("backends") or {}):
            cfg["backends"] = {**cfg["backends"], "default": {**cfg["backends"]["default"], "endpoint": env["SIFT_ENDPOINT"]}}
    return validate(cfg)


def save_user(update: dict[str, Any], env: dict[str, str] | None = None) -> Path:
    """Merge top-level keys into the user config (mode 0600). The read-modify-write happens under a lock and the file is replaced
    atomically, so two Sift processes (panel, timer, terminal) cannot lose each other's change or leave a half-written file."""
    from . import state
    p = user_config_path(env)
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with state.lock(p.with_name(p.name + ".flock")):
        cur = _read(p)
        cur.update(update)
        cur.setdefault("config_version", CONFIG_VERSION)
        state.write_atomic(p, json.dumps(cur, indent=2, sort_keys=True) + "\n")
    return p


def user_value(key: str, default: Any = None, env: dict[str, str] | None = None) -> Any:
    return _read(user_config_path(env)).get(key, default)


# ---- backends -------------------------------------------------------------------------------------------------

BACKEND_TYPES = {"jev", "chat", "demo"}


def backends(cfg: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Normalised backend table. A bare `endpoint` key (older configs) becomes a Jev backend named `default`."""
    table = {k: dict(v) for k, v in (cfg.get("backends") or {}).items()}
    if "default" not in table and cfg.get("endpoint"):
        table["default"] = {"type": "jev", "endpoint": cfg["endpoint"]}
    for name, b in table.items():
        b.setdefault("type", "jev")
        if b["type"] not in BACKEND_TYPES:
            raise ConfigError(f"backend {name!r}: unknown type {b['type']!r} (use one of {sorted(BACKEND_TYPES)})")
        if not b.get("endpoint"):
            raise ConfigError(f"backend {name!r}: endpoint is missing")
    return table


def keyring_lookup_cmd(name: str) -> list[str]:
    return ["secret-tool", "lookup", "service", "sift", "backend", name]


def key_code(b: dict[str, Any]) -> str:
    """Machine-readable key source: keyring, file, env, command or none (the panel translates it)."""
    for k, code in (("api_key_keyring", "keyring"), ("api_key_file", "file"), ("api_key_env", "env"), ("api_key_cmd", "command")):
        if b.get(k):
            return code
    return "none"


def key_source(b: dict[str, Any]) -> str:
    """Where a backend's API key comes from, for display: keyring, file, env, command or none."""
    for k, label in (("api_key_keyring", "keyring"), ("api_key_file", "file"), ("api_key_env", "environment variable"), ("api_key_cmd", "command")):
        if b.get(k):
            return label
    return "none"


def resolve_secret(b: dict[str, Any]) -> str | None:
    """API key from `api_key_env` (env var name), `api_key_file` (path) or `api_key_cmd` (argv list, e.g. secret-tool).
    Keys are never stored in the JSON and never logged."""
    if b.get("api_key_env"):
        return os.environ.get(b["api_key_env"]) or None
    if b.get("api_key_file"):
        p = Path(b["api_key_file"]).expanduser()
        try:
            return p.read_text().strip() or None
        except OSError as e:
            raise ConfigError(f"cannot read api_key_file {p}: {e.strerror}") from e
    if b.get("api_key_keyring"):               # the desktop keyring through libsecret's secret-tool
        cmd = keyring_lookup_cmd(str(b["api_key_keyring"]))
    elif b.get("api_key_cmd"):
        cmd = b["api_key_cmd"]
    else:
        cmd = None
    if cmd is not None:
        if not isinstance(cmd, list) or not all(isinstance(x, str) for x in cmd):
            raise ConfigError("api_key_cmd must be a list of strings (no shell)")
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=10, check=True)
        except subprocess.TimeoutExpired as e:
            raise ConfigError("the key command timed out (is the keyring locked? unlock it and retry)") from e
        except (OSError, subprocess.SubprocessError) as e:
            what = "no key is stored in the keyring for this backend: run `sift secret set`" if b.get("api_key_keyring") else "api_key_cmd failed"
            raise ConfigError(f"{what} ({type(e).__name__})") from e
        return r.stdout.strip() or None
    return None


# ---- endpoint trust ---------------------------------------------------------------------------------------------

def _addrs(host: str) -> list[ipaddress._BaseAddress]:
    try:
        return [ipaddress.ip_address(host)]
    except ValueError:
        pass
    try:
        return [ipaddress.ip_address(i[4][0]) for i in socket.getaddrinfo(host, None)]
    except OSError:
        return []


def is_local(url: str, trusted_networks: list[str]) -> bool:
    """True when the endpoint is this machine or inside a network the user listed as theirs."""
    u = urlparse(url)
    if not u.hostname:
        return False
    if u.hostname == "localhost":
        return True
    nets = [ipaddress.ip_network(n, strict=False) for n in trusted_networks]
    addrs = _addrs(u.hostname)
    return bool(addrs) and all(a.is_loopback or any(a in n for n in nets) for a in addrs)


def check_endpoint(url: str, trusted_networks: list[str], allow_insecure: bool) -> None:
    """HTTPS always; plain HTTP only for loopback or user-listed networks."""
    u = urlparse(url)
    if u.scheme == "https":
        return
    if u.scheme != "http" or not u.hostname:
        raise ConfigError(f"unsupported endpoint {url!r}")
    if allow_insecure or is_local(url, trusted_networks):
        return
    raise ConfigError(
        f"refusing plain-HTTP endpoint {u.hostname!r}: not loopback and not in trusted_networks "
        "(use https, add its network to trusted_networks, or set allow_insecure)")


def check_backend(name: str, b: dict[str, Any], cfg: dict[str, Any]) -> None:
    """Transport rules plus consent: file text goes only to this machine, a network the user listed, or a remote service
    the user explicitly approved for that backend with `"consent": true`."""
    nets = list(cfg.get("trusted_networks", []))
    check_endpoint(b["endpoint"], nets, bool(cfg.get("allow_insecure")))
    if not is_local(b["endpoint"], nets) and not b.get("consent"):
        host = urlparse(b["endpoint"]).hostname
        raise ConfigError(
            f"backend {name!r} would send file text to {host}, which is not this machine or a trusted network. "
            f'Set "consent": true on that backend (sift setup does this) to allow it.')


# ---- watched and ignored folders ----------------------------------------------------------------------------------

def dirs(cfg: dict[str, Any]) -> list[Path]:
    """Folders Sift scans, tags and watches. Older configs used `index_dirs` / `watch_dirs`."""
    raw = _migrate(cfg).get("dirs", ["~/Downloads"])        # an empty list means "scan nothing", not "use the default"
    return [Path(d).expanduser().resolve() for d in raw]


def ignore(cfg: dict[str, Any]) -> tuple[str, ...]:
    """Name globs (`node_modules`, `*.bak`) or path globs (`~/Documents/private/*`) that are never walked or read."""
    return tuple(_migrate(cfg).get("ignore", []))
