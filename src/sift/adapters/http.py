"""Small JSON-over-HTTP helper shared by the backend adapters: auth header, retries on transient failures, bounded error text."""
from __future__ import annotations

import http.client
import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

from .. import netpolicy


class BackendError(RuntimeError):
    pass


class _PinnedMixin:
    """Connect to an address the network policy approved, from the single lookup it made, never to a name the OS resolves again."""

    def _pinned_socket(self):
        last: Exception | None = None
        for _fam, addr in netpolicy.resolve(self.host, self.port):
            try:
                return socket.create_connection((addr, self.port), self.timeout, self.source_address)
            except OSError as e:
                last = e
        raise OSError(f"cannot connect to {self.host}: {last}")            # an OSError: the callers retry transient failures


class _HTTPConn(_PinnedMixin, http.client.HTTPConnection):
    def connect(self):
        self.sock = self._pinned_socket()


class _HTTPSConn(_PinnedMixin, http.client.HTTPSConnection):
    def connect(self):
        self.sock = self._context.wrap_socket(self._pinned_socket(), server_hostname=self.host)


class _HTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_HTTPConn, req)


class _HTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_HTTPSConn, req, context=self._context)


class _SameHostRedirects(urllib.request.HTTPRedirectHandler):
    """Follow redirects only within the same host and never from https to http, so an Authorization header cannot follow a redirect to
    another server."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old, new = urllib.parse.urlparse(req.full_url), urllib.parse.urlparse(newurl)
        if old.hostname != new.hostname or (old.scheme == "https" and new.scheme != "https"):
            raise BackendError(f"the endpoint redirected to another host ({new.hostname}); refusing to send credentials there")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# No proxy handler: a proxy would receive the request instead of the destination the policy approved, so Sift connects directly.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _HTTPHandler, _HTTPSHandler, _SameHostRedirects)


def open_request(req: urllib.request.Request, timeout: float):
    """The one way adapters open a URL: pinned to the approved address, same-host redirects only."""
    try:
        return _OPENER.open(req, timeout=timeout)
    except netpolicy.NetworkRefused as e:
        raise BackendError(str(e)) from e
    except urllib.error.URLError as e:
        if isinstance(e.reason, netpolicy.NetworkRefused):
            raise BackendError(str(e.reason)) from e
        raise


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
            with open_request(req, timeout) as r:
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
        with open_request(req, timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise BackendError(f"HTTP {e.code}") from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise BackendError(f"backend unreachable: {_detail(str(e).encode())}") from e
