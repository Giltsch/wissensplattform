"""Pure-function tests for the source connectors (no DB, no network).

Covers:
  * connector_factory dispatch (local / url / unknown),
  * URL normalization (fragment strip, host lowercasing, scheme allow-list),
  * the SSRF reject list (literal IPs + name resolution),
  * the HTML markup stripper (stable text projection for dedup hashing).

Live fetches are out of scope here — they belong behind the -m integration
marker because they need the network.
"""
from __future__ import annotations

import ipaddress

import pytest

from connectors import SourceItem, connector_factory
from connectors.url import (
    FetchError,
    UrlConnector,
    _assert_host_allowed,
    _is_blocked_ip,
    normalize_url,
    strip_markup,
)


# ---- connector_factory dispatch ------------------------------------------

def test_factory_local():
    from connectors.local import LocalFileConnector

    c = connector_factory({"type": "local", "root": "/app/docs"})
    assert isinstance(c, LocalFileConnector)


def test_factory_url():
    c = connector_factory({"type": "url", "urls": ["https://example.com/a"]})
    assert isinstance(c, UrlConnector)


def test_factory_unknown_type_raises():
    with pytest.raises(ValueError):
        connector_factory({"type": "ftp", "urls": []})


def test_factory_missing_type_raises():
    with pytest.raises(ValueError):
        connector_factory({"urls": []})


# ---- URL normalization ----------------------------------------------------

def test_normalize_strips_fragment():
    assert normalize_url("https://example.com/p?q=1#frag") == "https://example.com/p?q=1"


def test_normalize_lowercases_scheme_and_host():
    assert normalize_url("HTTPS://Example.COM/Path") == "https://example.com/Path"


def test_normalize_preserves_port_and_query():
    assert normalize_url("http://Example.com:8080/x?a=B") == "http://example.com:8080/x?a=B"


def test_normalize_rejects_non_http_scheme():
    for bad in ("file:///etc/passwd", "ftp://example.com/x", "gopher://x/"):
        with pytest.raises(ValueError):
            normalize_url(bad)


def test_normalize_rejects_missing_host():
    with pytest.raises(ValueError):
        normalize_url("https:///path-only")


# ---- SSRF: blocked-IP predicate ------------------------------------------

@pytest.mark.parametrize("addr", [
    "127.0.0.1",      # loopback
    "127.5.5.5",      # 127/8
    "10.0.0.1",       # 10/8
    "10.255.255.255",
    "172.16.0.1",     # 172.16/12
    "172.31.255.255",
    "192.168.1.1",    # 192.168/16
    "169.254.169.254",  # link-local / cloud metadata
    "0.0.0.0",        # unspecified
    "::1",            # IPv6 loopback
    "fc00::1",        # IPv6 unique-local (fc00::/7)
    "fe80::1",        # IPv6 link-local
    "::ffff:10.0.0.1",  # IPv4-mapped IPv6 of a private addr
])
def test_blocked_ips_rejected(addr):
    assert _is_blocked_ip(ipaddress.ip_address(addr)) is True


@pytest.mark.parametrize("addr", [
    "1.1.1.1",
    "8.8.8.8",
    "93.184.216.34",   # example.com (public)
    "2606:2800:220:1::1",  # public IPv6
])
def test_public_ips_allowed(addr):
    assert _is_blocked_ip(ipaddress.ip_address(addr)) is False


# ---- SSRF: host gate (literal + resolved) --------------------------------

def test_assert_host_allowed_rejects_literal_metadata():
    with pytest.raises(FetchError):
        _assert_host_allowed("169.254.169.254")


def test_assert_host_allowed_rejects_literal_loopback():
    with pytest.raises(FetchError):
        _assert_host_allowed("127.0.0.1")


def test_assert_host_allowed_rejects_when_name_resolves_private(monkeypatch):
    # A hostname that resolves to a private address must be rejected — this is
    # the DNS-based SSRF dodge.
    def fake_getaddrinfo(host, *a, **k):
        return [(2, 1, 6, "", ("10.0.0.5", 0))]

    monkeypatch.setattr("connectors.url.socket.getaddrinfo", fake_getaddrinfo)
    with pytest.raises(FetchError):
        _assert_host_allowed("internal.evil.example")


def test_assert_host_allowed_rejects_if_any_resolved_addr_private(monkeypatch):
    # Mixed public + private resolution: reject if ANY address is blocked.
    def fake_getaddrinfo(host, *a, **k):
        return [
            (2, 1, 6, "", ("93.184.216.34", 0)),
            (2, 1, 6, "", ("127.0.0.1", 0)),
        ]

    monkeypatch.setattr("connectors.url.socket.getaddrinfo", fake_getaddrinfo)
    with pytest.raises(FetchError):
        _assert_host_allowed("rebind.example")


def test_assert_host_allowed_passes_public(monkeypatch):
    def fake_getaddrinfo(host, *a, **k):
        return [(2, 1, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr("connectors.url.socket.getaddrinfo", fake_getaddrinfo)
    # Should not raise.
    _assert_host_allowed("example.com")


def test_assert_host_allowed_unresolvable_raises(monkeypatch):
    import socket as _socket

    def fake_getaddrinfo(host, *a, **k):
        raise _socket.gaierror("nope")

    monkeypatch.setattr("connectors.url.socket.getaddrinfo", fake_getaddrinfo)
    with pytest.raises(FetchError):
        _assert_host_allowed("does-not-exist.invalid")


# ---- UrlConnector construction (scheme validation + dedupe) --------------

def test_url_connector_rejects_bad_scheme_at_construction():
    with pytest.raises(ValueError):
        UrlConnector(["file:///etc/passwd"])


def test_url_connector_dedupes_and_normalizes():
    c = UrlConnector(["https://Example.com/a#x", "https://example.com/a"])
    # Both normalise to the same URL -> one entry.
    assert c._urls == ["https://example.com/a"]


# ---- markup stripping (stable hashing) -----------------------------------

def test_strip_markup_removes_script_and_style():
    html = (
        b"<html><head><style>.x{color:red}</style></head>"
        b"<body><script>var t=Date.now()</script>"
        b"<p>Hallo Welt</p></body></html>"
    )
    out = strip_markup(html, "text/html").decode("utf-8")
    assert "Hallo Welt" in out
    assert "Date.now" not in out
    assert "color:red" not in out


def test_strip_markup_is_stable_across_volatile_script():
    a = b"<html><body><script>1700000000</script><p>Inhalt</p></body></html>"
    b = b"<html><body><script>1800000000</script><p>Inhalt</p></body></html>"
    assert strip_markup(a, "text/html") == strip_markup(b, "text/html")


def test_strip_markup_passes_non_html_through():
    raw = b"# Markdown heading\n\nbody"
    assert strip_markup(raw, "text/markdown") == raw


def test_strip_markup_sniffs_html_without_content_type():
    html = b"<!DOCTYPE html><html><body><p>X</p></body></html>"
    out = strip_markup(html, "")
    assert out.decode("utf-8").strip() == "X"


def test_source_item_defaults():
    item = SourceItem(source="s", raw_bytes=b"b")
    assert item.content_type == ""
    assert item.suggested_tags is None
