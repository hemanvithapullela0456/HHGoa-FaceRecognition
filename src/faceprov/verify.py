"""Stage C — re-verify candidate images against the probe embedding."""
from __future__ import annotations

import hashlib
import tempfile
from dataclasses import dataclass, asdict
from pathlib import Path

import numpy as np
import requests

from .face import Face, detect_and_encode, cosine


@dataclass
class CandidateResult:
    source_url: str          # page the image was found on
    image_url: str           # direct image URL
    fetched_at: str
    image_sha256: str
    page_sha256: str | None
    best_cosine: float
    accepted: bool
    n_faces: int
    engine: str
    note: str = ""

    def as_leaf(self) -> dict:
        return asdict(self)


def _download(url: str, timeout: int = 20) -> bytes:
    r = requests.get(url, timeout=timeout, headers={"User-Agent": "faceprov/0.1"})
    r.raise_for_status()
    return r.content


def verify_candidate(
    probe: Face,
    *,
    image_url: str,
    source_url: str,
    engine: str,
    threshold: float,
    page_html: bytes | None = None,
    fetched_at: str,
) -> CandidateResult:
    img_bytes = _download(image_url)
    img_sha = "0x" + hashlib.sha256(img_bytes).hexdigest()
    page_sha = ("0x" + hashlib.sha256(page_html).hexdigest()) if page_html else None

    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tf:
        tf.write(img_bytes)
        tmp = tf.name
    try:
        faces = detect_and_encode(tmp)
    except Exception as e:  # noqa: BLE001
        return CandidateResult(
            source_url, image_url, fetched_at, img_sha, page_sha,
            best_cosine=-1.0, accepted=False, n_faces=0, engine=engine,
            note=f"decode/detect failed: {e}",
        )
    finally:
        Path(tmp).unlink(missing_ok=True)

    if not faces:
        return CandidateResult(
            source_url, image_url, fetched_at, img_sha, page_sha,
            best_cosine=-1.0, accepted=False, n_faces=0, engine=engine,
            note="no face in candidate image",
        )

    best = max(cosine(probe.embedding, f.embedding) for f in faces)
    return CandidateResult(
        source_url, image_url, fetched_at, img_sha, page_sha,
        best_cosine=round(best, 4),
        accepted=best >= threshold,
        n_faces=len(faces),
        engine=engine,
    )
