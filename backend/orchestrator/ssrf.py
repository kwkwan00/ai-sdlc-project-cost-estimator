"""SSRF guard for outbound URLs the in-process MCP research loop may fetch/scrape.

The docs-mcp research loop (``llm.research_with_local_mcp``) lets the LLM call the
documentation server's ``fetch_url`` / ``scrape_docs`` tools, and the LLM's inputs derive from
untrusted Stage-3 free text. Before any such URL is forwarded to the MCP server, the backend
validates it here so the model can't be steered into fetching internal services, cloud-metadata
endpoints, or non-HTTP schemes.

Pure stdlib (``urllib`` / ``ipaddress`` / ``socket``). NOTE: this blocks the obvious cases
(literal internal IPs, internal hostnames that resolve private, non-http schemes). It cannot by
itself defeat DNS-rebinding (the docs-mcp-server re-resolves at fetch time), so the docs-mcp-server
should ALSO enforce egress rules — this is one defense-in-depth layer, not the only one.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

_ALLOWED_SCHEMES = frozenset({"http", "https"})
# Carrier-grade NAT (RFC 6598) — not covered by ``is_private`` on older Pythons.
_CGNAT = ipaddress.ip_network("100.64.0.0/10")

_IpAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


class UrlNotAllowed(ValueError):
    """Raised when a URL targets a blocked address/scheme/host (fail closed)."""


def looks_like_url(value: str) -> bool:
    """True for a string the model intends as a fetchable URL (so it must be validated)."""
    return "://" in value.strip()[:12]


def _ip_is_blocked(ip: _IpAddress) -> bool:
    """Block loopback / private / link-local (incl. 169.254.169.254 metadata) / reserved /
    multicast / unspecified / CGNAT, unwrapping IPv4-mapped IPv6 (``::ffff:127.0.0.1``)."""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    ):
        return True
    return ip.version == 4 and ip in _CGNAT


def _resolve(host: str) -> list[_IpAddress]:
    infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return [ipaddress.ip_address(info[4][0]) for info in infos]


def assert_url_allowed(url: str, *, allowlist: frozenset[str] | None = None) -> None:
    """Raise ``UrlNotAllowed`` unless ``url`` is an http(s) URL whose host is public (and, when
    ``allowlist`` is given, a suffix-match of one of its domains). All resolved IPs are checked,
    so a hostname that resolves to a private address is blocked."""
    parsed = urlparse(url.strip())
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        raise UrlNotAllowed(f"scheme {parsed.scheme!r} not permitted (http/https only)")
    host = parsed.hostname
    if not host:
        raise UrlNotAllowed("URL has no host")

    if allowlist:
        h = host.lower()
        if not any(h == dom or h.endswith("." + dom) for dom in allowlist):
            raise UrlNotAllowed(f"host {host!r} is not in the docs allowlist")

    # Literal-IP host: check directly. Otherwise resolve and check every address.
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if _ip_is_blocked(literal):
            raise UrlNotAllowed(f"blocked address {host}")
        return

    try:
        ips = _resolve(host)
    except OSError as exc:
        raise UrlNotAllowed(f"cannot resolve host {host!r}") from exc
    if not ips:
        raise UrlNotAllowed(f"no addresses for host {host!r}")
    for ip in ips:
        if _ip_is_blocked(ip):
            raise UrlNotAllowed(f"host {host!r} resolves to blocked address {ip}")


def parse_allowlist(raw: str) -> frozenset[str]:
    """Parse a comma-separated domain allowlist into a lowercased frozenset ({} when empty)."""
    return frozenset(d.strip().lower() for d in raw.split(",") if d.strip())
