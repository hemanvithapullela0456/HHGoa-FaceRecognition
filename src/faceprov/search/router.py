"""Entity router: Path A (entity resolved) vs Path B (visual union), + candidate fusion."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..face import Face
from ..verify import CandidateResult, verify_candidate
from . import lens, yandex
from .harvest import harvest


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class SearchOutcome:
    path_taken: str                      # "A:entity" | "B:visual"
    entity_name: str | None
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
        return max(self.accepted, key=lambda c: c.best_cosine)


def _dedupe(urls: list[str]) -> list[str]:
    seen, out = set(), []
    for u in urls:
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def run_search(
    probe: Face,
    probe_image_url: str,
    api_key: str,
    *,
    threshold: float,
    max_candidates: int,
) -> SearchOutcome:
    lens_res = lens.search(probe_image_url, api_key)
    y_matches, y_meta = yandex.search(probe_image_url, api_key, bbox_norm=probe.bbox_norm)

    outcome = SearchOutcome(
        path_taken="",
        entity_name=lens_res.entity_name,
        lens_meta=lens_res.raw_search_metadata,
        yandex_meta=y_meta,
        query_image_url=probe_image_url,
    )

    # ---- choose path ----
    target_pages: list[str] = []
    if lens_res.entity_name:
        outcome.path_taken = "A:entity"
        profiles = lens.find_social_profiles(lens_res.entity_name, api_key)
        outcome.social_profiles = profiles
        target_pages = [p["link"] for p in profiles]
        # still fold in the direct visual matches as extra candidates
        target_pages += [m.source_url for m in lens_res.matches]
        target_pages += [m.source_url for m in y_matches]
    else:
        outcome.path_taken = "B:visual"
        target_pages = [m.source_url for m in lens_res.matches] + [m.source_url for m in y_matches]

    target_pages = _dedupe(target_pages)[:max_candidates]

    engine_of = {m.source_url: m.engine for m in (*lens_res.matches, *y_matches)}

    for page_url in target_pages:
        page = harvest(page_url)
        if not page.candidate_images:
            continue
        for img_url in page.candidate_images[:3]:
            try:
                res = verify_candidate(
                    probe,
                    image_url=img_url,
                    source_url=page_url,
                    engine=engine_of.get(page_url, "profile" if outcome.path_taken.startswith("A") else "unknown"),
                    threshold=threshold,
                    page_html=page.html,
                    fetched_at=_now(),
                )
            except Exception as e:  # noqa: BLE001
                continue
            res.note = (res.note + f" platform={page.platform} handle={page.author_handle}").strip()
            outcome.candidates.append(res)
            if res.accepted:
                outcome.accepted.append(res)
        # early exit once we have a confident hit on a real social platform
        if outcome.accepted and page.platform:
            break

    return outcome
