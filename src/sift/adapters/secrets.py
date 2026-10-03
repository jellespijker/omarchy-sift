"""Deterministic secret detector. Matching files are never sent to a classifier."""
from __future__ import annotations

import re

NAME = re.compile(r"(^|/)(client_secret[^/]*\.json|id_(rsa|dsa|ed25519|ecdsa)(_sk)?|\.env(\..*)?|.*\.(pem|key|ppk|kdbx|kdb|p12|pfx|p8|jks|keystore|gpg|asc)"
                  r"|credentials(\.json)?|\.netrc|_netrc|\.htpasswd|\.pgpass|\.npmrc|\.pypirc|\.git-credentials|kubeconfig|.*\.tfvars|secrets?\.(ya?ml|json|toml|env))$", re.I)
BODY = [re.compile(p) for p in (
    r"-----BEGIN [A-Z ]*PRIVATE KEY(?: BLOCK)?-----", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b", r"\bAIza[0-9A-Za-z_\-]{35}\b", r"\bsk-[A-Za-z0-9_\-]{20,}",
    r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.", r"(?i)\bauthorization\s*:\s*(?:bearer|basic)\s+[A-Za-z0-9._~+/=\-]{12,}",
    r"(?i)\b(?:password|passwd|pwd)\b\s*[:=]\s*['\"]?[^\s'\"<{$]{6,}", r"\bgh[pousr]_[A-Za-z0-9]{30,}", r"\bxox[baprs]-[A-Za-z0-9-]{10,}",
    r"\"client_secret\"\s*:", r"\"private_key\"\s*:", r"(?i)\b(api[_-]?key|secret|passwd|password|token)\b\s*[:=]\s*['\"]?[A-Za-z0-9/+_\-]{16,}")]


def looks_sensitive(name: str, text: str | None) -> bool:
    if NAME.search(name):
        return True
    return bool(text) and any(p.search(text) for p in BODY)
