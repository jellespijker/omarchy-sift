"""API keys in the desktop keyring: `sift secret set|clear|status [BACKEND]`.

Keys are stored with libsecret's `secret-tool` (GNOME Keyring, KWallet's secret service, KeePassXC...), under the attributes
`service=sift backend=NAME`. The key is sent to secret-tool on standard input, never on a command line, and is never printed."""
from __future__ import annotations

import getpass
import shutil
import subprocess
import sys

from .. import config as cfgmod


def available() -> bool:
    return shutil.which("secret-tool") is not None


def store(name: str, key: str) -> None:
    if not available():
        raise cfgmod.ConfigError("secret-tool is not installed (package libsecret); use a private key file instead")
    key = key.strip()
    if not key:
        raise cfgmod.ConfigError("the key is empty")
    try:
        subprocess.run(["secret-tool", "store", "--label", f"Sift API key ({name})", "service", "sift", "backend", name],
                       input=key, text=True, capture_output=True, timeout=60, check=True)
    except subprocess.TimeoutExpired as e:
        raise cfgmod.ConfigError("the keyring did not answer (is it locked? unlock it and retry)") from e
    except (OSError, subprocess.CalledProcessError) as e:
        raise cfgmod.ConfigError("could not store the key: is a keyring service running and unlocked?") from e


def clear(name: str) -> None:
    subprocess.run(["secret-tool", "clear", "service", "sift", "backend", name], capture_output=True, timeout=30)


def has_key(name: str) -> bool:
    try:
        r = subprocess.run(cfgmod.keyring_lookup_cmd(name), capture_output=True, text=True, timeout=10)
        return r.returncode == 0 and bool(r.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        return False


def attach(name: str) -> None:
    """Point a backend at its keyring entry (and drop any other key source)."""
    backends = {k: dict(v) for k, v in (cfgmod.user_value("backends", {}) or {}).items()}
    b = backends.setdefault(name, {})
    for k in ("api_key_env", "api_key_file", "api_key_cmd", "api_key"):
        b.pop(k, None)
    b["api_key_keyring"] = name
    cfgmod.save_user({"backends": backends})


def run(a, cfg) -> int:
    name = a.name or "default"
    try:
        if a.action == "status":
            table = cfgmod.backends(cfg)
            for n, b in table.items():
                src = cfgmod.key_source(b)
                state = ""
                if src == "keyring":
                    state = " (stored)" if has_key(b["api_key_keyring"]) else " (NOT FOUND: run `sift secret set " + n + "`)"
                print(f"{n:<12} key: {src}{state}")
            print(f"secret-tool: {'found' if available() else 'missing (install libsecret)'}")
            return 0
        if a.action == "clear":
            clear(name)
            print(f"removed the stored key for {name}")
            return 0
        key = sys.stdin.read() if a.stdin or not sys.stdin.isatty() else getpass.getpass(f"API key for {name} (typing is hidden): ")
        store(name, key)
        attach(name)
        print(f"stored in the keyring and linked to backend {name}")
        return 0
    except cfgmod.ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


def register(sub) -> None:
    p = sub.add_parser("secret", help="API keys in the desktop keyring: set | clear | status [BACKEND]")
    p.add_argument("action", choices=["set", "clear", "status"]); p.add_argument("name", nargs="?")
    p.add_argument("--stdin", action="store_true", help="read the key from standard input")
    p.set_defaults(run=run)
