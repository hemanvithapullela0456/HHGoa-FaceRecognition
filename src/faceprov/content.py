"""Content-normalized page fingerprints.

A raw sha256 over page bytes is close to useless as a tamper signal. Real pages
change on every fetch — rotating ad slots, CSRF tokens, session ids, view counters,
A/B buckets, build hashes in asset URLs — so a byte hash reports "changed" almost
always, and an alarm that always fires carries no information.

So each harvested page gets three nested fingerprints, narrowest last:

  raw     sha256 of the exact bytes fetched. Advisory only; expected to drift.
  content the page's semantic body — canonical URL, title, description, author,
          og:image and the visible text with boilerplate stripped. Drift here means
          the page's substance changed.
  claim   only the assertion the page makes *about this image*: canonical URL,
          og:image, author handle, title. This is the provenance claim itself, and
          drift here is the signal that actually matters.

Re-verification reports all three, so it can say *what* changed instead of a binary.

Auditability rule: we hash exactly what we publish. Every value covered by a hash is
stored in the bundle alongside it, so a third party recomputes both digests straight
from the pinned evidence — no need to trust our extraction. `claim` is stored as a
key subset of `content` rather than a second copy, so the two can never disagree.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup

from .evidence import canonical_json

# Chrome/Firefox strip these before sharing; they carry no page identity.
_TRACKING_PARAMS = {
    "fbclid", "gclid", "dclid", "gbraid", "wbraid", "msclkid", "igshid", "igsh",
    "mc_cid", "mc_eid", "yclid", "_openstat", "ref", "ref_src", "ref_url",
    "source", "spm", "scm", "share_id", "si", "feature", "app",
}

# Structural chrome that changes constantly and says nothing about the content.
_BOILERPLATE_TAGS = (
    "script", "style", "noscript", "template", "svg", "canvas", "iframe",
    "nav", "header", "footer", "aside", "form", "button", "select", "option",
)

# The visible-text excerpt that goes into the bundle and into `content_sha256`.
# Capped so the pinned evidence stays small; `text_chars` records the true length so
# a change past the cut-off is still detectable.
TEXT_EXCERPT_CHARS = 2000

# Fields of `content` that make up the narrow provenance claim. Sorted, because the
# list itself is published in the bundle and must be byte-stable.
CLAIM_KEYS = ("author_handle", "canonical_url", "og_image_key", "title")

FINGERPRINT_VERSION = "faceprov/page-fingerprint/v1"


def _sha256(b: bytes) -> str:
    return "0x" + hashlib.sha256(b).hexdigest()


def normalize_url(url: str | None) -> str | None:
    """Canonical form of a URL: lowercase host, no fragment, no tracking params.

    Two URLs that differ only in the junk a share button appended are the same URL
    for provenance purposes, and must not read as a change.
    """
    if not url:
        return None
    try:
        u = urlparse(url.strip())
    except ValueError:
        return url.strip()
    if not u.scheme and not u.netloc:
        return url.strip()

    host = u.netloc.lower()
    for scheme, port in (("http", ":80"), ("https", ":443")):
        if u.scheme.lower() == scheme and host.endswith(port):
            host = host[: -len(port)]

    params = [(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True)
              if k.lower() not in _TRACKING_PARAMS and not k.lower().startswith("utm_")]
    path = u.path.rstrip("/") or "/"
    return urlunparse((u.scheme.lower(), host, path, "", urlencode(sorted(params)), ""))


def image_key(url: str | None) -> str | None:
    """Identity of an image URL, ignoring the query string entirely.

    CDN image URLs on Instagram/Facebook/X carry signed, *expiring* query parameters
    (`oh=`, `oe=`, `_nc_ht=`). Hashing those guarantees a false "image changed" on
    every re-verification. The path is what identifies the file; the signature is
    plumbing, so only the path is hashed. The full URL is still recorded, unhashed.
    """
    n = normalize_url(url)
    if not n:
        return None
    u = urlparse(n)
    return urlunparse((u.scheme, u.netloc, u.path, "", "", ""))


def visible_text(soup: BeautifulSoup) -> str:
    """Readable page text with structural chrome removed and whitespace collapsed."""
    body = soup.body or soup
    for tag in body.find_all(_BOILERPLATE_TAGS):
        tag.decompose()
    return re.sub(r"\s+", " ", body.get_text(" ", strip=True)).strip()


@dataclass(frozen=True)
class PageFingerprint:
    raw_sha256: str
    content_sha256: str
    claim_sha256: str
    content: dict
    og_image: str | None          # full URL, kept for reference; not hashed
    status: int

    def as_leaf(self) -> dict:
        """The bundle representation — everything needed to recompute both digests."""
        return {
            "version": FINGERPRINT_VERSION,
            "status": self.status,
            "raw_sha256": self.raw_sha256,
            "content_sha256": self.content_sha256,
            "claim_sha256": self.claim_sha256,
            "claim_keys": list(CLAIM_KEYS),
            "content": self.content,
            "og_image_full": self.og_image,
        }


def _claim_of(content: dict) -> dict:
    return {k: content.get(k) for k in CLAIM_KEYS}


def fingerprint_page(
    html: bytes,
    url: str,
    *,
    status: int = 0,
    title: str | None = None,
    description: str | None = None,
    author_handle: str | None = None,
    og_image: str | None = None,
) -> PageFingerprint:
    """Build the three-tier fingerprint for one fetched page.

    The metadata the harvester already extracted is passed in rather than re-parsed,
    so the bundle records the same values the pipeline actually acted on.
    """
    canonical = None
    text = ""
    try:
        soup = BeautifulSoup(html, "lxml")
        link = soup.find("link", rel=lambda v: v and "canonical" in (v if isinstance(v, list) else [v]))
        if link and link.get("href"):
            canonical = urljoin(url, link["href"])
        text = visible_text(soup)
    except Exception:  # noqa: BLE001 - a fingerprint must never break the pipeline
        pass

    content = {
        "canonical_url": normalize_url(canonical or url),
        "title": (title or "").strip() or None,
        "description": (description or "").strip() or None,
        "author_handle": author_handle,
        "og_image_key": image_key(og_image),
        "text_excerpt": text[:TEXT_EXCERPT_CHARS],
        "text_chars": len(text),
    }

    return PageFingerprint(
        raw_sha256=_sha256(html),
        content_sha256=_sha256(canonical_json(content)),
        claim_sha256=_sha256(canonical_json(_claim_of(content))),
        content=content,
        og_image=og_image,
        status=status,
    )


def recompute(leaf: dict) -> tuple[str, str]:
    """Recompute (content_sha256, claim_sha256) from a stored leaf.

    Uses the leaf's own `claim_keys`, so a bundle written by a future version with a
    different claim definition still verifies against the definition it was sealed
    with rather than today's.
    """
    content = leaf.get("content") or {}
    keys = leaf.get("claim_keys") or list(CLAIM_KEYS)
    claim = {k: content.get(k) for k in keys}
    return _sha256(canonical_json(content)), _sha256(canonical_json(claim))
