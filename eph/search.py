"""Live web search used to verify parts (pinout, voltage, library) with citations.

Primary: Linkup (/v1/search, outputType=sourcedAnswer). Fallback: Tavily. Results are cached
in MongoDB (search_cache) so reruns and partial rebuilds don't burn credits.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

import httpx

from .config import Settings
from .db import Store
from .schema import Evidence


@dataclass
class SearchResult:
    query: str
    answer: str = ""
    sources: list[Evidence] = field(default_factory=list)
    provider: str = ""
    cached: bool = False

    def to_payload(self) -> dict:
        return {"answer": self.answer, "sources": [s.model_dump(mode="json") for s in self.sources]}

    @classmethod
    def from_payload(cls, query: str, provider: str, p: dict, cached: bool) -> "SearchResult":
        return cls(query=query, answer=p.get("answer", ""), provider=provider, cached=cached,
                   sources=[Evidence(**s) for s in p.get("sources", [])])


class Search(Protocol):
    name: str

    def search(self, query: str) -> SearchResult: ...


class LinkupSearch:
    name = "linkup"
    URL = "https://api.linkup.so/v1/search"

    def __init__(self, api_key: str, depth: str = "standard"):
        self.api_key, self.depth = api_key, depth

    def search(self, query: str) -> SearchResult:
        r = httpx.post(
            self.URL,
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            json={"q": query, "depth": self.depth, "outputType": "sourcedAnswer"},
            timeout=90,
        )
        r.raise_for_status()
        data = r.json()
        sources = [
            Evidence(source="linkup", url=s.get("url"), title=s.get("name"),
                     snippet=(s.get("snippet") or s.get("content") or "")[:500])
            for s in data.get("sources", [])[:5]
        ]
        return SearchResult(query=query, answer=data.get("answer", ""), sources=sources, provider=self.name)


class TavilySearch:
    name = "tavily"
    URL = "https://api.tavily.com/search"

    def __init__(self, api_key: str):
        self.api_key = api_key

    def search(self, query: str) -> SearchResult:
        r = httpx.post(
            self.URL,
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={"query": query, "include_answer": True, "max_results": 5, "search_depth": "basic"},
            timeout=60,
        )
        r.raise_for_status()
        data = r.json()
        sources = [
            Evidence(source="tavily", url=s.get("url"), title=s.get("title"), snippet=(s.get("content") or "")[:500])
            for s in data.get("results", [])[:5]
        ]
        return SearchResult(query=query, answer=data.get("answer") or "", sources=sources, provider=self.name)


class NullSearch:
    name = "none"

    def search(self, query: str) -> SearchResult:
        return SearchResult(query=query, provider=self.name)


class CachedSearch:
    """Wraps a provider with the Mongo search cache."""

    def __init__(self, inner: Search, store: Store):
        self.inner, self.store = inner, store
        self.name = inner.name

    def search(self, query: str) -> SearchResult:
        hit = self.store.cache_get(self.name, query)
        if hit is not None:
            return SearchResult.from_payload(query, self.name, hit, cached=True)
        res = self.inner.search(query)
        if res.answer or res.sources:
            self.store.cache_put(self.name, query, res.to_payload())
        return res


def make_search(settings: Settings, store: Store) -> Search:
    if settings.mock:
        from .mock import MockSearch

        inner: Search = MockSearch()
    elif settings.linkup_api_key:
        inner = LinkupSearch(settings.linkup_api_key, settings.linkup_depth)
    elif settings.tavily_api_key:
        inner = TavilySearch(settings.tavily_api_key)
    else:
        inner = NullSearch()
    return CachedSearch(inner, store) if inner.name != "none" else inner
