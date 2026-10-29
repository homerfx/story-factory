"""Grabs the readable text of an article (or a Reddit post + top comments) so the AI has real facts."""
from __future__ import annotations

import ipaddress
import json
import re
import socket
import threading
from contextlib import contextmanager
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import urllib3.util.connection as _u3conn

from .collect import HTTP, clean

READ_SECONDS = 30  # a page that takes longer than this to arrive is dropped (stops slow-drip servers)


def is_public_url(url: str) -> bool:
    """Only ordinary websites on the open internet. Never your own PC, your router, your home network or the
    cloud machine's private addresses, even if a link (or a redirect) points there."""
    try:
        p = urlparse(url)
        if p.scheme not in ("http", "https") or not p.hostname or p.port not in (None, 80, 443):
            return False
        host = p.hostname.lower()
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".lan", ".home")):
            return False
        addresses = {info[4][0].split("%")[0] for info in socket.getaddrinfo(host, None)}
        return bool(addresses) and all(ipaddress.ip_address(a).is_global for a in addresses)
    except (ValueError, OSError):
        return False


# The address is checked again at the moment of connecting, so a website can't pass the check above with a public
# address and then switch to a home-network one ("DNS rebinding"). Only active while articles are being read.
_guard = threading.local()
_original_create_connection = _u3conn.create_connection


def _guarded_create_connection(address, *args, **kwargs):
    if not getattr(_guard, "on", False):
        return _original_create_connection(address, *args, **kwargs)
    host, port = address
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    for info in infos:
        if not ipaddress.ip_address(info[4][0].split("%")[0]).is_global:
            raise OSError("blocked: that link points to a private address")
    # connect to exactly the address that was checked (the site's name is still used for HTTPS)
    return _original_create_connection((infos[0][4][0], port), *args, **kwargs)


_u3conn.create_connection = _guarded_create_connection


@contextmanager
def public_only():
    _guard.on = True
    try:
        yield
    finally:
        _guard.on = False


def _host_is(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def _get_public(http: HTTP, url: str, **kw):
    """GET that checks every hop of a redirect with is_public_url. Returns None if any hop isn't public."""
    check = getattr(http, "is_public", is_public_url)
    for _ in range(5):
        if not check(url):
            return None
        with public_only():
            r = http.get(url, allow_redirects=False, **kw)
        location = r.headers.get("Location") if r.status_code in (301, 302, 303, 307, 308) else None
        if not location:
            return r
        _close(r)
        url = urljoin(url, location)
    return None


class _Paragraphs(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._in_p = 0
        self._skip = 0
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "nav", "footer", "aside"):
            self._skip += 1
        elif tag == "p":
            self._in_p += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "nav", "footer", "aside") and self._skip:
            self._skip -= 1
        elif tag == "p" and self._in_p:
            self._in_p -= 1
            text = re.sub(r"\s+", " ", "".join(self._buf)).strip()
            if len(text) > 40:
                self.parts.append(text)
            self._buf = []

    def handle_data(self, data):
        if self._in_p and not self._skip:
            self._buf.append(data)


def article_text(http: HTTP, url: str, limit: int = 5000) -> str:
    try:
        host = (urlparse(url).hostname or "").lower()
        if _host_is(host, "news.google.com") or _host_is(host, "youtube.com"):
            return ""  # Google News links can't be opened without a browser; YouTube text is already in the snippet
        if _host_is(host, "reddit.com") and "/r/" in url:
            return _reddit_post(http, url, limit)
        r = _get_public(http, url, timeout=15, stream=True)
        if r is None:
            return ""
        if r.status_code != 200 or "html" not in r.headers.get("Content-Type", ""):
            _close(r)
            return ""
        parser = _Paragraphs()
        parser.feed(_read_capped(r).decode(getattr(r, "encoding", None) or "utf-8", errors="replace"))
        return "\n".join(parser.parts)[:limit]
    except Exception:  # noqa: BLE001
        return ""


def _read_capped(r, cap: int = 2_000_000) -> bytes:
    """Read at most ~2 MB in at most READ_SECONDS, so a huge file or a slow-drip server can't hang or fill the
    machine."""
    if not hasattr(r, "iter_content"):
        return (r.text or "")[:cap].encode("utf-8")
    timer = threading.Timer(READ_SECONDS, _close, args=(r,))
    timer.start()
    chunks, size = [], 0
    try:
        for chunk in r.iter_content(65536):
            chunks.append(chunk)
            size += len(chunk)
            if size >= cap:
                break
    finally:
        timer.cancel()
        _close(r)
    return b"".join(chunks)


def _close(r) -> None:
    try:
        r.close()
    except Exception:  # noqa: BLE001
        pass


def _reddit_post(http: HTTP, url: str, limit: int) -> str:
    """Post text plus the top comments, which usually explain where a clip came from."""
    r = _get_public(http, url.split("?")[0].rstrip("/") + ".json", params={"limit": 12, "sort": "top"}, timeout=15,
                    stream=True)
    if r is None or r.status_code != 200:
        return ""
    data = json.loads(_read_capped(r))
    post = data[0]["data"]["children"][0]["data"]
    parts = [f"POST: {clean(post.get('title', ''), 300)}"]
    if post.get("selftext"):
        parts.append(clean(post["selftext"], 1500))
    if post.get("url") and "reddit.com" not in post["url"]:
        parts.append(f"LINKED MEDIA: {post['url']}")
    parts.append("TOP COMMENTS:")
    for child in data[1]["data"]["children"][:12]:
        body = child.get("data", {}).get("body")
        if body:
            parts.append(f"- ({child['data'].get('ups', 0)} upvotes) {clean(body, 400)}")
    return "\n".join(parts)[:limit]
