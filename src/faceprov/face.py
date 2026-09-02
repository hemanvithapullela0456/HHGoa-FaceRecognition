"""Stage A — face detection & encoding via InsightFace (RetinaFace + ArcFace R100)."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np


@dataclass
class Face:
    bbox: tuple[float, float, float, float]      # x1, y1, x2, y2 (pixels)
    bbox_norm: tuple[float, float, float, float] # x1, y1, x2, y2 in [0,1]
    det_score: float
    embedding: np.ndarray                        # L2-normalized, 512-d
    landmarks: list[list[float]]

    @property
    def embedding_digest(self) -> str:
        q = np.round(self.embedding, 4).astype(np.float32)
        return "0x" + hashlib.sha256(q.tobytes()).hexdigest()


@lru_cache(maxsize=1)
def _model():
    from insightface.app import FaceAnalysis

    app = FaceAnalysis(name="buffalo_l", allowed_modules=["detection", "recognition"])
    app.prepare(ctx_id=-1, det_size=(640, 640))  # ctx_id=-1 => CPU
    return app


def _read_image(path: str | Path) -> np.ndarray:
    img = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError(f"could not read image: {path}")
    return img


def detect_and_encode(path: str | Path) -> list[Face]:
    img = _read_image(path)
    h, w = img.shape[:2]
    out: list[Face] = []
    for f in _model().get(img):
        x1, y1, x2, y2 = [float(v) for v in f.bbox]
        emb = np.asarray(f.normed_embedding, dtype=np.float32)
        out.append(
            Face(
                bbox=(x1, y1, x2, y2),
                bbox_norm=(
                    max(0.0, x1 / w), max(0.0, y1 / h),
                    min(1.0, x2 / w), min(1.0, y2 / h),
                ),
                det_score=float(f.det_score),
                embedding=emb,
                landmarks=[[float(a), float(b)] for a, b in np.asarray(f.kps)],
            )
        )
    return out


def probe_face(path: str | Path) -> Face:
    """The face we search for: largest area, tie-broken by detection score."""
    faces = detect_and_encode(path)
    if not faces:
        raise ValueError("no face detected in probe image")
    return max(
        faces,
        key=lambda f: ((f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]), f.det_score),
    )


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    a = a / (np.linalg.norm(a) + 1e-9)
    b = b / (np.linalg.norm(b) + 1e-9)
    return float(np.dot(a, b))
