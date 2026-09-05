"""Re-verification drift grading — pure, network stubbed.

These are the tests for the thing that was actually broken: a raw byte comparison
flags every live page, so the tamper signal has to distinguish innocent churn from a
changed provenance claim.
"""
import pytest

from faceprov import drift
from faceprov.content import fingerprint_page
from faceprov.search.harvest import HarvestedPage


def _html(*, og="https://cdn.example.com/photo.jpg", title="Alice at the summit",
          body="Alice spoke at the summit.", ads="slot-1", author="@alice") -> bytes:
    return f"""
    <html><head><title>{title}</title>
      <meta property="og:title" content="{title}">
      <meta property="og:image" content="{og}">
      <link rel="canonical" href="https://example.com/news/alice">
    </head><body>
      <script>var ad="{ads}";</script>
      <article><p>{body}</p></article>
    </body></html>""".encode()


def _fingerprint(html: bytes, *, og="https://cdn.example.com/photo.jpg",
                 title="Alice at the summit", author="@alice"):
    return fingerprint_page(html, "https://example.com/news/alice", status=200,
                            title=title, description="A talk.",
                            author_handle=author, og_image=og)


def _page(html: bytes, **kw) -> HarvestedPage:
    return HarvestedPage(
        url="https://example.com/news/alice", html=html, status=200, platform=None,
        author_handle=kw.get("author", "@alice"), og_image=kw.get("og"),
        title=kw.get("title"), description="A talk.",
        fingerprint=_fingerprint(html, **kw),
    )


def _candidate(**kw) -> dict:
    """A sealed candidate leaf, as the pipeline would have written it."""
    return {
        "source_url": "https://example.com/news/alice",
        "image_url": "https://cdn.example.com/photo.jpg",
        "image_origin": "og:image",
        "image_sha256": "0xdeadbeef",
        "page": _fingerprint(_html(), **kw).as_leaf(),
    }


def _fetch(html: bytes, **kw):
    return lambda url: _page(html, **kw)


# ---- the core regression: churn must not read as tampering ----

def test_ad_churn_passes():
    entry = drift.recheck_page(0, _candidate(), fetch=_fetch(_html(ads="slot-99999")))
    assert entry["level"] == "ok"
    assert entry["claim_ok"] and entry["content_ok"]
    assert entry["raw_ok"] is False, "the bytes really did change"
    assert "ads/tokens" in entry["note"]


def test_identical_page_is_byte_identical():
    entry = drift.recheck_page(0, _candidate(), fetch=_fetch(_html()))
    assert entry["level"] == "ok"
    assert entry["raw_ok"] is True


# ---- the signal that has to survive ----

def test_swapped_image_fails_and_says_what_changed():
    entry = drift.recheck_page(
        0, _candidate(),
        fetch=_fetch(_html(og="https://cdn.example.com/other.jpg"),
                     og="https://cdn.example.com/other.jpg"),
    )
    assert entry["level"] == "fail"
    assert entry["ok"] is False
    assert "og_image_key" in entry["changed_fields"]
    assert entry["changed_fields"]["og_image_key"]["now"].endswith("other.jpg")


def test_reattributed_author_fails():
    entry = drift.recheck_page(0, _candidate(), fetch=_fetch(_html(), author="@someone_else"))
    assert entry["level"] == "fail"
    assert "author_handle" in entry["changed_fields"]


def test_edited_body_warns_but_does_not_fail():
    entry = drift.recheck_page(
        0, _candidate(), fetch=_fetch(_html(body="Alice denied attending the summit.")))
    assert entry["level"] == "warn"
    assert entry["claim_ok"] is True and entry["content_ok"] is False


# ---- degradation ----

def test_v1_bundle_without_a_fingerprint_is_skipped():
    c = _candidate()
    del c["page"]
    assert drift.recheck_page(0, c, fetch=_fetch(_html())) is None


def test_unreachable_page_is_skipped_not_failed():
    """A login wall must not read the same as a doctored page."""
    def dead(url):
        return HarvestedPage(url, b"", 0, None, None, None, None, None, error="HTTP 403")

    entry = drift.recheck_page(0, _candidate(), fetch=dead)
    assert entry["level"] == "skip"
    assert entry["ok"] is True


def test_fetch_exception_is_skipped_not_failed():
    def boom(url):
        raise ConnectionError("dns failure")

    entry = drift.recheck_page(0, _candidate(), fetch=boom)
    assert entry["level"] == "skip"
    assert "dns failure" in entry["error"]


# ---- image tier ----

@pytest.mark.parametrize("origin,expected", [("og:image", "warn"), ("serpapi_thumbnail", "info")])
def test_changed_image_severity_depends_on_who_served_it(monkeypatch, origin, expected):
    """An engine rotating its own thumbnail cache is not evidence of anything."""
    class R:
        content = b"different bytes"

    monkeypatch.setattr(drift.requests, "get", lambda *a, **k: R())
    c = _candidate() | {"image_origin": origin}
    entry = drift.recheck_image(0, c)
    assert entry["level"] == expected


def test_unchanged_image_passes(monkeypatch):
    from faceprov.evidence import sha256_bytes

    class R:
        content = b"the attested bytes"

    monkeypatch.setattr(drift.requests, "get", lambda *a, **k: R())
    c = _candidate() | {"image_sha256": sha256_bytes(b"the attested bytes")}
    assert drift.recheck_image(0, c)["level"] == "ok"


# ---- verdict rollup ----

def test_verdict_rollup():
    assert drift.verdict_of([{"level": "ok"}, {"level": "info"}]) == "PASS"
    assert drift.verdict_of([{"level": "ok"}, {"level": "warn"}]) == "PASS_WITH_WARNINGS"
    assert drift.verdict_of([{"level": "warn"}, {"level": "fail"}]) == "FAIL"
    assert drift.verdict_of([{"level": "skip"}]) == "PASS"


def test_check_keeps_a_plain_ok_bool():
    """Older consumers read `ok`; only a fail may flip it false."""
    assert drift.check("x", "warn")["ok"] is True
    assert drift.check("x", "skip")["ok"] is True
    assert drift.check("x", "fail")["ok"] is False
