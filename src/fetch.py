"""Plain HTTP helpers: resolving grounding redirects and scraping page text.

Free stage. Whatever text we can pull off the event's own page is fed to the
enrichment call, which makes that call cheaper to ground and harder to
hallucinate against.
"""

from __future__ import annotations

import logging
import re

import httpx
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

MAX_PAGE_CHARS = 8000
TIMEOUT = httpx.Timeout(15.0, connect=10.0)
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en,de;q=0.8",
}

# Gemini hands back opaque redirect links rather than the page it actually read.
# Which is useless for Evidence and Page scraping.
REDIRECT_HOSTS = ("vertexaisearch.cloud.google.com", "grounding-api-redirect")

_STRIP_TAGS = ("script", "style", "noscript", "nav", "footer", "header", "svg", "form")
_WHITESPACE = re.compile(r"\s+")


def make_client() -> httpx.Client:
    return httpx.Client(timeout=TIMEOUT, headers=HEADERS, follow_redirects=True)


def resolve_url(client: httpx.Client, url: str) -> str:
    """Follow a grounding redirect to the real page URL, or return the input."""
    if not any(host in url for host in REDIRECT_HOSTS):
        return url
    try:
        response = client.head(url)
        if response.status_code >= 400:
            response = client.get(url)
        return str(response.url)
    except httpx.HTTPError as exc:
        log.debug("Could not resolve redirect %s: %s", url, exc)
        return url


def fetch_page_text(client: httpx.Client, url: str, max_chars: int = MAX_PAGE_CHARS) -> str | None:
    """Return readable text from ``url``, or None if it cannot be fetched."""
    try:
        response = client.get(url)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        log.debug("Fetch failed for %s: %s", url, exc)
        return None

    if "html" not in response.headers.get("content-type", "text/html"):
        return None

    soup = BeautifulSoup(response.text, "html.parser")
    for tag in soup(_STRIP_TAGS):
        tag.decompose()
    text = _WHITESPACE.sub(" ", soup.get_text(" ", strip=True)).strip()
    if not text:
        return None
    return text[:max_chars]
