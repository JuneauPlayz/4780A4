"""Official Riot sources for Riftbound rules.

The Rules Hub is the entry point. `discover_sources()` reads the live hub page and
returns whatever documents it currently links to, so a refresh picks up new Core Rules
PDFs, patch notes and errata without code changes. The FAQ articles are not linked from
the hub, so they are discovered from the official "Rules and Releases" news listing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

RULES_HUB_URL = "https://playriftbound.com/en-us/rules-hub/"
RULES_NEWS_URL = "https://playriftbound.com/en-us/news/rules-and-releases/"
CARD_GALLERY_URL = "https://playriftbound.com/en-us/card-gallery/"
SITE = "https://playriftbound.com"

USER_AGENT = "Mozilla/5.0 (RiftboundRulesExplainer student project; rules refresh)"


@dataclass
class Source:
    kind: str  # core_rules | tournament_rules | patch_notes | errata | faq | legality | cards
    title: str
    url: str

    def to_dict(self) -> dict:
        return asdict(self)


def fetch(url: str, timeout: int = 60) -> requests.Response:
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    resp.raise_for_status()
    return resp


def _canonical(url: str) -> str:
    url = urljoin(SITE, url.strip())
    # Older riftbound.leagueoflegends.com links redirect to playriftbound.com.
    url = url.replace("https://riftbound.leagueoflegends.com", SITE)
    return url if url.endswith("/") or url.endswith(".pdf") else url + "/"


def discover_sources(hub_html: str, news_html: str | None = None) -> list[Source]:
    """Find the official documents linked from the Rules Hub (and FAQ articles from news)."""
    soup = BeautifulSoup(hub_html, "html.parser")
    sources: list[Source] = [Source("legality", "Rules Hub: Format Legality", RULES_HUB_URL)]
    seen: set[str] = set()
    section = ""
    for el in soup.find_all(["h2", "h3", "h4", "a"]):
        if el.name != "a":
            section = el.get_text(" ", strip=True).lower()
            continue
        text = el.get_text(" ", strip=True)
        href = el.get("href") or ""
        if not text or not href or href.startswith("#"):
            continue
        url = _canonical(href)
        if url in seen:
            continue
        kind = None
        if href.lower().endswith(".pdf") and "core rules" in text.lower():
            kind = "core_rules"
        elif href.lower().endswith(".pdf") and "tournament" in text.lower():
            kind = "tournament_rules"
        elif "patch notes" in section and "patch" in text.lower():
            kind = "patch_notes"
        elif section == "errata" and "errata" in text.lower():
            kind = "errata"
        if kind:
            seen.add(url)
            sources.append(Source(kind, text, url))

    if news_html:
        nsoup = BeautifulSoup(news_html, "html.parser")
        for a in nsoup.find_all("a", href=True):
            href = a["href"]
            if "/news/rules-and-releases/" in href and re.search(r"faq", href, re.I):
                url = _canonical(href)
                if url not in seen:
                    seen.add(url)
                    slug = url.rstrip("/").rsplit("/", 1)[-1]
                    sources.append(Source("faq", slug.replace("-", " ").title(), url))

    sources.append(Source("cards", "Official Card Gallery", CARD_GALLERY_URL))
    return sources
