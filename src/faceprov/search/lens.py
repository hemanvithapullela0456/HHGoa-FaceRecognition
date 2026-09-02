"""SerpApi — Google Lens reverse image search (high recall + entity hints)."""
from __future__ import annotations

from dataclasses import dataclass

import requests

ENDPOINT = "https://serpapi.com/search"


@dataclass
class VisualMatch:
    title: str
    source: str          # domain / platform
    source_url: str      # page URL
    thumbnail: str       # image URL
    engine: str = "google_lens"


@dataclass
class LensResult:
    entity_name: str | None
    entity_type: str | None
    matches: list[VisualMatch]
    raw_search_metadata: dict


def search(image_url: str, api_key: str, *, hl: str = "en") -> LensResult:
    params = {
        "engine": "google_lens",
        "url": image_url,
        "hl": hl,
        "api_key": api_key,
    }
    r = requests.get(ENDPOINT, params=params, timeout=60)
    r.raise_for_status()
    data = r.json()

    kg = data.get("knowledge_graph") or {}
    if isinstance(kg, list):
        kg = kg[0] if kg else {}

    matches = [
        VisualMatch(
            title=m.get("title", ""),
            source=m.get("source", ""),
            source_url=m.get("link", ""),
            thumbnail=m.get("thumbnail", ""),
        )
        for m in data.get("visual_matches", [])
        if m.get("link")
    ]

    return LensResult(
        entity_name=kg.get("title"),
        entity_type=kg.get("type") or kg.get("subtitle"),
        matches=matches,
        raw_search_metadata={
            "id": data.get("search_metadata", {}).get("id"),
            "google_lens_url": data.get("search_metadata", {}).get("google_lens_url"),
            "created_at": data.get("search_metadata", {}).get("created_at"),
        },
    )


def find_social_profiles(name: str, api_key: str) -> list[dict]:
    """Path A — targeted text search for a resolved person's verified profiles."""
    q = f'{name} (site:instagram.com OR site:x.com OR site:twitter.com OR site:linkedin.com OR site:facebook.com)'
    params = {"engine": "google", "q": q, "num": "10", "api_key": api_key}
    r = requests.get(ENDPOINT, params=params, timeout=60)
    r.raise_for_status()
    data = r.json()
    return [
        {"title": o.get("title", ""), "link": o.get("link", ""), "snippet": o.get("snippet", "")}
        for o in data.get("organic_results", [])
        if o.get("link")
    ]
