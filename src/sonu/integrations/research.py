"""Looking something up on the web for the user (D84): a search with their own words, the top pages fetched by
SONU itself and turned into text for the local model.

What leaves the laptop is the user's words (the query, to DuckDuckGo's HTML search — no key, no JavaScript)
and plain requests for the result pages. Pages are fetched with no cookies, no scripts, http(s) only, at
most ``MAX_BYTES`` and ``TIMEOUT_S`` each, HTML or plain text only, and **only from public addresses** — a
result, or a redirect, that points at this laptop or the local network is refused (checked on every hop).
Their text is what other people wrote: data for the local model, never instructions.

Measured on the laptop: the search answered in ~1 s with real result addresses (behind DuckDuckGo's
redirect).
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import re
import socket
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

import httpx

SEARCH_URL = "https://html.duckduckgo.com/html/"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) SONU/1.0"
TIMEOUT_S = 8.0
MAX_BYTES = 2_000_000
MAX_REDIRECTS = 3
_RESULT = re.compile(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S)
_TAGS = re.compile(r"<[^>]+>")


class ResearchError(Exception):
    pass


Resolve = Callable[[str], Iterable[str]]


def _resolve(host: str) -> list[str]:
    return [str(info[4][0]) for info in socket.getaddrinfo(host, None)]


@dataclass
class WebAccess:
    """How SONU reaches the web for research: tests and evals pass a fake transport and resolver."""

    transport: httpx.AsyncBaseTransport | None = None
    resolve: Resolve = field(default=_resolve)


@dataclass(frozen=True, slots=True)
class Result:
    url: str
    title: str

    @property
    def host(self) -> str:
        return (urlsplit(self.url).hostname or "").removeprefix("www.")


def is_public(host: str, resolve: Resolve) -> bool:
    """Every address the host has is on the public internet (not this laptop, the local network, …)."""
    try:
        addresses = list(resolve(host))
    except (OSError, UnicodeError):
        return False
    if not addresses:
        return False
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address.split("%")[0])
        except ValueError:
            return False
        if not ip.is_global or ip.is_multicast:
            return False
    return True


def _client(web: WebAccess) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=web.transport,
        timeout=TIMEOUT_S,
        follow_redirects=False,  # each hop is checked
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,text/plain;q=0.9"},
    )


async def search(query: str, web: WebAccess, limit: int = 6) -> list[Result]:
    """The top results for the user's words (DuckDuckGo's HTML search)."""
    try:
        async with _client(web) as client:
            response = await client.post(SEARCH_URL, data={"q": query})
    except httpx.HTTPError as exc:
        raise ResearchError(f"the search didn't answer ({exc.__class__.__name__})") from exc
    if response.status_code != 200:
        raise ResearchError(f"the search answered {response.status_code}")
    results: list[Result] = []
    for href, title in _RESULT.findall(response.text):
        url = unescape(href)
        if "uddg=" in url:  # DuckDuckGo's own redirect: the real address is in it
            url = unquote(parse_qs(urlsplit(url).query).get("uddg", [""])[0])
        if url.startswith(("https://", "http://")) and url not in {r.url for r in results}:
            results.append(Result(url, " ".join(unescape(_TAGS.sub("", title)).split())))
        if len(results) >= limit:
            break
    return results


async def fetch_text(url: str, web: WebAccess) -> tuple[str, str] | None:
    """(title, text) of a page, or None if it can't or mustn't be read."""
    async with _client(web) as client:
        for _ in range(MAX_REDIRECTS + 1):
            parts = urlsplit(url)
            if parts.scheme not in ("https", "http") or not parts.hostname:
                return None
            if not await asyncio.to_thread(is_public, parts.hostname, web.resolve):
                return None  # this laptop or the local network: never
            try:
                async with client.stream("GET", url) as response:
                    if response.is_redirect and (location := response.headers.get("location")):
                        url = urljoin(url, location)
                        continue
                    kind = response.headers.get("content-type", "").split(";")[0].strip().lower()
                    if response.status_code != 200 or kind not in ("text/html", "text/plain", ""):
                        return None
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body += chunk
                        if len(body) > MAX_BYTES:
                            break
                    text = bytes(body[:MAX_BYTES]).decode(response.encoding or "utf-8", errors="replace")
            except httpx.HTTPError:
                return None
            return page_text(text) if kind != "text/plain" else ("", " ".join(text.split()))
    return None


class _Reader(HTMLParser):
    """The text a reader sees: no scripts, styles, menus, headers, footers or forms."""

    SKIP = frozenset(
        {"script", "style", "noscript", "nav", "header", "footer", "aside", "form", "svg", "template"}
    )
    BLOCK = frozenset(
        {"p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "br", "section", "article", "pre"}
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.skipping = 0
        self.title, self.in_title = "", False
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.SKIP:
            self.skipping += 1
        elif tag == "title":
            self.in_title = True
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP and self.skipping:
            self.skipping -= 1
        elif tag == "title":
            self.in_title = False
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title += data
        elif not self.skipping:
            self.parts.append(data)


def page_text(html: str) -> tuple[str, str]:
    reader = _Reader()
    with contextlib.suppress(Exception):  # broken HTML: what was read so far
        reader.feed(html)
    lines = [" ".join(line.split()) for line in "".join(reader.parts).splitlines()]
    text = "\n".join(line for line in lines if len(line) > 1)
    return " ".join(reader.title.split()), re.sub(r"\n{3,}", "\n\n", text)
