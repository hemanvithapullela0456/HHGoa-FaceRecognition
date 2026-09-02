"""SerpApi — Yandex Images reverse search (strongest on faces, native crop param)."""
from __future__ import annotations

import requests

from .lens import VisualMatch

ENDPOINT = "https://serpapi.com/search"


def _crop_param(bbox_norm: tuple[float, float, float, float]) -> str:
    # Yandex expects left;top;right;bottom, each in [0,1]
    l, t, r, b = bbox_norm
    return f"{l:.4f};{t:.4f};{r:.4f};{b:.4f}"


def search(
    image_url: str,
    api_key: str,
    *,
    bbox_norm: tuple[float, float, float, float] | None = None,
) -> tuple[list[VisualMatch], dict]:
    params = {
        "engine": "yandex_images",
        "url": image_url,
        "api_key": api_key,
    }
    if bbox_norm is not None:
        params["crop"] = _crop_param(bbox_norm)

    r = requests.get(ENDPOINT, params=params, timeout=60)
    r.raise_for_status()
    data = r.json()

    matches: list[VisualMatch] = []
    for m in data.get("image_results", []):
        link = m.get("link")
        if not link:
            continue
        thumb = m.get("thumbnail")
        if isinstance(thumb, dict):
            thumb = thumb.get("link", "")
        matches.append(
            VisualMatch(
                title=m.get("title", ""),
                source=m.get("source", ""),
                source_url=link,
                thumbnail=thumb or "",
                engine="yandex_images",
            )
        )

    meta = {
        "id": data.get("search_metadata", {}).get("id"),
        "yandex_images_url": data.get("search_metadata", {}).get("yandex_images_url"),
        "crop": params.get("crop"),
    }
    return matches, meta
