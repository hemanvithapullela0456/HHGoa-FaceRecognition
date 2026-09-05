"""Fetch candidate source pages and pull the images / metadata worth verifying.

Social platforms serve almost nothing to unauthenticated bots, so this is best-effort:
we take whatever OpenGraph / Twitter-card image and metadata the page exposes and
degrade gracefully when a page returns a login wall or 403. The router treats the
SerpApi-provided thumbnail as the primary, always-fetchable candidate image and uses
this harvest only to enrich the record (page hash, author handle, caption).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ..content import PageFingerprint, fingerprint_page

UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

_PLATFORM = {
    "instagram.com": "instagram",
    "x.com": "x",
    "twitter.com": "twitter",
    "facebook.com": "facebook",
    "fb.com": "facebook",
    "linkedin.com": "linkedin",
    "tiktok.com": "tiktok",
    "threads.net": "threads",
    "youtube.com": "youtube",
    "reddit.com": "reddit",
}


@dataclass
class HarvestedPage:
    url: str
    html: bytes
    status: int
    platform: str | None
    author_handle: str | None
    og_image: str | None
    title: str | None
    description: str | None
    candidate_images: list[str] = field(default_factory=list)
    error: str | None = None
    # Three-tier content fingerprint (see faceprov.content). None only when the fetch
    # itself failed and there are no bytes to fingerprint.
    fingerprint: PageFingerprint | None = None


def platform_of(url: str) -> str | None:
    host = urlparse(url).netloc.lower().removeprefix("www.")
    for dom, name in _PLATFORM.items():
        if host == dom or host.endswith("." + dom):
            return name
    return None


def _meta(soup: BeautifulSoup, *keys: str) -> str | None:
    for k in keys:
        tag = soup.find("meta", property=k) or soup.find("meta", attrs={"name": k})
        if tag and tag.get("content"):
            return tag["content"].strip()
    return None


def _handle(url: str, soup: BeautifulSoup) -> str | None:
    path = [p for p in urlparse(url).path.strip("/").split("/") if p]
    skip = {"p", "reel", "reels", "status", "posts", "photo", "video", "watch", "share"}
    if path and path[0].lower() not in skip:
        return "@" + path[0]
    title = _meta(soup, "og:title", "twitter:title") or ""
    m = re.search(r"@([A-Za-z0-9_.]+)", title)
    return "@" + m.group(1) if m else None


def harvest(url: str, *, timeout: int = 20, retries: int = 1) -> HarvestedPage:
    last_err = None
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, headers=UA, timeout=timeout, allow_redirects=True)
            html, status = r.content, r.status_code
            break
        except requests.RequestException as e:  # noqa: BLE001
            last_err = str(e)
    else:
        return HarvestedPage(url, b"", 0, platform_of(url), None, None, None, None, error=last_err)

    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception as e:  # noqa: BLE001
        return HarvestedPage(
            url, html, status, platform_of(url), None, None, None, None,
            error=f"parse: {e}",
            fingerprint=fingerprint_page(html, url, status=status),
        )

    og_image = _meta(soup, "og:image", "og:image:url", "twitter:image", "twitter:image:src")
    if og_image:
        og_image = urljoin(url, og_image)

    imgs: list[str] = [og_image] if og_image else []
    for tag in soup.find_all("img"):
        src = tag.get("src") or tag.get("data-src") or tag.get("data-lazy-src")
        if not src:
            continue
        full = urljoin(url, src)
        if full not in imgs and not full.lower().endswith((".svg", ".gif", ".ico")):
            imgs.append(full)

    author_handle = _handle(url, soup)
    title = _meta(soup, "og:title", "twitter:title") or (
        soup.title.string.strip() if soup.title and soup.title.string else None
    )
    description = _meta(soup, "og:description", "twitter:description", "description")

    return HarvestedPage(
        url=url,
        html=html,
        status=status,
        platform=platform_of(url),
        author_handle=author_handle,
        og_image=og_image,
        title=title,
        description=description,
        candidate_images=imgs[:8],
        error=None if status == 200 else f"HTTP {status}",
        # fingerprint the same metadata the pipeline acted on, so the sealed evidence
        # and the decision it drove can never describe different pages
        fingerprint=fingerprint_page(
            html, url, status=status, title=title, description=description,
            author_handle=author_handle, og_image=og_image,
        ),
    )
