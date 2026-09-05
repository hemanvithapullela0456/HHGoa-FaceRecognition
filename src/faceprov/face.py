"""Stage A — face detection & encoding.

Backend: OpenCV's built-in **YuNet** detector + **SFace** recognizer (both ONNX,
shipped with `opencv-python`, no compiler / no extra deps — important on Windows).
SFace is an ArcFace-family model producing a 128-d embedding; identity match is a
cosine-similarity threshold (~0.36 per the OpenCV reference).

The model files (~37 MB total) are fetched once from the official `opencv_zoo`
repository into `~/.faceprov/models/`.
"""
from __future__ import annotations

import hashlib
import urllib.request
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np

MODEL_DIR = Path.home() / ".faceprov" / "models"
_ZOO = "https://github.com/opencv/opencv_zoo/raw/main/models"
# (filename, url, minimum plausible size) - the size is what makes a truncated
# download detectable instead of being cached and failing later in the ONNX parser.
_MODELS = {
    "detector": (
        "face_detection_yunet_2023mar.onnx",
        f"{_ZOO}/face_detection_yunet/face_detection_yunet_2023mar.onnx",
        200_000,          # ~232 KB
    ),
    "recognizer": (
        "face_recognition_sface_2021dec.onnx",
        f"{_ZOO}/face_recognition_sface/face_recognition_sface_2021dec.onnx",
        35_000_000,       # ~38.7 MB
    ),
}

DETECTOR_VERSION = "opencv/yunet-2023mar"
ENCODER_VERSION = "opencv/sface-2021dec"


def _ensure_model(kind: str) -> str:
    name, url, min_bytes = _MODELS[kind]
    dest = MODEL_DIR / name
    # Download to a sidecar and rename only once it is complete. urlretrieve writes
    # straight to the destination, so an interrupted download (a timeout, a dropped
    # connection) leaves a truncated file that is large enough to pass a size check
    # and poisons the cache: every later run then fails inside the ONNX parser with
    # no hint that the real problem is a half-written file.
    if not dest.exists() or dest.stat().st_size < min_bytes:
        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        print(f"[faceprov] downloading {name} ...")
        part = dest.with_suffix(".part")
        req = urllib.request.Request(url, headers={"User-Agent": "faceprov/0.1"})
        with urllib.request.urlopen(req, timeout=120) as r, open(part, "wb") as f:  # noqa: S310
            while chunk := r.read(1 << 20):
                f.write(chunk)
        if part.stat().st_size < min_bytes:
            part.unlink(missing_ok=True)
            raise RuntimeError(
                f"{name} download truncated ({part.stat().st_size} bytes); retry"
            )
        part.replace(dest)
    return str(dest)


@dataclass
class Face:
    bbox: tuple[float, float, float, float]       # x1, y1, x2, y2 (pixels)
    bbox_norm: tuple[float, float, float, float]  # x1, y1, x2, y2 in [0,1]
    det_score: float
    embedding: np.ndarray                         # L2-normalized, 128-d
    landmarks: list[list[float]]
    _row: np.ndarray | None = None                # raw YuNet row, for alignCrop

    @property
    def embedding_digest(self) -> str:
        q = np.round(self.embedding, 4).astype(np.float32)
        return "0x" + hashlib.sha256(q.tobytes()).hexdigest()


@lru_cache(maxsize=1)
def _detector() -> "cv2.FaceDetectorYN":
    return cv2.FaceDetectorYN.create(_ensure_model("detector"), "", (320, 320),
                                     score_threshold=0.6, nms_threshold=0.3, top_k=5000)


@lru_cache(maxsize=1)
def _recognizer() -> "cv2.FaceRecognizerSF":
    return cv2.FaceRecognizerSF.create(_ensure_model("recognizer"), "")


def _read_image(path: str | Path) -> np.ndarray:
    data = np.fromfile(str(path), dtype=np.uint8)      # unicode-safe on Windows
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError(f"could not read image: {path}")
    return img


def detect_and_encode(path: str | Path) -> list[Face]:
    img = _read_image(path)
    h, w = img.shape[:2]
    det = _detector()
    det.setInputSize((w, h))
    _, raw = det.detect(img)
    if raw is None:
        return []

    rec = _recognizer()
    out: list[Face] = []
    for row in raw:
        x, y, bw, bh = [float(v) for v in row[:4]]
        x1, y1, x2, y2 = x, y, x + bw, y + bh
        score = float(row[-1])
        try:
            aligned = rec.alignCrop(img, row)
            feat = rec.feature(aligned).flatten().astype(np.float32)
        except cv2.error:
            continue
        feat = feat / (np.linalg.norm(feat) + 1e-9)
        out.append(
            Face(
                bbox=(x1, y1, x2, y2),
                bbox_norm=(
                    max(0.0, x1 / w), max(0.0, y1 / h),
                    min(1.0, x2 / w), min(1.0, y2 / h),
                ),
                det_score=score,
                embedding=feat,
                landmarks=[[float(row[4 + 2 * i]), float(row[5 + 2 * i])] for i in range(5)],
                _row=row,
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
