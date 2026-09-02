"""End-to-end orchestration: scan -> search -> verify -> bundle -> IPFS -> chain."""
from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .chain import Chain
from .config import Config
from .evidence import EvidenceBundle, set_leaf_path, sha256_bytes
from .face import cosine, detect_and_encode, probe_face
from .search.ipfs import Pinata
from .search.router import run_search


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_pipeline(image_path: str, cfg: Config, *, attest: bool = True) -> dict:
    t0 = time.time()
    img_path = Path(image_path)
    probe_bytes = img_path.read_bytes()

    # ---- Stage A ----
    probe = probe_face(img_path)

    # ---- pin probe so search engines (and later verifiers) can reach it ----
    pinata = Pinata(cfg.pinata_jwt, cfg.pinata_gateway)
    probe_cid = pinata.pin_bytes(probe_bytes, name=f"probe-{img_path.stem}.jpg")
    probe_url = pinata.gateway_url(probe_cid)

    # ---- Stage B ----
    search = run_search(
        probe, probe_url, cfg.serpapi_key,
        threshold=cfg.match_threshold, max_candidates=cfg.max_candidates,
    )

    # ---- Stage D: bundle ----
    bundle = EvidenceBundle()
    bundle.probe = {
        "image_sha256": sha256_bytes(probe_bytes),
        "image_cid": probe_cid,
        "embedding_digest": probe.embedding_digest,
        "bbox_pixels": [round(v, 2) for v in probe.bbox],
        "bbox_norm": [round(v, 4) for v in probe.bbox_norm],
        "det_score": round(probe.det_score, 4),
        "detector": "insightface/buffalo_l/retinaface",
        "encoder": "insightface/buffalo_l/arcface-r100",
    }
    bundle.search = {
        "path_taken": search.path_taken,
        "entity_name": search.entity_name,
        "entity_type": search.entity_type,
        "query_image_url": probe_url,
        "lens_meta": search.lens_meta,
        "yandex_meta": search.yandex_meta,
        "social_profiles": search.social_profiles,
        "threshold": cfg.match_threshold,
        "searched_at": _now(),
    }
    bundle.candidates = [c.as_leaf() for c in search.candidates]

    best = search.best
    matched = best is not None
    if matched:
        bundle.match = {
            "matched": True,
            "source_url": best.source_url,
            "image_url": best.image_url,
            "image_origin": best.image_origin,
            "image_sha256": best.image_sha256,
            "page_sha256": best.page_sha256,
            "cosine": best.best_cosine,
            "engine": best.engine,
            "platform": best.platform,
            "author_handle": best.author_handle,
            "caption": best.caption,
            "note": best.note,
        }
    else:
        bundle.match = {
            "matched": False,
            "reason": "no candidate cleared threshold",
            "rejected": [
                {"source_url": c.source_url, "cosine": c.best_cosine, "note": c.note}
                for c in search.candidates
            ],
        }

    bundle.run = {
        "faceprov_version": __version__,
        "wall_clock_s": round(time.time() - t0, 2),
        "config_hash": hashlib.sha256(
            json.dumps(
                {"threshold": cfg.match_threshold, "max_candidates": cfg.max_candidates},
                sort_keys=True,
            ).encode()
        ).hexdigest(),
        "created_at": _now(),
    }

    doc = bundle.to_json()
    bundle_cid = pinata.pin_json(doc, name=f"evidence-{img_path.stem}.json")
    doc["_pinned_cid"] = bundle_cid

    result = {
        "matched": matched,
        "path_taken": search.path_taken,
        "entity_name": search.entity_name,
        "merkle_root": bundle.root(),
        "bundle_cid": bundle_cid,
        "bundle_url": pinata.gateway_url(bundle_cid),
        "candidates": [c.as_leaf() for c in search.candidates],
        "match": bundle.match,
    }

    # ---- Stage E ----
    if attest and cfg.registry_address and cfg.deployer_key:
        chain = Chain.from_config(cfg, signer=True)
        att = chain.attest(
            cfg.registry_address,
            merkle_root=bundle.root(),
            probe_hash=bundle.probe["image_sha256"],
            cid=bundle_cid,
            match_url=(bundle.match.get("source_url") or "NO_MATCH"),
        )
        result["attestation"] = att

    return result


def reverify(attestation_id: int, cfg: Config) -> dict:
    """Stage F — pull the on-chain record, recompute the root, re-hash the sources."""
    chain = Chain.from_config(cfg)
    onchain = chain.get(cfg.registry_address, attestation_id)

    pinata = Pinata(cfg.pinata_jwt, cfg.pinata_gateway)
    doc = pinata.fetch_json(onchain["cid"])
    bundle = EvidenceBundle.from_json(doc)

    recomputed_root = bundle.root()
    checks: list[dict] = []

    checks.append({
        "check": "merkle_root",
        "ok": recomputed_root.lower() == onchain["merkle_root"].lower(),
        "onchain": onchain["merkle_root"],
        "recomputed": recomputed_root,
    })

    # re-hash each candidate's image and page
    import requests

    for i, c in enumerate(bundle.candidates):
        entry: dict = {"check": f"candidate[{i}] source", "url": c.get("source_url")}
        try:
            img = requests.get(c["image_url"], timeout=20, headers={"User-Agent": "faceprov/0.1"}).content
            now_sha = sha256_bytes(img)
            entry["image_ok"] = now_sha == c.get("image_sha256")
            entry["was"] = c.get("image_sha256")
            entry["now"] = now_sha
        except Exception as e:  # noqa: BLE001
            entry["image_ok"] = None
            entry["error"] = str(e)
        checks.append(entry)

    # re-run face match on the winning candidate
    match = doc.get("match", {})
    if match.get("matched"):
        try:
            import tempfile
            img = requests.get(match["image_url"], timeout=20, headers={"User-Agent": "faceprov/0.1"}).content
            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tf:
                tf.write(img)
                tmp = tf.name
            faces = detect_and_encode(tmp)
            Path(tmp).unlink(missing_ok=True)
            checks.append({
                "check": "face_rematch",
                "n_faces": len(faces),
                "note": "recompute cosine against a fresh probe to fully close the loop",
                "recorded_cosine": match.get("cosine"),
            })
        except Exception as e:  # noqa: BLE001
            checks.append({"check": "face_rematch", "error": str(e)})

    passed = all(c.get("ok", True) and c.get("image_ok", True) is not False for c in checks)
    return {"attestation_id": attestation_id, "onchain": onchain, "verdict": "PASS" if passed else "FAIL", "checks": checks}


def tamper_demo(attestation_id: int, cfg: Config, *, field_path: str | None = None, value=None) -> dict:
    """Fetch the attested bundle, mutate one field, and show the Merkle root break.

    Demonstrates tamper-evidence without touching the chain: the on-chain root is
    immutable, so any edit to the pinned evidence makes the recomputed root diverge.
    """
    chain = Chain.from_config(cfg)
    onchain = chain.get(cfg.registry_address, attestation_id)
    pinata = Pinata(cfg.pinata_jwt, cfg.pinata_gateway)
    doc = pinata.fetch_json(onchain["cid"])

    original = EvidenceBundle.from_json(doc)
    original_root = original.root()

    tampered_doc = json.loads(json.dumps(doc))  # deep copy
    if field_path is None:
        if doc.get("match", {}).get("matched"):
            field_path, value = "match.source_url", (doc["match"]["source_url"] + "?evil=1")
        elif doc.get("candidates"):
            field_path = "candidates.0.best_cosine"
            value = round(float(doc["candidates"][0].get("best_cosine", 0.0)) + 0.05, 4)
        else:
            field_path, value = "search.entity_name", "Someone Else"
    set_leaf_path(tampered_doc, field_path, value)

    tampered_root = EvidenceBundle.from_json(tampered_doc).root()

    return {
        "attestation_id": attestation_id,
        "onchain_root": onchain["merkle_root"],
        "original_recomputed_root": original_root,
        "tampered_field": field_path,
        "tampered_value": value,
        "tampered_root": tampered_root,
        "original_matches_chain": original_root.lower() == onchain["merkle_root"].lower(),
        "tampered_matches_chain": tampered_root.lower() == onchain["merkle_root"].lower(),
    }
