"""Page-fingerprint tests — pure, no network.

The thing under test is a claim about noise, so most of these assert that something
did *not* change: a page whose ads, tokens and counters churned must fingerprint
identically, or the tamper signal is worthless.
"""
from faceprov.content import (
    CLAIM_KEYS,
    fingerprint_page,
    image_key,
    normalize_url,
    recompute,
    visible_text,
)
from bs4 import BeautifulSoup


def _page(*, og="https://cdn.example.com/photo.jpg", title="Alice at the summit",
          body="Alice spoke at the summit on Tuesday.", ads="ad-slot-0001",
          csrf="tok-aaaa", views="1,204 views") -> bytes:
    return f"""
    <html><head>
      <title>{title}</title>
      <meta property="og:title" content="{title}">
      <meta property="og:image" content="{og}">
      <meta name="csrf-token" content="{csrf}">
      <link rel="canonical" href="https://example.com/news/alice">
    </head><body>
      <nav>Home About Contact</nav>
      <script>var adSlot = "{ads}"; track();</script>
      <article><p>{body}</p></article>
      <footer><span>{views}</span></footer>
    </body></html>
    """.encode()


def _fp(html: bytes):
    return fingerprint_page(
        html, "https://example.com/news/alice", status=200,
        title="Alice at the summit", description="A talk.",
        author_handle="@alice",
        og_image=BeautifulSoup(html, "lxml").find("meta", property="og:image")["content"],
    )


def test_ad_and_token_churn_does_not_move_content_or_claim():
    """The core claim: a page that only churned its plumbing must not read as changed."""
    a = _fp(_page(ads="ad-slot-0001", csrf="tok-aaaa"))
    b = _fp(_page(ads="ad-slot-9999", csrf="tok-zzzz"))

    assert a.raw_sha256 != b.raw_sha256, "bytes really did differ"
    assert a.content_sha256 == b.content_sha256
    assert a.claim_sha256 == b.claim_sha256


def test_changing_the_image_breaks_the_claim():
    a = _fp(_page())
    b = _fp(_page(og="https://cdn.example.com/someone-else.jpg"))
    assert a.claim_sha256 != b.claim_sha256
    assert a.content_sha256 != b.content_sha256


def test_body_edit_moves_content_but_not_the_claim():
    """A rewritten article is worth a warning; it is not a broken image claim."""
    a = _fp(_page())
    b = _fp(_page(body="Alice spoke at the summit on Wednesday, sources say."))
    assert a.content_sha256 != b.content_sha256
    assert a.claim_sha256 == b.claim_sha256


def test_expiring_cdn_signature_is_not_a_change():
    """Instagram/Facebook CDN URLs carry signatures that expire within hours."""
    a = _fp(_page(og="https://cdn.example.com/photo.jpg?oh=aaa&oe=1111&_nc_ht=x1"))
    b = _fp(_page(og="https://cdn.example.com/photo.jpg?oh=zzz&oe=9999&_nc_ht=x9"))
    assert a.claim_sha256 == b.claim_sha256


def test_leaf_recomputes_from_published_values():
    """A third party must be able to redo both digests from the bundle alone."""
    leaf = _fp(_page()).as_leaf()
    content_sha, claim_sha = recompute(leaf)
    assert content_sha == leaf["content_sha256"]
    assert claim_sha == leaf["claim_sha256"]


def test_recompute_honours_the_sealed_claim_definition():
    """An old bundle verifies against the claim keys it was sealed with, not today's."""
    leaf = _fp(_page()).as_leaf()
    leaf["claim_keys"] = ["title"]
    _, claim_sha = recompute(leaf)
    assert claim_sha != leaf["claim_sha256"]        # recomputed under the stored rule


def test_claim_keys_are_a_subset_of_content():
    content = _fp(_page()).content
    assert set(CLAIM_KEYS) <= set(content), "claim must never assert a field content lacks"


def test_normalize_url_strips_tracking_and_fragments():
    assert normalize_url("https://Example.com/a/?utm_source=x&id=7#frag") == \
        "https://example.com/a?id=7"
    assert normalize_url("https://example.com/a?fbclid=zz") == "https://example.com/a"
    assert normalize_url("https://example.com:443/a") == "https://example.com/a"
    assert normalize_url("") is None


def test_normalize_url_keeps_meaningful_query_params():
    assert normalize_url("https://example.com/p?id=7&v=2") == "https://example.com/p?id=7&v=2"


def test_image_key_drops_the_whole_query():
    assert image_key("https://cdn.x.com/p.jpg?oh=1&oe=2") == "https://cdn.x.com/p.jpg"


def test_visible_text_drops_chrome():
    text = visible_text(BeautifulSoup(_page(), "lxml"))
    assert "Alice spoke at the summit" in text
    assert "Home About Contact" not in text     # nav
    assert "adSlot" not in text                 # script


def test_fingerprint_survives_unparseable_bytes():
    """A fingerprint must never be the thing that breaks a run."""
    fp = fingerprint_page(b"\x00\xff not html at all", "https://example.com/x", status=200)
    assert fp.raw_sha256.startswith("0x")
    assert fp.content_sha256.startswith("0x")
