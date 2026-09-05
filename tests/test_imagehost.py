"""Probe-hosting tests — pure, network stubbed.

The failure these guard against actually happened on the first live run: catbox
answered a rejected upload with HTTP 200 and a well-formed URL that served 404
forever, both search engines fetched the dead link, and the pipeline attested a clean
NO_MATCH_FOUND. An infrastructure failure had become indistinguishable from a finding.
"""
from faceprov.search import imagehost


class FakeResponse:
    def __init__(self, status=200, ctype="image/jpeg", body=b"x" * 100):
        self.status_code = status
        self.headers = {"content-type": ctype}
        self.content = body


def _stub_get(monkeypatch, table):
    """Map url -> FakeResponse (or an Exception instance to raise)."""
    def fake_get(url, **kw):
        r = table.get(url, FakeResponse(404, "text/html", b"not found"))
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(imagehost.requests, "get", fake_get)


def _stub_hosts(monkeypatch, hosts):
    """hosts: list of (name, url_or_exception) tried in order."""
    def make(result):
        def upload(data, filename, timeout):
            if isinstance(result, Exception):
                raise result
            return result
        return upload

    monkeypatch.setattr(imagehost, "_HOSTS",
                        tuple((name, make(res)) for name, res in hosts))


def test_a_host_that_200s_then_404s_is_rejected(monkeypatch):
    """The catbox failure mode: a perfectly formed URL that serves nothing."""
    _stub_hosts(monkeypatch, [("catbox.moe", "https://files.catbox.moe/dead.jpg")])
    _stub_get(monkeypatch, {})          # every URL 404s
    h = imagehost.host_probe(b"imagebytes", fallback_url="https://gw/ipfs/cid")

    assert h.verified is False, "an unfetchable URL must never be reported as hosted"
    assert h.attempts[0]["ok"] is False
    assert "404" in h.attempts[0]["detail"]


def test_falls_through_to_the_next_host(monkeypatch):
    _stub_hosts(monkeypatch, [
        ("catbox.moe", "https://files.catbox.moe/dead.jpg"),
        ("uguu.se", "https://h.uguu.se/good.jpg"),
    ])
    _stub_get(monkeypatch, {"https://h.uguu.se/good.jpg": FakeResponse()})
    h = imagehost.host_probe(b"imagebytes", fallback_url="https://gw/ipfs/cid")

    assert h.verified is True
    assert h.host == "uguu.se"
    assert h.url == "https://h.uguu.se/good.jpg"
    assert [a["ok"] for a in h.attempts] == [False, True]


def test_upload_exception_moves_on(monkeypatch):
    _stub_hosts(monkeypatch, [
        ("catbox.moe", ConnectionError("refused")),
        ("uguu.se", "https://h.uguu.se/good.jpg"),
    ])
    _stub_get(monkeypatch, {"https://h.uguu.se/good.jpg": FakeResponse()})
    h = imagehost.host_probe(b"imagebytes", fallback_url="https://gw/ipfs/cid")

    assert h.verified is True and h.host == "uguu.se"
    assert "upload:" in h.attempts[0]["detail"]


def test_falls_back_to_ipfs_and_verifies_that_too(monkeypatch):
    _stub_hosts(monkeypatch, [("catbox.moe", "https://files.catbox.moe/dead.jpg")])
    _stub_get(monkeypatch, {"https://gw/ipfs/cid": FakeResponse()})
    h = imagehost.host_probe(b"imagebytes", fallback_url="https://gw/ipfs/cid")

    assert h.verified is True
    assert h.host == "ipfs-gateway(fallback)"


def test_everything_down_reports_unverified(monkeypatch):
    _stub_hosts(monkeypatch, [("catbox.moe", "https://files.catbox.moe/dead.jpg")])
    _stub_get(monkeypatch, {})
    h = imagehost.host_probe(b"imagebytes", fallback_url="https://gw/ipfs/cid")

    assert h.verified is False and h.usable is False
    assert h.attempts[-1]["host"] == "ipfs-gateway"


def test_html_error_page_is_not_an_image(monkeypatch):
    """A host serving its own error page still returns 200 - content-type catches it.

    tmpfiles.org does exactly this: 200 with text/html where the image should be.
    """
    _stub_get(monkeypatch, {"https://x/y.jpg": FakeResponse(200, "text/html", b"<html>")})
    ok, detail = imagehost.verify_public_image("https://x/y.jpg")
    assert ok is False
    assert "not an image" in detail


def test_size_mismatch_is_recorded_but_accepted(monkeypatch):
    """A transcoding host still gives the engines something fetchable."""
    _stub_get(monkeypatch, {"https://x/y.jpg": FakeResponse(body=b"z" * 50)})
    ok, detail = imagehost.verify_public_image("https://x/y.jpg", expect_bytes=100)
    assert ok is True
    assert "50B" in detail and "100B" in detail


def test_non_url_upload_response_is_rejected(monkeypatch):
    ok, detail = imagehost.verify_public_image("ERROR: file too large")
    assert ok is False
    assert "not a url" in detail


def test_leaf_carries_the_verification_flag(monkeypatch):
    _stub_hosts(monkeypatch, [("uguu.se", "https://h.uguu.se/good.jpg")])
    _stub_get(monkeypatch, {"https://h.uguu.se/good.jpg": FakeResponse()})
    leaf = imagehost.host_probe(b"x", fallback_url="https://gw/ipfs/cid").as_leaf()

    assert leaf["query_image_verified"] is True
    assert leaf["query_image_host"] == "uguu.se"
    assert "attempts" in leaf
