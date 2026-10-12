"""A fake web for evals (D84): any search finds two pages, each with a little text — no network at all."""

from __future__ import annotations

from urllib.parse import parse_qs

import httpx

from sonu.integrations.research import WebAccess

_RESULTS = (
    '<a class="result__a" href="https://encyclopedia.example/topic">An encyclopedia</a>'
    '<a class="result__a" href="https://news.example/story">A news story</a>'
)


def _handler(request: httpx.Request) -> httpx.Response:
    if request.url.host == "html.duckduckgo.com":
        query = parse_qs(request.content.decode()).get("q", [""])[0]
        return httpx.Response(200, text=f"<html><body>{_RESULTS}<!-- {query} --></body></html>")
    host = request.url.host
    page = f"<html><title>{host}</title><body><p>What {host} says about it.</p></body></html>"
    return httpx.Response(200, text=page, headers={"content-type": "text/html"})


def fake_web() -> WebAccess:
    return WebAccess(httpx.MockTransport(_handler), resolve=lambda host: ["93.184.216.34"])
