"""Temporary public hosting for the probe image, used only for the search step.

Reverse-image APIs need a URL their crawlers can fetch. The IPFS pin (Pinata) is the
tamper-evidence record, but public IPFS gateways are rate-limited and SerpApi's
Yandex engine in particular rejects them ("not publicly accessible"). So for the
*query* we push the probe to a plain image host that search engines fetch reliably.
"""
from __future__ import annotations

import io

import requests

CATBOX_API = "https://catbox.moe/user/api.php"


def upload_catbox(data: bytes, filename: str = "probe.jpg", timeout: int = 60) -> str:
    r = requests.post(
        CATBOX_API,
        data={"reqtype": "fileupload"},
        files={"fileToUpload": (filename, io.BytesIO(data), "image/jpeg")},
        timeout=timeout,
    )
    r.raise_for_status()
    url = r.text.strip()
    if not url.startswith("http"):
        raise RuntimeError(f"catbox upload failed: {url[:200]}")
    return url


def host_probe(data: bytes, *, fallback_url: str, filename: str = "probe.jpg") -> tuple[str, str]:
    """Return (query_url, host) — the URL to hand to search engines.

    Falls back to `fallback_url` (e.g. the IPFS gateway URL) if the host is down;
    Google Lens still works with that even when Yandex won't.
    """
    try:
        return upload_catbox(data, filename), "catbox.moe"
    except Exception:  # noqa: BLE001
        return fallback_url, "ipfs-gateway(fallback)"
