"""Entity router: Path A (entity resolved) vs Path B (visual union), + candidate fusion.

The SerpApi-provided thumbnail is the primary candidate image for every visual match
(it is a cached copy the engine will actually serve us, unlike a logged-out social
page). We additionally harvest each source page best-effort to record its HTML hash,
author handle, and caption, and to pull an `og:image` as a second candidate.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..face import Face
from ..verify import CandidateResult, verify_candidate
from . import lens, yandex
from .harvest import HarvestedPage, harvest, platform_of


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class CandidateSpec:
    image_url: str
    source_url: str
    engine: str
    image_origin: str


@dataclass
class SearchOutcome:
    path_taken: str                      # "A:entity" | "B:visual"
    entity_name: str | None
    entity_type: str | None
    lens_meta: dict
    yandex_meta: dict
    social_profiles: list[dict] = field(default_factory=list)
    candidates: list[CandidateResult] = field(default_factory=list)
    accepted: list[CandidateResult] = field(default_factory=list)
    query_image_url: str = ""

    @property
    def best(self) -> CandidateResult | None:
        if not self.accepted:
            return None
        # prefer an accepted candidate that sits on a real social platform;
        # among equals, the highest cosine wins.
        return max(self.accepted, key=lambda c: (c.platform is not None, c.best_cosine))


def _interleave(a: list, b: list) -> list:
    out = []
    for x, y in zip(a, b):
        out += [x, y]
    out += a[len(b):] if len(a) > len(b) else b[len(a):]
    return out


def _dedupe_specs(specs: list[CandidateSpec]) -> list[CandidateSpec]:
    seen, out = set(), []
    for s in specs:
        key = (s.image_url, s.source_url)
        if s.image_url and key not in seen:
            seen.add(key)
            out.append(s)
    return out


def run_search(
    probe: Face,
    probe_image_url: str,
    api_key: str,
    *,
    threshold: float,
    max_candidates: int,
    progress=None,
) -> SearchOutcome:
    def p(stage: str, detail: str) -> None:
        if progress:
            progress(stage, detail)

    lens_res = lens.search(probe_image_url, api_key)
    p("search", f"Google Lens: {len(lens_res.matches)} visual matches"
                + (f", entity '{lens_res.entity_name}'" if lens_res.entity_name else ", no entity"))
    y_matches, y_meta = yandex.search(probe_image_url, api_key, bbox_norm=probe.bbox_norm)
    p("search", f"Yandex Images: {len(y_matches)} visual matches")

    outcome = SearchOutcome(
        path_taken="A:entity" if lens_res.entity_name else "B:visual",
        entity_name=lens_res.entity_name,
        entity_type=lens_res.entity_type,
        lens_meta=lens_res.raw_search_metadata,
        yandex_meta=y_meta,
        query_image_url=probe_image_url,
    )

    # interleave the two engines so a long Lens list can't starve Yandex of budget
    visual = _interleave(lens_res.matches, y_matches)

    # ---- build the ordered candidate spec list ----
    specs: list[CandidateSpec] = []

    if lens_res.entity_name:
        profiles = lens.find_social_profiles(lens_res.entity_name, api_key)
        outcome.social_profiles = profiles
        p("search", f"Path A - {len(profiles)} candidate social profiles for '{lens_res.entity_name}'")
        for prof in profiles:
            specs.append(CandidateSpec(image_url="", source_url=prof["link"],
                                       engine="profile", image_origin="og:image"))

    for m in visual:
        if m.thumbnail:
            specs.append(CandidateSpec(m.thumbnail, m.source_url, m.engine, "serpapi_thumbnail"))
        specs.append(CandidateSpec(image_url="", source_url=m.source_url,
                                   engine=m.engine, image_origin="og:image"))

    specs = _dedupe_specs(specs)[: max_candidates * 2]

    # ---- verify, harvesting each page at most once ----
    page_cache: dict[str, HarvestedPage] = {}
    verified = 0

    for spec in specs:
        if verified >= max_candidates:
            break

        page = page_cache.get(spec.source_url)
        if page is None:
            page = harvest(spec.source_url)
            page_cache[spec.source_url] = page

        image_url = spec.image_url
        if not image_url:                       # og:image spec - needs the harvest
            image_url = page.og_image
        if not image_url:
            continue

        try:
            res = verify_candidate(
                probe,
                image_url=image_url,
                source_url=spec.source_url,
                engine=spec.engine,
                image_origin=spec.image_origin,
                threshold=threshold,
                fetched_at=_now(),
                page_html=page.html or None,
                page_fingerprint=page.fingerprint,
                platform=page.platform or platform_of(spec.source_url),
                author_handle=page.author_handle,
                caption=page.description,
            )
        except Exception:  # noqa: BLE001
            continue

        verified += 1
        outcome.candidates.append(res)
        mark = "accept" if res.accepted else "reject"
        p("verify", f"[{mark}] cos {res.best_cosine:+.3f}  {res.engine}  "
                    f"{(res.platform or '') and res.platform + '  '}{res.source_url[:60]}")
        if res.accepted:
            outcome.accepted.append(res)
            if res.platform:                    # confident hit on a real platform
                break

    return outcome
