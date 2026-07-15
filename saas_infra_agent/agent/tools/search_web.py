from __future__ import annotations

import os
import re
import socket
import urllib.parse
import urllib.request
from html import unescape

from langchain.tools import tool

from saas_infra_agent.config.config import config
from saas_infra_agent.observability.logger import get_logger

logger = get_logger(__name__)


# ── config helpers ────────────────────────────────────────────────────────────

def _get_cfg() -> dict:
    return (config.get("web_search") or {}) if isinstance(config, dict) else {}


def _provider() -> str:
    """Returns: 'tavily' | 'duckduckgo'  (default: tavily)."""
    return str(_get_cfg().get("provider", "tavily")).lower()


# ── Tavily ────────────────────────────────────────────────────────────────────

def _search_tavily(query: str, max_results: int) -> list[dict]:
    """
    Calls Tavily Search API.
    Requires config:
        web_search:
            provider: tavily
    Docs: https://docs.tavily.com/docs/rest-api/api-reference
    """
    try:
        from tavily import TavilyClient  # pip install tavily-python
    except ImportError as exc:
        raise RuntimeError(
            "tavily-python is not installed. Run: poetry add tavily-python"
        ) from exc

    api_key = os.getenv("TAVILY_API_KEY") or ""
    if not api_key:
        raise ValueError(
            "web_search.TAVILY_API_KEY is missing from config. "
            "Get a free key at https://app.tavily.com"
        )
    
    logger.info(f"Tavily search initiated for query {query}")

    client = TavilyClient(api_key=api_key)
    response = client.search(
        query=query,
        max_results=max_results,
        search_depth="basic",   # "basic" (fast) | "advanced" (deeper, costs 2 credits)
        include_answer=False,   # raw results only — agent will reason over them
    )

    results = []
    for item in response.get("results", []):
        results.append(
            {
                "title":   item.get("title", "").strip(),
                "url":     item.get("url", "").strip(),
                "snippet": item.get("content", "").strip(),
            }
        )
    return results


# ── DuckDuckGo fallback ───────────────────────────────────────────────────────

def _http_get(url: str, timeout_s: float) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "saas-cli/0.1 (web search tool; +https://example.invalid)",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, errors="replace")


def _parse_duckduckgo_html(html: str, max_results: int) -> list[dict]:
    link_re = re.compile(
        r'<a[^>]+class="result__a"[^>]+href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>',
        re.IGNORECASE | re.DOTALL,
    )
    snippet_re = re.compile(
        r'<(?:a|div)[^>]+class="result__snippet"[^>]*>(?P<snippet>.*?)</(?:a|div)>',
        re.IGNORECASE | re.DOTALL,
    )
    tag_re = re.compile(r"<[^>]+>")

    links    = list(link_re.finditer(html))
    snippets = list(snippet_re.finditer(html))

    results: list[dict] = []
    for idx, m in enumerate(links[:max_results]):
        href    = unescape(m.group("href")).strip()
        title   = unescape(tag_re.sub("", m.group("title"))).strip()
        snippet = ""
        if idx < len(snippets):
            snippet = unescape(tag_re.sub("", snippets[idx].group("snippet"))).strip()
        results.append({"title": title, "url": href, "snippet": snippet})
    return results


def _search_duckduckgo(query: str, max_results: int) -> list[dict]:
    cfg      = _get_cfg()
    timeout  = float(cfg.get("timeout_s", 10))
    base_url = str(cfg.get("duckduckgo_html_url", "https://duckduckgo.com/html/"))
    url      = f"{base_url}?{urllib.parse.urlencode({'q': query})}"
    html     = _http_get(url, timeout_s=timeout)
    return _parse_duckduckgo_html(html, max_results)


# ── format results ────────────────────────────────────────────────────────────

def _format(results: list[dict]) -> str:
    if not results:
        return "No results found."
    lines: list[str] = []
    for i, r in enumerate(results, start=1):
        lines.append(f"{i}. {r['title']}")
        lines.append(f"   {r['url']}")
        if r["snippet"]:
            lines.append(f"   {r['snippet']}")
    return "\n".join(lines)


# ── LangChain tool ────────────────────────────────────────────────────────────

@tool
def search_web(query: str, max_results: int = 5) -> str:
    """
    Search the public web and return top results.

    Primary provider : Tavily  (reliable, AI-optimised, 1000 free searches/month)
    Fallback provider: DuckDuckGo HTML scrape (best-effort, no API key needed)

    """
    query = (query or "").strip()
    if not query:
        return "Query is empty."

    max_results = max(1, min(int(max_results), 10))
    provider    = _provider()

    logger.info(f"search_web | provider={provider} query={query!r} max={max_results}")

    # ── primary: Tavily ───────────────────────────────────────────────────────
    if provider == "tavily":
        try:
            results = _search_tavily(query, max_results)
            logger.info(f"search_web | tavily returned {len(results)} results")
            return _format(results)
        except Exception as exc:
            logger.warning(f"search_web | Tavily failed ({exc}); falling back to DuckDuckGo")
            # fall through to DuckDuckGo

    # ── fallback: DuckDuckGo ──────────────────────────────────────────────────
    try:
        results = _search_duckduckgo(query, max_results)
        logger.info(f"search_web | duckduckgo returned {len(results)} results")
        return _format(results)
    except (urllib.error.URLError, socket.timeout) as exc:
        return f"Web search failed (network/timeout): {exc}"
    except Exception as exc:
        logger.exception("search_web | DuckDuckGo fallback also failed")
        return f"Web search failed: {exc}"