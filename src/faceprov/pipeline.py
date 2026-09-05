"""End-to-end orchestration: scan -> search -> verify -> bundle -> IPFS -> chain."""
from __future__ import annotations

import hashlib
import json
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import requests

from . import __version__
from . import face as face_mod
from .chain import Chain
from .config import Config
from .drift import check as _check
from .drift import recheck_candidate, verdict_of
from .evidence import EvidenceBundle, set_leaf_path, sha256_bytes
from .face import cosine, detect_and_encode, probe_face
from .search import wayback
from .search.imagehost import host_probe
from .search.ipfs import Pinata
from .search.router import run_search

Progress = Callable[[str, str], None]


def _noop(stage: str, detail: str) -> None:  # default progress sink
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_pipeline(
    image_path: str, cfg: Config, *, attest: bool = True, progress: Progress | None = None
) -> dict:
    p = progress or _noop
    t0 = time.time()
    img_path = Path(image_path)
    probe_bytes = img_path.read_bytes()

    # ---- Stage A ----
    p("detect", "detecting and encoding the face (YuNet + SFace)")
    probe = probe_face(img_path)
    p("detect", f"face found - det_score {probe.det_score:.2f}, 128-d embedding")

    # ---- pin probe to IPFS (the tamper-evidence record) ----
    p("pin_probe", "pinning the probe image to IPFS (Pinata)")
    pinata = Pinata(cfg.pinata_jwt, cfg.pinata_gateway)
    probe_cid = pinata.pin_bytes(probe_bytes, name=f"probe-{img_path.stem}.jpg")
    ipfs_url = pinata.gateway_url(probe_cid)
    p("pin_probe", f"probe CID {probe_cid}")

    # ---- host the probe where search-engine crawlers can fetch it ----
    hosting = host_probe(probe_bytes, fallback_url=ipfs_url,
                         filename=f"probe-{img_path.stem}.jpg")
    query_url = hosting.url
    if hosting.verified:
        p("host_probe", f"probe hosted for search via {hosting.host} (fetch verified)")
    else:
        # Every engine is about to fetch a URL that does not serve an image, so the
        # search cannot find anything. Say so now rather than letting the run end in
        # a NO_MATCH that looks like a finding.
        p("host_probe", f"WARNING: probe URL is not publicly fetchable ({hosting.host}) - "
                        "search results will be empty and mean nothing")

    # ---- Stage B ----
    p("search", "reverse-image search: Google Lens + Yandex Images")
    search = run_search(
        probe, query_url, cfg.serpapi_key,
        threshold=cfg.match_threshold, max_candidates=cfg.max_candidates,
        progress=p,
    )
    p("search", f"path {search.path_taken} - {len(search.candidates)} candidates, "
                f"{len(search.accepted)} above threshold")

    # ---- Stage B2: earliest-appearance dating ----
    # "When did this image first appear online?" is the question that actually settles
    # a miscaptioning dispute, and it is answered by a third party we do not control.
    timeline: dict = {}
    if cfg.wayback_enabled:
        p("timeline", "dating source pages against the Internet Archive")
        urls = ([search.best.source_url] if search.best else []) + \
               [c.source_url for c in search.candidates]
        timeline = wayback.build_timeline(
            urls, max_urls=cfg.wayback_max_urls, timeout=cfg.wayback_timeout,
        )
        first = timeline.get("earliest_known_appearance")
        p("timeline", f"earliest archived appearance: {first[:10]}" if first
                      else "no archived capture found for any source page")

    # ---- Stage D: bundle ----
    bundle = EvidenceBundle()
    bundle.probe = {
        "image_sha256": sha256_bytes(probe_bytes),
        "image_cid": probe_cid,
        "image_ipfs_url": ipfs_url,
        "embedding_digest": probe.embedding_digest,
        "bbox_pixels": [round(v, 2) for v in probe.bbox],
        "bbox_norm": [round(v, 4) for v in probe.bbox_norm],
        "det_score": round(probe.det_score, 4),
        "detector": face_mod.DETECTOR_VERSION,
        "encoder": face_mod.ENCODER_VERSION,
    }
    bundle.search = {
        "path_taken": search.path_taken,
        "entity_name": search.entity_name,
        "entity_type": search.entity_type,
        "query_image_url": query_url,
        "query_image_host": hosting.host,
        # whether an engine could actually fetch the probe. A search leaf without this
        # cannot be told apart from one where the engines saw a 404.
        "query_image_verified": hosting.verified,
        "query_image_host_attempts": hosting.attempts,
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
            "page": best.page,
            "cosine": best.best_cosine,
            "engine": best.engine,
            "platform": best.platform,
            "author_handle": best.author_handle,
            "caption": best.caption,
            "note": best.note,
        }
    elif not hosting.verified and not search.candidates:
        # Distinguish "we looked and found nothing" from "we were never able to look".
        bundle.match = {
            "matched": False,
            "reason": "probe image was not publicly fetchable, so no engine could see "
                      "it - this is an infrastructure failure, not a finding about the "
                      "person in the image",
            "probe_publicly_fetchable": False,
            "rejected": [],
        }
    else:
        bundle.match = {
            "matched": False,
            "reason": "no candidate cleared threshold",
            "probe_publicly_fetchable": hosting.verified,
            "rejected": [
                {"source_url": c.source_url, "cosine": c.best_cosine, "note": c.note}
                for c in search.candidates
            ],
        }

    bundle.timeline = timeline
    bundle.run = {
        "faceprov_version": __version__,
        "wall_clock_s": round(time.time() - t0, 2),
        "config_hash": hashlib.sha256(
            json.dumps(
                {"threshold": cfg.match_threshold, "max_candidates": cfg.max_candidates,
                 "wayback": cfg.wayback_enabled, "wayback_max_urls": cfg.wayback_max_urls},
                sort_keys=True,
            ).encode()
        ).hexdigest(),
        "created_at": _now(),
    }

    p("bundle", f"built evidence bundle - Merkle root {bundle.root()[:18]}...")
    doc = bundle.to_json()
    bundle_cid = pinata.pin_json(doc, name=f"evidence-{img_path.stem}.json")
    doc["_pinned_cid"] = bundle_cid
    p("pin_bundle", f"evidence bundle pinned to IPFS - CID {bundle_cid}")

    result = {
        "matched": matched,
        "path_taken": search.path_taken,
        "entity_name": search.entity_name,
        "merkle_root": bundle.root(),
        "bundle_cid": bundle_cid,
        "bundle_url": pinata.gateway_url(bundle_cid),
        "candidates": [c.as_leaf() for c in search.candidates],
        "match": bundle.match,
        "timeline": timeline,
        "earliest_known_appearance": timeline.get("earliest_known_appearance"),
        "probe_publicly_fetchable": hosting.verified,
        "query_image_host": hosting.host,
    }

    # ---- Stage E ----
    if attest and cfg.registry_address and cfg.deployer_key:
        p("attest", f"writing attestation on-chain (chain id {cfg.chain_id})")
        chain = Chain.from_config(cfg, signer=True)
        att = chain.attest(
            cfg.registry_address,
            merkle_root=bundle.root(),
            probe_hash=bundle.probe["image_sha256"],
            cid=bundle_cid,
            match_url=(bundle.match.get("source_url") or "NO_MATCH"),
        )
        result["attestation"] = att
        p("attest", f"attestation #{att['id']} - tx {att['tx']}")
    elif attest:
        p("attest", "skipped - no registry address / deployer key configured")

    p("done", f"complete in {round(time.time() - t0, 1)}s")
    return result


def reverify(attestation_id: int, cfg: Config) -> dict:
    """Stage F - pull the on-chain record, recompute the root, re-check the sources.

    Drift is graded rather than treated as binary. A news or social page rewrites its
    own bytes on every request (ad slots, CSRF tokens, session ids, view counters), so
    a raw byte comparison flags essentially every page and the alarm stops carrying
    information. What matters is whether the *claim* the page made about this image -
    its canonical URL, its og:image, its author, its title - still holds. That is
    graded `fail`; a body-text edit is `warn`; churning bytes are `info`.
    """
    chain = Chain.from_config(cfg)
    onchain = chain.get(cfg.registry_address, attestation_id)

    pinata = Pinata(cfg.pinata_jwt, cfg.pinata_gateway)
    doc = pinata.fetch_json(onchain["cid"])
    bundle = EvidenceBundle.from_json(doc)

    recomputed_root = bundle.root()
    root_ok = recomputed_root.lower() == onchain["merkle_root"].lower()
    checks: list[dict] = [_check(
        "merkle_root", "ok" if root_ok else "fail",
        onchain=onchain["merkle_root"], recomputed=recomputed_root,
    )]

    for i, c in enumerate(bundle.candidates):
        checks.extend(recheck_candidate(i, c))

    # The Archive is the one baseline in this record that we do not control. A capture
    # predating our run bounds the claim's age independently of anything we asserted.
    timeline = doc.get("timeline") or {}
    if timeline.get("earliest_known_appearance"):
        checks.append(_check(
            "earliest_appearance", "info",
            first_seen=timeline["earliest_known_appearance"],
            url=timeline.get("earliest_url"),
            archived=f"{timeline.get('archived_count')}/{timeline.get('checked_count')} pages",
        ))

    match = doc.get("match", {})
    if match.get("matched"):
        checks.append(_reverify_match_face(match, bundle, pinata))

    return {"attestation_id": attestation_id, "onchain": onchain,
            "verdict": verdict_of(checks), "checks": checks}


def _reverify_match_face(match: dict, bundle: EvidenceBundle, pinata: Pinata) -> dict:
    """Re-run SFace on the winning candidate and compare against the sealed probe.

    The bundle records the probe's embedding *digest*, not the vector, so the honest
    way to close the loop is to re-fetch the pinned probe image from IPFS, re-embed
    it, and recompute the cosine. The probe is fetched **by CID** through the gateway
    fallback rather than by the single URL recorded in the bundle: that URL names one
    gateway, and one unreachable gateway must not silently skip the only check that
    re-proves the match.
    """
    ua = {"User-Agent": "faceprov/0.1"}
    try:
        faces = _faces_from_url(match["image_url"], ua, timeout=20)
        if not faces:
            return _check("face_rematch", "warn", n_faces=0,
                          note="no face detected in the candidate image now",
                          recorded_cosine=match.get("cosine"))

        probe = bundle.probe or {}
        pfaces = None
        if probe.get("image_cid"):
            pfaces = _faces_from_bytes(pinata.fetch_bytes(probe["image_cid"]))
        elif probe.get("image_ipfs_url"):
            pfaces = _faces_from_url(probe["image_ipfs_url"], ua, timeout=30)
        else:
            return _check("face_rematch", "info", n_faces=len(faces),
                          recorded_cosine=match.get("cosine"),
                          note="probe image absent from bundle; face count only")

        if not pfaces:
            return _check("face_rematch", "info", n_faces=len(faces),
                          recorded_cosine=match.get("cosine"),
                          note="probe re-fetched but no face detected in it")

        probe_vec = max(pfaces, key=lambda f: f.det_score).embedding
        now_cos = round(max(cosine(probe_vec, f.embedding) for f in faces), 4)
        recorded = match.get("cosine")
        drift = abs(now_cos - recorded) if isinstance(recorded, (int, float)) else None
        # The same two images through the same model must give the same score, so a
        # real gap means the served image is not the one that was attested.
        level = "ok" if (drift is not None and drift <= 0.02) else "warn"
        return _check("face_rematch", level, n_faces=len(faces),
                      recorded_cosine=recorded, recomputed_cosine=now_cos,
                      drift=None if drift is None else round(drift, 4))
    except Exception as e:  # noqa: BLE001
        return _check("face_rematch", "skip", error=str(e))


def _faces_from_url(url: str, ua: dict, *, timeout: int) -> list:
    """Download an image and run detection on it."""
    return _faces_from_bytes(requests.get(url, timeout=timeout, headers=ua).content)


def _faces_from_bytes(img: bytes) -> list:
    """Detect + encode from raw bytes, always cleaning up the temp file.

    OpenCV's reader takes a path, so the bytes have to land on disk briefly.
    """
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tf:
        tf.write(img)
        tmp = tf.name
    try:
        return detect_and_encode(tmp)
    finally:
        Path(tmp).unlink(missing_ok=True)


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
