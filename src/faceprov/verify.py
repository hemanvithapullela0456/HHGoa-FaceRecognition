"""Stage C — re-verify candidate images against the probe embedding."""
from __future__ import annotations

import hashlib
import tempfile
from dataclasses import dataclass, asdict
from pathlib import Path

import requests

from .face import Face, detect_and_encode, cosine

UA = {"User-Agent": "Mozilla/5.0 (compatible; faceprov/0.1)"}


@dataclass
class CandidateResult:
    source_url: str          # page the image was found on
    image_url: str           # direct image URL that was fetched + hashed
    image_origin: str        # "serpapi_thumbnail" | "og:image" | "page_img"
    engine: str              # "google_lens" | "yandex_images" | "profile"
    fetched_at: str
    image_sha256: str
    page_sha256: str | None
    best_cosine: float
    accepted: bool
    n_faces: int
    platform: str | None = None
    author_handle: str | None = None
    caption: str | None = None
    note: str = ""

    def as_leaf(self) -> dict:
        return asdict(self)


def _download(url: str, timeout: int = 20) -> bytes:
    r = requests.get(url, timeout=timeout, headers=UA)
    r.raise_for_status()
    return r.content


def verify_candidate(
    probe: Face,
    *,
    image_url: str,
    source_url: str,
    engine: str,
    image_origin: str,
    threshold: float,
    fetched_at: str,
    page_html: bytes | None = None,
    platform: str | None = None,
    author_handle: str | None = None,
    caption: str | None = None,
) -> CandidateResult:
    page_sha = ("0x" + hashlib.sha256(page_html).hexdigest()) if page_html else None
    base = dict(
        source_url=source_url, image_url=image_url, image_origin=image_origin,
        engine=engine, fetched_at=fetched_at, page_sha256=page_sha,
        platform=platform, author_handle=author_handle, caption=caption,
    )

    try:
        img_bytes = _download(image_url)
    except Exception as e:  # noqa: BLE001
        return CandidateResult(
            **base, image_sha256="0x", best_cosine=-1.0, accepted=False,
            n_faces=0, note=f"download failed: {e}",
        )

    img_sha = "0x" + hashlib.sha256(img_bytes).hexdigest()
    base["image_sha256"] = img_sha

    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tf:
        tf.write(img_bytes)
        tmp = tf.name
    try:
        faces = detect_and_encode(tmp)
    except Exception as e:  # noqa: BLE001
        return CandidateResult(**base, best_cosine=-1.0, accepted=False, n_faces=0,
                               note=f"decode/detect failed: {e}")
    finally:
        Path(tmp).unlink(missing_ok=True)

    if not faces:
        return CandidateResult(**base, best_cosine=-1.0, accepted=False, n_faces=0,
                               note="no face in candidate image")

    best = max(cosine(probe.embedding, f.embedding) for f in faces)
    return CandidateResult(
        **base,
        best_cosine=round(best, 4),
        accepted=best >= threshold,
        n_faces=len(faces),
    )
