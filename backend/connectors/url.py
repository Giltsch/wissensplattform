"""URL connector: fetch an operator-supplied allow-list of http(s) URLs.

Security is the whole point of this module. The fetch layer enforces, on
*every* request and *every* redirect hop:

  * an http/https scheme allow-list (no file://, ftp://, gopher://, ...),
  * an SSRF guard that resolves the hostname and rejects private / loopback
    / link-local / metadata ranges (the cloud-metadata endpoint at
    169.254.169.254 is the classic target),
  * a byte cap enforced *while streaming* the response body — we never trust
    a server-supplied Content-Length, which can lie to slip past the cap,
  * connect + read timeouts.

After fetch, volatile markup (``<script>`` / ``<style>`` blocks and all
tags) is stripped via the stdlib ``html.parser`` so the *text* we hash for
dedup is stable across re-fetches even when the page's surrounding chrome
(nonces, timestamps, CSRF tokens in hidden inputs) churns. ``source`` is the
normalized URL (fragment stripped, host lowercased) so re-syncing the same
page versions the same document instead of creating a duplicate.

Tags are NEVER derived from URL structure — external sources require an
explicit tags decision by the caller (CLI/admin). ``suggested_tags`` is
passed through on the SourceItem but the driver ignores it for the ACL.
"""
from __future__ import annotations

import ipaddress
import logging
import socket
from html.parser import HTMLParser
from typing import AsyncIterator
from urllib.parse import urlsplit, urlunsplit

import httpx

from config import get_settings
from connectors import SourceItem

log = logging.getLogger(__name__)

_ALLOWED_SCHEMES = {"http", "https"}


class FetchError(RuntimeError):
    """A URL could not be fetched safely (blocked target, oversize body,
    timeout, transport error, or a disallowed scheme). The driver maps this
    to a per-item error result rather than aborting the whole sync."""


# ---- SSRF guard ----------------------------------------------------------

def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True if ``ip`` falls in a range we must never fetch from.

    Covers the standard RFC1918 private space (10/8, 172.16/12, 192.168/16),
    loopback (127/8, ::1), link-local incl. the cloud-metadata 169.254/16 and
    IPv6 fe80::/10, the IPv6 unique-local fc00::/7, plus unspecified and
    multicast/reserved. We also map IPv4-mapped IPv6 (::ffff:a.b.c.d) back to
    v4 so an attacker can't dodge the v4 checks by spelling the address in
    v6. ``ip_address``'s own predicates already encode most of these ranges
    — we lean on them rather than re-deriving CIDR maths by hand."""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return (
        ip.is_private          # 10/8, 172.16/12, 192.168/16, fc00::/7, ...
        or ip.is_loopback      # 127/8, ::1
        or ip.is_link_local    # 169.254/16, fe80::/10
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _assert_host_allowed(host: str) -> None:
    """Resolve ``host`` and reject if any resolved address is in a blocked
    range. Raises FetchError on a blocked or unresolvable host.

    Resolving *all* addresses (getaddrinfo) and rejecting if *any* is blocked
    closes the DNS-rebinding-ish hole where a name resolves to both a public
    and a private address. A literal IP host is validated the same way."""
    if not host:
        raise FetchError("missing host")
    # A bracketed/!literal IPv6 or plain IPv4/6 literal: validate directly.
    literal = host.strip("[]")
    try:
        ip = ipaddress.ip_address(literal)
        if _is_blocked_ip(ip):
            raise FetchError(f"blocked address: {host}")
        return
    except ValueError:
        pass  # not a literal — resolve the name below

    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise FetchError(f"cannot resolve host {host!r}: {e}") from e
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if _is_blocked_ip(ip):
            raise FetchError(f"blocked address for {host}: {addr}")


# ---- URL normalization ----------------------------------------------------

def normalize_url(url: str) -> str:
    """Canonicalise a URL for use as the stable ``documents.source`` key.

    Strips the fragment (``#...`` never reaches the server and would
    needlessly fork versions), lowercases the scheme and host, and validates
    the scheme against the http(s) allow-list. Path/query are preserved
    verbatim (they are server-significant). Raises ValueError on a missing or
    disallowed scheme / missing host so a bad entry in the allow-list fails
    loudly before any fetch."""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise ValueError(
            f"unsupported URL scheme {parts.scheme!r}: only http/https allowed"
        )
    if not parts.hostname:
        raise ValueError(f"URL has no host: {url!r}")
    # Rebuild netloc with a lowercased host but the original port/userinfo
    # left untouched (userinfo in a fetch URL is unusual but not our call to
    # strip). hostname is already lowercased by urlsplit for the .hostname
    # accessor, but netloc preserves original case — rebuild explicitly.
    host = parts.hostname.lower()
    netloc = host
    if parts.port is not None:
        netloc = f"{host}:{parts.port}"
    if parts.username:
        userinfo = parts.username
        if parts.password:
            userinfo += f":{parts.password}"
        netloc = f"{userinfo}@{netloc}"
    return urlunsplit((scheme, netloc, parts.path, parts.query, ""))


# ---- Markup stripping -----------------------------------------------------

class _TextExtractor(HTMLParser):
    """Collect visible text, dropping the contents of <script>/<style> and
    other volatile/non-content elements. Tag *structure* is discarded — we
    only want a stable text projection for hashing + embedding."""

    _SKIP = {"script", "style", "noscript", "template", "head"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: object) -> None:
        if tag in self._SKIP:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            text = data.strip()
            if text:
                self._chunks.append(text)

    def text(self) -> str:
        return "\n".join(self._chunks)


def _looks_like_html(content_type: str, body: bytes) -> bool:
    ct = (content_type or "").split(";", 1)[0].strip().lower()
    if ct in ("text/html", "application/xhtml+xml"):
        return True
    if ct:
        return False
    # No content-type: sniff the first bytes for an HTML marker.
    head = body[:512].lstrip().lower()
    return head.startswith(b"<!doctype html") or head.startswith(b"<html")


def strip_markup(body: bytes, content_type: str) -> bytes:
    """Return a stable text projection of an HTML body as utf-8 bytes; pass
    non-HTML bodies through unchanged. Stripping <script>/<style> and tag
    chrome keeps the dedup hash stable across re-fetches of the same page."""
    if not _looks_like_html(content_type, body):
        return body
    html = body.decode("utf-8", errors="replace")
    extractor = _TextExtractor()
    extractor.feed(html)
    extractor.close()
    return extractor.text().encode("utf-8")


# ---- Connector ------------------------------------------------------------

class UrlConnector:
    """Fetch each URL in an allow-list into a ``SourceItem``.

    The allow-list is supplied by the operator (CLI ``--urls-file`` or the
    admin sync request body) — there is no crawling, no link-following beyond
    the explicit redirect hops of a single fetch, and no URL discovery.
    """

    def __init__(
        self, urls: list[str], *, suggested_tags: list[str] | None = None,
    ) -> None:
        # Normalise (and scheme-validate) up front so a bad entry fails before
        # we open any socket. De-dupe while preserving order.
        seen: set[str] = set()
        self._urls: list[str] = []
        for u in urls:
            norm = normalize_url(u)
            if norm not in seen:
                seen.add(norm)
                self._urls.append(norm)
        self._suggested_tags = suggested_tags

    async def iter_items(self) -> AsyncIterator[SourceItem]:
        settings = get_settings()
        max_bytes = settings.source_max_bytes
        timeout = httpx.Timeout(
            settings.source_fetch_timeout_s,
            connect=settings.source_fetch_timeout_s,
        )
        headers = {"User-Agent": settings.url_fetch_user_agent}
        # follow_redirects=False: we drive the redirect chain by hand so the
        # SSRF guard runs on EVERY hop's host (httpx's own following would
        # only let us inspect the final response, after the requests have
        # already left for intermediate — possibly internal — hosts).
        async with httpx.AsyncClient(
            timeout=timeout, headers=headers, follow_redirects=False,
        ) as client:
            for url in self._urls:
                body, content_type, final_url = await self._fetch_one(
                    client, url, max_bytes,
                )
                text_bytes = strip_markup(body, content_type)
                yield SourceItem(
                    # Stable versioning key: the URL the operator asked for,
                    # not the post-redirect URL (which may drift), normalised.
                    source=url,
                    raw_bytes=text_bytes,
                    content_type="text/plain",
                    suggested_tags=self._suggested_tags,
                )

    async def _fetch_one(
        self, client: httpx.AsyncClient, url: str, max_bytes: int,
    ) -> tuple[bytes, str, str]:
        """Fetch a single URL, following up to a small number of redirects,
        re-running the SSRF guard on every hop. Returns (body, content_type,
        final_url). Raises FetchError on any safety violation."""
        current = url
        max_hops = 5
        for _hop in range(max_hops + 1):
            parts = urlsplit(current)
            if parts.scheme.lower() not in _ALLOWED_SCHEMES:
                raise FetchError(f"disallowed scheme on hop: {parts.scheme!r}")
            host = (parts.hostname or "").lower()
            # SSRF guard BEFORE the request leaves the process.
            _assert_host_allowed(host)
            try:
                async with client.stream("GET", current) as resp:
                    if resp.is_redirect:
                        loc = resp.headers.get("location")
                        if not loc:
                            raise FetchError(f"redirect without Location: {current}")
                        # Resolve relative redirects against the current URL.
                        current = str(httpx.URL(current).join(loc))
                        # Re-normalise/validate scheme+host on the next loop.
                        continue
                    if resp.status_code >= 400:
                        raise FetchError(
                            f"HTTP {resp.status_code} for {current}"
                        )
                    content_type = resp.headers.get("content-type", "")
                    # Stream the body with our OWN byte counter — never trust
                    # Content-Length, which can under-report to slip the cap.
                    buf = bytearray()
                    async for chunk in resp.aiter_bytes():
                        buf += chunk
                        if len(buf) > max_bytes:
                            raise FetchError(
                                f"body exceeds {max_bytes} bytes: {current}"
                            )
                    return bytes(buf), content_type, current
            except httpx.HTTPError as e:
                raise FetchError(f"fetch failed for {current}: {e}") from e
        raise FetchError(f"too many redirects: {url}")
