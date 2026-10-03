"""Small JSON-over-HTTP helper shared by the backend adapters: auth header, retries on transient failures, bounded error text."""
from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.parse
import urllib.request


class BackendError(RuntimeError):
    pass


class _SameHostRedirects(urllib.request.HTTPRedirectHandler):
    """Follow redirects only within the same host and never from https to http, so an Authorization header cannot follow a redirect to
    another server."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old, new = urllib.parse.urlparse(req.full_url), urllib.parse.urlparse(newurl)
        if old.hostname != new.hostname or (old.scheme == "https" and new.scheme != "https"):
            raise BackendError(f"the endpoint redirected to another host ({new.hostname}); refusing to send credentials there")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_SameHostRedirects)


def safe_url(url: str) -> str:
    """The endpoint without credentials or query string, for messages and logs."""
    u = urllib.parse.urlparse(url)
    host = u.hostname or ""
    if u.port:
        host += f":{u.port}"
    return urllib.parse.urlunparse((u.scheme, host, u.path, "", "", ""))


def _detail(raw: bytes) -> str:
    """A short, single-line, printable piece of a server's error text."""
    return "".join(c for c in raw.decode(errors="replace") if c.isprintable())[:120]


def post_json(url: str, body: dict, headers: dict[str, str] | None = None, timeout: float = 30.0, retries: int = 2,
              backoff: float = 1.0) -> dict:
    req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json", **(headers or {})})
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with _OPENER.open(req, timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            detail = _detail(e.read(200))
            err = BackendError(f"HTTP {e.code}: {detail}")
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:     # rate limit or transient server trouble
                last = err
                time.sleep(backoff * (attempt + 1) * 3)
                continue
            if e.code in (401, 403):
                raise BackendError(f"HTTP {e.code}: the endpoint rejected the credentials") from e
            raise err from e
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException, ValueError) as e:
            last = e
            time.sleep(backoff * (attempt + 1))
    raise BackendError(f"backend unreachable: {last}") from last


def get_json(url: str, headers: dict[str, str] | None = None, timeout: float = 10.0) -> dict:
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise BackendError(f"HTTP {e.code}") from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise BackendError(f"backend unreachable: {_detail(str(e).encode())}") from e
