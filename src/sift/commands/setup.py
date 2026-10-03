"""First-run setup and config editing: `sift setup`, `sift config show|set`.

`build_backend` turns a preset plus the user's answers into a backend entry; it is pure so it can be tested and scripted
(`sift setup --preset typesafe --api-key-env TYPESAFE_API_KEY --consent`). API keys are never written to the config, only the
name of the environment variable, a file or a command that supplies them.
"""
from __future__ import annotations

import getpass
import json
import sys
from typing import Any

from .. import config as cfgmod
from ..adapters.http import BackendError, safe_url
from ..validate import ValidationError, valid_count, valid_unit_interval

PRESETS: dict[str, dict[str, Any]] = {
    "open-jev": {"label": "Open-Jev server (this machine or your network)", "type": "jev", "endpoint": "http://127.0.0.1:8791", "ask": ["endpoint"]},
    "typesafe": {"label": "TypeSafe hosted Jev (api.typesafe.ai)", "type": "jev", "endpoint": "https://api.typesafe.ai", "model": "jev-latest",
                 "remote": True, "ask": ["api_key"]},
    "gateway": {"label": "Jev through a gateway (LiteLLM, OpenRouter, ...)", "type": "jev", "remote": True,
                "ask": ["endpoint", "path", "model", "api_key"]},
    "ollama": {"label": "Ollama chat model on this machine", "type": "chat", "endpoint": "http://127.0.0.1:11434/v1", "ask": ["endpoint", "model"]},
    "openai": {"label": "Any OpenAI-compatible chat endpoint (OpenAI, OpenRouter, vLLM, ...)", "type": "chat", "remote": True,
               "ask": ["endpoint", "model", "api_key"]},
}
CHAT_THRESHOLDS = {"act": 0.9, "review": 0.6}


def build_backend(preset: str, answers: dict[str, Any]) -> dict[str, Any]:
    if preset not in PRESETS:
        raise cfgmod.ConfigError(f"unknown preset {preset!r}; choose from {', '.join(PRESETS)}")
    spec = PRESETS[preset]
    b: dict[str, Any] = {"type": spec["type"], "endpoint": answers.get("endpoint") or spec.get("endpoint", "")}
    for key in ("model", "path", "api_key_env", "api_key_file", "api_key_keyring", "auth_header", "auth_scheme"):
        val = answers.get(key) or spec.get(key)
        if val:
            b[key] = val
    if answers.get("consent"):
        b["consent"] = True
    if spec["type"] == "chat" and answers.get("trust"):       # explicit opt-in: tag automatically with an uncalibrated model
        b.update(CHAT_THRESHOLDS)
    if not b["endpoint"]:
        raise cfgmod.ConfigError("an endpoint URL is required")
    return b


def store_key(name: str, key: str) -> str:
    """Save an API key in a private file next to the config (mode 0600, never inside the JSON). Returns the path."""
    d = cfgmod.config_dir() / "keys"
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    p = d / f"{name}.key"
    from .. import state
    state.write_atomic(p, key.strip() + "\n")
    return str(p)


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        v = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        v = ""
    return v or default


def _yes(prompt: str, default: bool = False) -> bool:
    return _ask(prompt + (" (Y/n)" if default else " (y/N)"), "y" if default else "n").lower().startswith("y")


def run_setup(a, cfg) -> int:
    interactive = sys.stdin.isatty() and not a.yes
    preset = a.preset
    if not preset:
        if not interactive:
            print("error: --preset is required when not running in a terminal", file=sys.stderr)
            return 2
        names = list(PRESETS)
        print("Which service classifies your files?")
        for i, n in enumerate(names, 1):
            print(f"  {i}. {PRESETS[n]['label']}")
        pick = _ask("Choose a number", "1")
        if not pick.isdigit() or not 1 <= int(pick) <= len(names):
            print("error: not a valid choice", file=sys.stderr)
            return 2
        preset = names[int(pick) - 1]
    spec = PRESETS[preset]
    answers: dict[str, Any] = {k: v for k, v in {"endpoint": a.endpoint, "model": a.model, "path": a.path, "api_key_env": a.api_key_env,
                                                  "api_key_file": a.api_key_file, "auth_header": a.auth_header, "auth_scheme": a.auth_scheme,
                                                  "consent": a.consent, "trust": a.trust}.items() if v}
    if a.api_key_keyring:                                   # the key arrives on stdin, never as an argument
        from . import secret
        secret.store("default", sys.stdin.read())
        answers["api_key_keyring"] = "default"
    if interactive:
        for key in spec["ask"]:
            if key == "api_key":
                if not (answers.get("api_key_env") or answers.get("api_key_file") or answers.get("api_key_keyring")):
                    from . import secret
                    kr = secret.available()
                    print("\nWhere should the API key be kept? Scheduled scans run under systemd, which does not see your shell's variables.")
                    opts = (["1 = the desktop keyring (recommended)", "2 = a private file", "3 = an environment variable"] if kr
                            else ["2 = a private file", "3 = an environment variable"])
                    how = _ask("; ".join(opts) + " ", "1" if kr else "2")
                    if how == "3":
                        answers["api_key_env"] = _ask("Environment variable name", "TYPESAFE_API_KEY" if preset == "typesafe" else "")
                    elif how == "1" and kr:
                        secret.store("default", getpass.getpass("API key (typing is hidden): "))
                        answers["api_key_keyring"] = "default"
                    else:
                        answers["api_key_file"] = store_key("default", getpass.getpass("API key (typing is hidden): "))
            elif key not in answers:
                hint = {"path": "URL path of the decision endpoint", "endpoint": "Endpoint URL", "model": "Model name"}[key]
                answers[key] = _ask(hint, spec.get(key, ""))
        if spec.get("remote") and not answers.get("consent"):
            print("\nSift will send the text of your files (about the first 2,000 characters of each, in several pieces) to this service.")
            print("Files that look like they contain secrets are never sent.")
            answers["consent"] = _yes("Allow that?")
            if not answers["consent"]:
                print("Nothing was saved. Choose a local service (Open-Jev or Ollama) to keep file contents on this machine.")
                return 1
        if spec["type"] == "chat" and not answers.get("trust"):
            print("\nA chat model does not give calibrated confidence, so by default Sift only suggests tags for you to accept.")
            answers["trust"] = _yes("Let Sift tag files automatically anyway (high confidence only)?")
    backend = build_backend(preset, answers)
    update: dict[str, Any] = {"backends": {**(cfgmod.user_value("backends", {}) or {}), "default": backend}}      # keep the user's other backends
    if a.dir:
        update["dirs"] = a.dir
    elif interactive:
        d = _ask("Folders to scan, separated by commas", "~/Downloads, ~/Documents")
        update["dirs"] = [x.strip() for x in d.split(",") if x.strip()]
    try:
        merged = {**cfg, **update}
        from ..api import make_classifier
        clf = make_classifier("default", backend, merged)
        h = clf.health()
        print(f"connection ok: {h.get('status')} {h.get('model', '')} {('(' + h['note'] + ')') if h.get('note') else ''}".strip())
    except (cfgmod.ConfigError, BackendError) as e:
        print(f"warning: could not reach the service yet: {e}", file=sys.stderr)
        if not a.force:
            print("Settings were not saved. Fix the endpoint or run again with --force to save anyway.", file=sys.stderr)
            return 1
    path = cfgmod.save_user(update)
    print(f"saved {path}")
    if a.interval:
        from . import schedule
        schedule.install(a.interval, "weekly", systemctl=not a.no_systemctl)
        print(f"scanning every {a.interval}")
    print("Next: `sift doctor` to check everything, `sift index --dry-run` to see how many files are eligible.")
    return 0


def _redact(cfg: dict[str, Any]) -> dict[str, Any]:
    out = json.loads(json.dumps(cfg))
    for b in [*(out.get("backends") or {}).values(), out.get("proposer") or {}, out.get("tuner") or {}]:
        for k in ("api_key", "api_key_cmd"):
            if k in b:
                b[k] = "<hidden>"
        if isinstance(b.get("endpoint"), str):
            b["endpoint"] = safe_url(b["endpoint"])
    return out


SETTABLE = {"write_xattrs": bool, "threshold_act": float, "threshold_review": float, "index_budget": int, "index_max_seconds": int,
            "allow_insecure": bool, "language": str}


def run_config(a, cfg) -> int:
    if a.action == "show":
        print(json.dumps(_redact(cfg), indent=2, sort_keys=True))
        print(f"\nuser config: {cfgmod.user_config_path()}", file=sys.stderr)
        return 0
    if a.action == "path":
        print(cfgmod.user_config_path())
        return 0
    if a.key not in SETTABLE:
        print(f"error: {a.key!r} cannot be set here; settable keys: {', '.join(SETTABLE)} (use `sift dirs`, `sift schedule`, `sift tags`, `sift setup` for the rest)", file=sys.stderr)
        return 2
    kind = SETTABLE[a.key]
    try:
        val = (a.value.lower() in ("1", "true", "yes", "on")) if kind is bool else kind(a.value)
        if a.key.startswith("threshold_"):
            val = valid_unit_interval(val, a.key)
        elif a.key == "index_budget":
            val = valid_count(val, 1, 100000, a.key)
        elif a.key == "index_max_seconds":
            val = valid_count(val, 60, 86400, a.key)
        elif a.key == "language":
            from .. import i18n
            val = val.lower()
            if val != "auto" and val not in i18n.SUPPORTED:
                raise ValidationError(f"language must be auto or one of {', '.join(i18n.SUPPORTED)}")
    except (TypeError, ValueError, AttributeError) as e:
        print(f"error: {a.key} expects a {kind.__name__}" + (f" ({e})" if isinstance(e, ValidationError) else ""), file=sys.stderr)
        return 2
    cfgmod.save_user({a.key: val})
    print(f"{a.key} = {val}")
    return 0


def register(sub) -> None:
    p = sub.add_parser("setup", help="choose the classification service, folders and scan frequency")
    p.add_argument("--preset", choices=list(PRESETS)); p.add_argument("--endpoint"); p.add_argument("--model"); p.add_argument("--path")
    p.add_argument("--api-key-env", dest="api_key_env", help="name of the environment variable that holds the API key")
    p.add_argument("--api-key-file", dest="api_key_file", help="path of a file that holds the API key (mode 0600 recommended)")
    p.add_argument("--api-key-keyring", dest="api_key_keyring", action="store_true", help="read the API key from stdin and store it in the keyring")
    p.add_argument("--auth-header", dest="auth_header", help="header that carries the key (default Authorization)")
    p.add_argument("--auth-scheme", dest="auth_scheme", help="prefix before the key (default Bearer; empty for a bare key)")
    p.add_argument("--consent", action="store_true", help="allow sending file text to a remote service")
    p.add_argument("--trust", action="store_true", help="chat models: allow automatic tagging at high confidence")
    p.add_argument("--dir", action="append"); p.add_argument("--interval"); p.add_argument("--yes", action="store_true", help="never prompt")
    p.add_argument("--force", action="store_true"); p.add_argument("--no-systemctl", action="store_true")
    p.set_defaults(run=run_setup)
    c = sub.add_parser("config", help="show or change settings: show | path | set KEY VALUE")
    c.add_argument("action", choices=["show", "path", "set"]); c.add_argument("key", nargs="?"); c.add_argument("value", nargs="?")
    c.set_defaults(run=run_config)
