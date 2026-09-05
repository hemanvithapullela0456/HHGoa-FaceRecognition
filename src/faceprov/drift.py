"""Grading source drift on re-verification.

The reason this is not a boolean: a live news or social page rewrites its own bytes
on every single request - rotating ad slots, CSRF tokens, session ids, view counters,
build hashes in asset URLs. Comparing raw bytes therefore reports "changed" for
essentially every page ever attested, and an alarm that always fires tells a
journalist nothing. Worse, it buries the one case that matters.

What matters is whether the *claim* the page made about this image still holds - its
canonical URL, its og:image, its author, its title. Those move only when someone
edits the page's assertion about the picture, which is exactly the event this project
exists to catch. So drift is graded:

  ok    recomputed exactly, or only the churn tier moved
  info  changed, but this value is expected to change; carries no signal
  warn  changed in a way worth a human look, but not proof of tampering
  fail  the sealed provenance claim no longer holds
"""
from __future__ import annotations

import requests

from .evidence import sha256_bytes
from .search.harvest import harvest

UA = {"User-Agent": "faceprov/0.1"}


def check(name: str, level: str, **extra) -> dict:
    """One re-verification line. `ok` stays a plain bool so older consumers work."""
    return {"check": name, "level": level, "ok": level != "fail", **extra}


def claim_diff(sealed: dict, fresh: dict) -> dict:
    """Name the fields that moved, so a FAIL says what broke instead of just breaking."""
    keys = sealed.get("claim_keys") or []
    old, new = sealed.get("content") or {}, fresh.get("content") or {}
    return {k: {"was": old.get(k), "now": new.get(k)}
            for k in keys if old.get(k) != new.get(k)}


def recheck_image(i: int, c: dict, *, timeout: int = 20) -> dict:
    """Re-fetch a candidate image and grade the hash against the sealed one."""
    try:
        img = requests.get(c["image_url"], timeout=timeout, headers=UA).content
    except Exception as e:  # noqa: BLE001
        return check(f"candidate[{i}] image", "skip", url=c.get("image_url"), error=str(e))

    now_sha = sha256_bytes(img)
    same = now_sha == c.get("image_sha256")
    # A SerpApi thumbnail is the engine's own cache entry, re-encoded and rotated on
    # the engine's schedule, so a changed hash there is not evidence of anything. An
    # og:image served by the source itself is a real signal.
    origin = c.get("image_origin", "")
    level = "ok" if same else ("warn" if origin == "og:image" else "info")
    return check(f"candidate[{i}] image", level, url=c.get("image_url"),
                 origin=origin, was=c.get("image_sha256"), now=now_sha)


def recheck_page(i: int, c: dict, *, fetch=harvest) -> dict | None:
    """Re-fetch a candidate's source page and grade the three fingerprint tiers.

    Returns None for a v1 bundle, which sealed no fingerprint to compare against.
    """
    sealed = c.get("page")
    if not sealed:
        return None
    try:
        page = fetch(c["source_url"])
        if not page.html or page.fingerprint is None:
            return check(f"candidate[{i}] page", "skip", url=c.get("source_url"),
                         error=page.error or "no content")
        fresh = page.fingerprint.as_leaf()
    except Exception as e:  # noqa: BLE001
        return check(f"candidate[{i}] page", "skip", url=c.get("source_url"), error=str(e))

    claim_ok = fresh["claim_sha256"] == sealed.get("claim_sha256")
    content_ok = fresh["content_sha256"] == sealed.get("content_sha256")
    raw_ok = fresh["raw_sha256"] == sealed.get("raw_sha256")

    if not claim_ok:
        level, note = "fail", "the page's claim about this image changed"
    elif not content_ok:
        level, note = "warn", "page text changed; the image claim still holds"
    elif not raw_ok:
        level, note = "ok", "bytes differ, content identical (ads/tokens)"
    else:
        level, note = "ok", "byte-identical"

    entry = check(f"candidate[{i}] page", level, url=c.get("source_url"),
                  claim_ok=claim_ok, content_ok=content_ok, raw_ok=raw_ok, note=note)
    if not claim_ok:
        entry["changed_fields"] = claim_diff(sealed, fresh)
    return entry


def recheck_candidate(i: int, c: dict, *, fetch=harvest) -> list[dict]:
    """Both re-checks for one candidate: the image bytes and the source page."""
    out = [recheck_image(i, c)]
    page = recheck_page(i, c, fetch=fetch)
    if page is not None:
        out.append(page)
    return out


def verdict_of(checks: list[dict]) -> str:
    """FAIL beats warn beats pass. PASS_WITH_WARNINGS is still a pass."""
    levels = {c.get("level") for c in checks}
    if "fail" in levels:
        return "FAIL"
    return "PASS_WITH_WARNINGS" if "warn" in levels else "PASS"
