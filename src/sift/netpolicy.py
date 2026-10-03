"""Where file text may be sent, decided at connect time on the address actually used.

A hostname can resolve to different addresses from one lookup to the next (DNS rebinding, or just a changing record). Deciding "is this
endpoint local?" from one lookup and then letting the HTTP client resolve the name again would let a destination that looked local receive
file text on a public address without the user's consent. So the decision and the connection use the same single lookup: the adapter asks
`resolve`, which returns only addresses this backend may use, and connects to exactly those."""
from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlparse


class NetworkRefused(OSError):
    pass


@dataclass(frozen=True)
class Policy:
    allow_remote: bool = False                       # the user consented to this backend receiving file text anywhere
    networks: tuple = ()                             # networks the user listed as theirs (ip_network objects)


_REGISTRY: dict[tuple[str, int], Policy] = {}
DEFAULT = Policy()                                    # an unregistered destination may only be this machine


def _key(host: str, port: int) -> tuple[str, int]:
    return host.strip("[]").lower(), int(port)


def register(url: str, allow_remote: bool, trusted_networks: list[str]) -> None:
    u = urlparse(url)
    if not u.hostname:
        return
    port = u.port or (443 if u.scheme == "https" else 80)
    nets = tuple(ipaddress.ip_network(n, strict=False) for n in trusted_networks)
    _REGISTRY[_key(u.hostname, port)] = Policy(bool(allow_remote), nets)


def policy_for(host: str, port: int) -> Policy:
    return _REGISTRY.get(_key(host, port), DEFAULT)


def _normal(ip: ipaddress._BaseAddress) -> ipaddress._BaseAddress:
    v4 = getattr(ip, "ipv4_mapped", None)
    return v4 if v4 is not None else ip              # ::ffff:127.0.0.1 is 127.0.0.1


def permitted(ip: ipaddress._BaseAddress, pol: Policy) -> bool:
    ip = _normal(ip)
    return pol.allow_remote or ip.is_loopback or any(ip in n for n in pol.networks if n.version == ip.version)


def resolve(host: str, port: int) -> list[tuple[int, str]]:
    """The (family, address) pairs this destination may be reached on. Raises NetworkRefused when none is allowed."""
    pol = policy_for(host, port)
    bare = host.strip("[]")
    try:
        literal = ipaddress.ip_address(bare)
        found = [(socket.AF_INET6 if literal.version == 6 else socket.AF_INET, str(literal))]
    except ValueError:
        try:
            found = [(i[0], i[4][0]) for i in socket.getaddrinfo(bare, port, type=socket.SOCK_STREAM)]
        except OSError as e:
            raise NetworkRefused(f"cannot resolve {bare}: {e}") from e
    ok = [(fam, addr) for fam, addr in found if permitted(ipaddress.ip_address(addr.split("%")[0]), pol)]
    if not ok:
        shown = ", ".join(sorted({a for _, a in found})) or "nothing"
        raise NetworkRefused(f"{bare} resolves to {shown}, which is not this machine or a trusted network, and this backend has no consent "
                             "to receive file text there")
    return ok
