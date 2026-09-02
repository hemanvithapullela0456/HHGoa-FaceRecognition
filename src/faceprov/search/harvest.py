"""Fetch candidate source pages and pull the images / metadata worth verifying."""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

UA = {"User-Agent": "Mozilla/5.0 (compatible; faceprov/0.1; +https://github.com/)"}

_PLATFORM = {
    "instagram.com": "instagram",
    "x.com": "x",
    "twitter.com": "twitter",
    "facebook.com": "facebook",
    "linkedin.com": "linkedin",
    "tiktok.com": "tiktok",
}


@dataclass
class HarvestedPage:
    url: str
    html: bytes
    status: int
    platform: str | None
    author_handle: str | None
    og_image: str | None
    candidate_images: list[str]


def platform_of(url: str) -> str | None:
    host = urlparse(url).netloc.lower().removeprefix("www.")
    for dom, name in _PLATFORM.items():
        if host == dom or host.endswith("." + dom):
            return name
    return None


def _handle(url: str, soup: BeautifulSoup) -> str | None:
    path = urlparse(url).path.strip("/").split("/")
    if path and path[0] not in {"", "p", "reel", "status", "posts"}:
        return "@" + path[0]
    m = soup.find("meta", property="og:title")
    if m and m.get("content"):
        mm = re.search(r"@([A-Za-z0-9_.]+)", m["content"])
        if mm:
            return "@" + mm.group(1)
    return None


def harvest(url: str, *, timeout: int = 20) -> HarvestedPage:
    try:
        r = requests.get(url, headers=UA, timeout=timeout)
        html, status = r.content, r.status_code
    except requests.RequestException as e:  # noqa: BLE001
        return HarvestedPage(url, str(e).encode(), 0, platform_of(url), None, None, [])

    soup = BeautifulSoup(html, "lxml")
    og = soup.find("meta", property="og:image")
    og_image = urljoin(url, og["content"]) if og and og.get("content") else None

    imgs: list[str] = []
    if og_image:
        imgs.append(og_image)
    for tag in soup.find_all("img"):
        src = tag.get("src") or tag.get("data-src")
        if src:
            full = urljoin(url, src)
            if full not in imgs and not full.lower().endswith((".svg", ".gif")):
                imgs.append(full)

    return HarvestedPage(
        url=url,
        html=html,
        status=status,
        platform=platform_of(url),
        author_handle=_handle(url, soup),
        og_image=og_image,
        candidate_images=imgs[:8],
    )
