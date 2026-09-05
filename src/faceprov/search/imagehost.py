"""Temporary public hosting for the probe image, used only for the search step.

Reverse-image APIs need a URL their crawlers can fetch. The IPFS pin (Pinata) is the
tamper-evidence record, but public IPFS gateways are rate-limited and SerpApi's
Yandex engine in particular rejects them ("not publicly accessible"). So for the
*query* we push the probe to a plain image host that search engines fetch reliably.

**The upload must be verified, not assumed.** catbox.moe answers a rejected upload
with `HTTP 200` and a perfectly well-formed URL that then serves `404` forever. A host
that reports success while serving nothing is the worst failure mode this pipeline
has: both engines fetch a dead link, return zero matches, and the run records a clean
`NO_MATCH_FOUND` — an infrastructure failure wearing the costume of a finding. So every
candidate URL is fetched back and checked before it is handed to a search engine, and
the result records which host served the query and whether that check passed.

Free hosts are volatile by nature (they rate-limit, get abused, and shut down), so
this tries several and reports what actually worked rather than pinning one.
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field

import requests

UA = {"User-Agent": "Mozilla/5.0 (compatible; faceprov/0.1)"}


@dataclass
class ProbeHosting:
    """Where the probe was served from for the search, and whether that was checked."""

    url: str
    host: str
    verified: bool
    attempts: list[dict] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        """True when an engine can actually fetch this URL."""
        return self.verified

    def as_leaf(self) -> dict:
        return {"query_image_url": self.url, "query_image_host": self.host,
                "query_image_verified": self.verified, "attempts": self.attempts}


def _upload_uguu(data: bytes, filename: str, timeout: int) -> str:
    r = requests.post("https://uguu.se/upload?output=text",
                      files={"files[]": (filename, io.BytesIO(data), "image/jpeg")},
                      headers=UA, timeout=timeout)
    r.raise_for_status()
    return r.text.strip()


def _upload_catbox(data: bytes, filename: str, timeout: int) -> str:
    r = requests.post("https://catbox.moe/user/api.php",
                      data={"reqtype": "fileupload"},
                      files={"fileToUpload": (filename, io.BytesIO(data), "image/jpeg")},
                      timeout=timeout)
    r.raise_for_status()
    return r.text.strip()


def _upload_litterbox(data: bytes, filename: str, timeout: int) -> str:
    r = requests.post("https://litterbox.catbox.moe/resources/internals/api.php",
                      data={"reqtype": "fileupload", "time": "24h"},
                      files={"fileToUpload": (filename, io.BytesIO(data), "image/jpeg")},
                      timeout=timeout)
    r.raise_for_status()
    return r.text.strip()


# Ordered by observed reliability. uguu.se holds files for a few hours, which outlasts
# a pipeline run; the permanent copy of the probe is the IPFS pin, not this.
_HOSTS = (
    ("uguu.se", _upload_uguu),
    ("catbox.moe", _upload_catbox),
    ("litterbox.catbox.moe", _upload_litterbox),
)


def verify_public_image(url: str, *, expect_bytes: int | None = None,
                        timeout: int = 25) -> tuple[bool, str]:
    """Fetch a URL back and confirm it really serves the image. Never raises."""
    if not url.startswith("http"):
        return False, f"not a url: {url[:80]}"
    try:
        r = requests.get(url, timeout=timeout, headers=UA)
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"[:160]

    ctype = r.headers.get("content-type", "")
    if r.status_code != 200:
        return False, f"HTTP {r.status_code}"
    if not ctype.startswith("image"):
        return False, f"content-type {ctype[:40]!r}, not an image"
    if expect_bytes and len(r.content) != expect_bytes:
        # a truncating or transcoding host still gives the engines a fetchable image,
        # so this is worth recording but not worth rejecting the host over
        return True, f"served {len(r.content)}B, uploaded {expect_bytes}B"
    return True, "ok"


def host_probe(data: bytes, *, fallback_url: str,
               filename: str = "probe.jpg", timeout: int = 60) -> ProbeHosting:
    """Upload the probe somewhere a search crawler can fetch it, and prove it works.

    Falls through the host list, then to `fallback_url` (the IPFS gateway URL) — Lens
    can usually still fetch that even when Yandex refuses it. The returned
    `verified` flag is what callers must branch on: an unverified probe URL means a
    `NO_MATCH` result says nothing about the person in the image.
    """
    attempts: list[dict] = []
    for name, upload in _HOSTS:
        try:
            url = upload(data, filename, timeout)
        except Exception as e:  # noqa: BLE001
            attempts.append({"host": name, "ok": False, "detail": f"upload: {type(e).__name__}"})
            continue
        ok, detail = verify_public_image(url, expect_bytes=len(data))
        attempts.append({"host": name, "ok": ok, "detail": detail, "url": url if ok else None})
        if ok:
            return ProbeHosting(url, name, True, attempts)

    ok, detail = verify_public_image(fallback_url, expect_bytes=len(data))
    attempts.append({"host": "ipfs-gateway", "ok": ok, "detail": detail})
    return ProbeHosting(fallback_url, "ipfs-gateway(fallback)", ok, attempts)
