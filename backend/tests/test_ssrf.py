"""Unit tests for the SSRF guard (orchestrator/ssrf.py) used by the docs-mcp research loop.

Offline: literal-IP / scheme / allowlist cases need no network; the hostname-resolution paths
monkeypatch ``orchestrator.ssrf._resolve`` so nothing actually hits DNS.
"""

from __future__ import annotations

import ipaddress

import pytest

from orchestrator import ssrf
from orchestrator.ssrf import UrlNotAllowed, assert_url_allowed, looks_like_url, parse_allowlist


def _patch_resolve(monkeypatch, ip: str) -> None:
    monkeypatch.setattr(ssrf, "_resolve", lambda host: [ipaddress.ip_address(ip)])


# ---------- looks_like_url / parse_allowlist ----------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("http://x.com", True),
        ("https://x.com/p", True),
        ("ftp://x.com", True),  # has '://' → must be validated (and will be rejected by scheme)
        ("Claude Code", False),
        ("C#", False),
        ("node.js", False),
    ],
)
def test_looks_like_url(value: str, expected: bool) -> None:
    assert looks_like_url(value) is expected


def test_parse_allowlist() -> None:
    assert parse_allowlist("docs.anthropic.com, Read.io ,") == frozenset(
        {"docs.anthropic.com", "read.io"}
    )
    assert parse_allowlist("") == frozenset()
    assert parse_allowlist("   ") == frozenset()


# ---------- scheme / host validation (no network) ----------


@pytest.mark.parametrize("url", ["ftp://x.com", "file:///etc/passwd", "gopher://x", "://x"])
def test_blocks_non_http_scheme(url: str) -> None:
    with pytest.raises(UrlNotAllowed):
        assert_url_allowed(url)


def test_blocks_missing_host() -> None:
    with pytest.raises(UrlNotAllowed):
        assert_url_allowed("http://")


# ---------- literal-IP hosts (no DNS) ----------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/x",            # loopback
        "http://10.0.0.5/x",             # private
        "http://192.168.1.1",            # private
        "http://172.16.9.9",             # private
        "http://169.254.169.254/latest", # link-local cloud metadata
        "http://100.64.0.1",             # CGNAT
        "http://0.0.0.0",                # unspecified
        "http://[::1]/x",                # IPv6 loopback
        "http://[::ffff:127.0.0.1]/x",   # IPv4-mapped IPv6 loopback
    ],
)
def test_blocks_internal_literal_ips(url: str) -> None:
    with pytest.raises(UrlNotAllowed):
        assert_url_allowed(url)


def test_allows_public_literal_ip() -> None:
    # Public IP literal needs no DNS and must pass.
    assert_url_allowed("https://8.8.8.8/docs")  # no raise


# ---------- hostname resolution path (monkeypatched) ----------


def test_blocks_hostname_resolving_private(monkeypatch) -> None:
    _patch_resolve(monkeypatch, "10.1.2.3")  # internal target behind a public-looking name
    with pytest.raises(UrlNotAllowed):
        assert_url_allowed("https://sneaky.example.com/docs")


def test_allows_hostname_resolving_public(monkeypatch) -> None:
    _patch_resolve(monkeypatch, "93.184.216.34")  # example.com public range
    assert_url_allowed("https://docs.example.com/guide")  # no raise


def test_unresolvable_host_blocked(monkeypatch) -> None:
    def _boom(host: str):
        raise OSError("nxdomain")

    monkeypatch.setattr(ssrf, "_resolve", _boom)
    with pytest.raises(UrlNotAllowed):
        assert_url_allowed("https://does-not-exist.invalid/x")


# ---------- domain allowlist ----------


def test_allowlist_rejects_off_list_host_before_dns() -> None:
    # Off-list host is rejected on the name alone — no resolution attempted.
    with pytest.raises(UrlNotAllowed):
        assert_url_allowed("https://evil.com/x", allowlist=frozenset({"docs.example.com"}))


def test_allowlist_allows_suffix_match(monkeypatch) -> None:
    _patch_resolve(monkeypatch, "93.184.216.34")
    # Exact and subdomain suffix match both pass the allowlist.
    assert_url_allowed("https://docs.example.com/x", allowlist=frozenset({"docs.example.com"}))
    assert_url_allowed("https://api.docs.example.com/x", allowlist=frozenset({"docs.example.com"}))
