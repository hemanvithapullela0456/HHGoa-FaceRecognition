"""Earliest-appearance dating via the Internet Archive CDX API.

The question a fact-checker asks first about a circulating image is not "who is
this" but "how long has this been online?" — an image presented as this week's news
that the Wayback Machine already indexed in 2019 is debunked by the date alone,
without anyone having to argue about the face.

This adds two things the pipeline could not previously say:

  1. `first_capture` — the earliest date the Archive holds for a source page, which
     bounds how old the claim is.
  2. An *independent* third-party record of that page. Until now the only baseline
     for "did this page change?" was our own hash, taken by us, at attestation time.
     A verifier had to take our word for the before-state. The Archive's capture is
     a baseline nobody in this pipeline controls.

The CDX endpoint is a plain unauthenticated GET, so this needs no key and no new
dependency. It is strictly best-effort: the Archive rate-limits and goes down, and a
lookup failure records an error on the leaf and never fails the run.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests

from ..content import normalize_url

CDX_ENDPOINT = "https://web.archive.org/cdx/search/cdx"
SNAPSHOT_BASE = "https://web.archive.org/web"
UA = {"User-Agent": "faceprov/0.1 (+provenance research)"}

# CDX columns we ask for. `digest` is the Archive's own SHA1 of the captured body —
# recording it lets a later verifier tell "the Archive holds a different version now"
# apart from "the Archive holds the same version it always did".
_FIELDS = "timestamp,original,statuscode,digest"

# How many of the oldest captures to pull before giving up on finding a 200. Small,
# because the point is one cheap request rather than an exhaustive search.
_OLDEST_ROWS = 10

# Seconds to wait after a throttled request, multiplied by the attempt number.
_BACKOFF_S = 2.0


def _iso(ts: str) -> str | None:
    """CDX stamps are YYYYMMDDhhmmss in UTC."""
    try:
        return datetime.strptime(ts, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc).isoformat()
    except ValueError:
        return None


def _cdx(url: str, *, limit: int, timeout: float) -> list[list[str]]:
    """One CDX query. A positive `limit` takes from the oldest end, negative the newest.

    Deliberately unfiltered. Asking CDX for `filter=statuscode:200` with `limit=1`
    makes it scan forward from 2001 until a row passes, which on a URL with millions
    of captures (a news front page) reliably times out. Fetching the first few rows
    unfiltered returns immediately, and we pick the successful capture ourselves.
    """
    r = requests.get(
        CDX_ENDPOINT,
        params={"url": url, "output": "json", "fl": _FIELDS, "limit": str(limit)},
        headers=UA,
        timeout=timeout,
    )
    r.raise_for_status()
    rows = r.json()
    return rows[1:] if rows else []          # row 0 is the column header


def _first_ok(rows: list[list[str]]) -> list[str] | None:
    """Prefer the oldest capture that actually returned content."""
    for row in rows:
        if len(row) > 2 and row[2] == "200":
            return row
    return rows[0] if rows else None         # nothing clean; report what exists


def _row(row: list[str], url: str) -> dict:
    ts = row[0]
    return {
        "timestamp": ts,
        "datetime": _iso(ts),
        # recorded rather than filtered on, so a first capture that was a redirect or
        # a 404 is visible as such instead of silently becoming the headline date
        "statuscode": row[2] if len(row) > 2 else None,
        "archive_digest": row[3] if len(row) > 3 else None,
        "snapshot_url": f"{SNAPSHOT_BASE}/{ts}/{url}",
    }


def lookup(url: str, *, timeout: float = 20.0, retries: int = 2,
           with_last: bool = False) -> dict:
    """Archive facts for one URL. Never raises.

    The Archive throttles hard under concurrent load, and a transient miss silently
    costs us a real date — observed live: a page archived since 2023 came back as
    never-archived purely because the request was throttled. So failures back off and
    retry, and only an exhausted retry budget is recorded as an error.

    `with_last` is off by default because the newest-capture query doubles the number
    of requests for a field nothing depends on; the earliest capture is the signal.
    """
    rec: dict = {"url": url, "archived": False, "first_capture": None,
                 "last_capture": None, "error": None}
    for attempt in range(retries + 1):
        try:
            oldest = _first_ok(_cdx(url, limit=_OLDEST_ROWS, timeout=timeout))
            rec["error"] = None
            if not oldest:
                return rec              # genuinely never archived - not an error
            rec["archived"] = True
            rec["first_capture"] = _row(oldest, url)
            if with_last:
                try:
                    newest = _cdx(url, limit=-1, timeout=min(timeout, 10.0))
                    if newest:
                        rec["last_capture"] = _row(newest[-1], url)
                except Exception:  # noqa: BLE001 - the newest capture is a bonus
                    pass
            return rec
        except Exception as e:  # noqa: BLE001
            rec["error"] = f"{type(e).__name__}: {e}"
            if attempt < retries:
                time.sleep(_BACKOFF_S * (attempt + 1))
    return rec


def build_timeline(urls: list[str], *, max_urls: int = 8, timeout: float = 20.0,
                   workers: int = 2) -> dict:
    """Look up every distinct URL concurrently and derive the headline date.

    URLs are normalized first (dropping `utm_*`, `fbclid` and the tracking parameters
    engines bolt on) because the Archive indexes the real URL, not the one Yandex
    handed us — and because two URLs differing only in that junk are one page and
    should cost one lookup, not two.

    Order is preserved so `max_urls` keeps the candidates the router ranked highest,
    and the result is keyed by URL — canonical JSON sorts those keys, so the leaf is
    deterministic regardless of which lookup finished first. Concurrency is kept low
    on purpose: the Archive throttles, and a throttled lookup loses a real date.
    """
    seen: list[str] = []
    for raw in urls:
        u = normalize_url(raw)
        if u and u not in seen:
            seen.append(u)
    seen = seen[:max_urls]

    checked_at = datetime.now(timezone.utc).isoformat()
    if not seen:
        return {"source": "web.archive.org/cdx", "checked_at": checked_at,
                "pages": {}, "earliest_known_appearance": None, "earliest_url": None}

    with ThreadPoolExecutor(max_workers=min(workers, len(seen))) as pool:
        records = list(pool.map(lambda u: lookup(u, timeout=timeout), seen))

    pages = {r["url"]: r for r in records}

    # Only a capture that actually returned content dates anything. The Archive holds
    # 404s and redirects for URLs it probed before they existed - bbc.com/news has a
    # 1999 capture that is a 404 - and treating one of those as "first appearance"
    # would date an image to before its page was ever published.
    dated = [(c["datetime"], r["url"]) for r in records
             if (c := r.get("first_capture")) and c.get("datetime")
             and c.get("statuscode") == "200"]
    earliest = min(dated) if dated else (None, None)

    return {
        "source": "web.archive.org/cdx",
        "checked_at": checked_at,
        "pages": pages,
        "archived_count": sum(1 for r in records if r["archived"]),
        "dated_count": len(dated),
        "checked_count": len(records),
        "errors": sum(1 for r in records if r.get("error")),
        "earliest_known_appearance": earliest[0],
        "earliest_url": earliest[1],
    }
