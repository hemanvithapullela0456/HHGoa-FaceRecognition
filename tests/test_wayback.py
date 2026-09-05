"""Wayback CDX tests — pure, network calls stubbed out."""
import faceprov.search.wayback as wb


def test_iso_parses_cdx_stamps():
    assert wb._iso("20190304120000") == "2019-03-04T12:00:00+00:00"
    assert wb._iso("garbage") is None


def test_row_builds_a_clickable_snapshot_url():
    row = wb._row(["20190304120000", "http://example.com/a", "200", "ABCDEF"],
                  "http://example.com/a")
    assert row["snapshot_url"] == \
        "https://web.archive.org/web/20190304120000/http://example.com/a"
    assert row["archive_digest"] == "ABCDEF"
    assert row["datetime"].startswith("2019-03-04")


def test_lookup_reports_first_and_last(monkeypatch):
    def fake_cdx(url, *, limit, timeout):
        ts = "20190304120000" if limit > 0 else "20240101000000"
        return [[ts, url, "200", "D" + str(limit)]]

    monkeypatch.setattr(wb, "_cdx", fake_cdx)
    rec = wb.lookup("http://example.com/a", with_last=True)
    assert rec["archived"] is True
    assert rec["first_capture"]["datetime"].startswith("2019-03-04")
    assert rec["last_capture"]["datetime"].startswith("2024-01-01")
    assert rec["error"] is None


def test_lookup_handles_a_never_archived_url(monkeypatch):
    monkeypatch.setattr(wb, "_cdx", lambda url, *, limit, timeout: [])
    rec = wb.lookup("http://example.com/never")
    assert rec["archived"] is False
    assert rec["first_capture"] is None
    assert rec["error"] is None


def test_lookup_never_raises(monkeypatch):
    """The Archive rate-limits and goes down; that must not fail a pipeline run."""
    def boom(url, *, limit, timeout):
        raise RuntimeError("429 Too Many Requests")

    monkeypatch.setattr(wb, "_cdx", boom)
    rec = wb.lookup("http://example.com/a")
    assert rec["archived"] is False
    assert "429" in rec["error"]


def test_build_timeline_picks_the_earliest_across_pages(monkeypatch):
    stamps = {
        "http://a.test/1": "20210101000000",
        "http://b.test/2": "20170615000000",     # the oldest
        "http://c.test/3": "20230101000000",
    }

    def fake_lookup(url, *, timeout):
        ts = stamps[url]
        return {"url": url, "archived": True, "error": None,
                "first_capture": wb._row([ts, url, "200", "X"], url),
                "last_capture": None}

    monkeypatch.setattr(wb, "lookup", fake_lookup)
    tl = wb.build_timeline(list(stamps))
    assert tl["earliest_url"] == "http://b.test/2"
    assert tl["earliest_known_appearance"].startswith("2017-06-15")
    assert tl["archived_count"] == 3
    assert set(tl["pages"]) == set(stamps)


def test_build_timeline_dedupes_and_caps(monkeypatch):
    seen = []

    def fake_lookup(url, *, timeout):
        seen.append(url)
        return {"url": url, "archived": False, "first_capture": None,
                "last_capture": None, "error": None}

    monkeypatch.setattr(wb, "lookup", fake_lookup)
    urls = ["http://a.test/1", "http://a.test/1", "http://b.test/2", "http://c.test/3"]
    wb.build_timeline(urls, max_urls=2)
    assert seen == ["http://a.test/1", "http://b.test/2"], "dedupe, keep router order, cap"


def test_build_timeline_with_no_urls():
    tl = wb.build_timeline([])
    assert tl["pages"] == {}
    assert tl["earliest_known_appearance"] is None


def test_build_timeline_survives_pages_with_no_captures(monkeypatch):
    monkeypatch.setattr(wb, "lookup", lambda url, *, timeout: {
        "url": url, "archived": False, "first_capture": None,
        "last_capture": None, "error": None})
    tl = wb.build_timeline(["http://a.test/1"])
    assert tl["earliest_known_appearance"] is None
    assert tl["archived_count"] == 0


def test_first_ok_prefers_a_successful_capture():
    """CDX is queried unfiltered for speed, so the 200 is picked here instead."""
    rows = [
        ["20010101000000", "u", "302", "A"],
        ["20020101000000", "u", "404", "B"],
        ["20030101000000", "u", "200", "C"],
    ]
    assert wb._first_ok(rows)[0] == "20030101000000"


def test_first_ok_falls_back_to_the_oldest_row():
    """A URL the Archive only ever failed to fetch still gets reported, with its status."""
    rows = [["20010101000000", "u", "404", "A"], ["20020101000000", "u", "500", "B"]]
    assert wb._first_ok(rows)[0] == "20010101000000"
    assert wb._first_ok([]) is None


def test_capture_records_the_status_code():
    row = wb._row(["20010101000000", "u", "404", "A"], "u")
    assert row["statuscode"] == "404"


def test_a_404_capture_never_becomes_the_headline_date(monkeypatch):
    """bbc.com/news really does have a 1999 capture that is a 404.

    Dating an image to a capture that returned nothing would place it before the page
    it appeared on ever existed, so only successful captures date anything.
    """
    def fake_lookup(url, *, timeout):
        status = "404" if "bbc" in url else "200"
        ts = "19990427000000" if "bbc" in url else "20200101000000"
        return {"url": url, "archived": True, "error": None, "last_capture": None,
                "first_capture": wb._row([ts, url, status, "X"], url)}

    monkeypatch.setattr(wb, "lookup", fake_lookup)
    tl = wb.build_timeline(["https://bbc.test/news", "https://other.test/p"])
    assert tl["earliest_url"] == "https://other.test/p"
    assert tl["earliest_known_appearance"].startswith("2020-01-01")
    assert tl["archived_count"] == 2      # both are archived...
    assert tl["dated_count"] == 1         # ...but only one dates anything


def test_no_successful_capture_means_no_date(monkeypatch):
    monkeypatch.setattr(wb, "lookup", lambda url, *, timeout: {
        "url": url, "archived": True, "error": None, "last_capture": None,
        "first_capture": wb._row(["19990427000000", url, "404", "X"], url)})
    tl = wb.build_timeline(["https://bbc.test/news"])
    assert tl["earliest_known_appearance"] is None
    assert tl["dated_count"] == 0


def test_lookup_retries_a_transient_failure(monkeypatch):
    calls = []

    def flaky(url, *, limit, timeout):
        calls.append(limit)
        if len(calls) == 1:
            raise TimeoutError("read timed out")
        return [["20200101000000", url, "200", "X"]]

    monkeypatch.setattr(wb, "_cdx", flaky)
    monkeypatch.setattr(wb.time, "sleep", lambda s: None)
    rec = wb.lookup("http://example.com/a")
    assert rec["archived"] is True
    assert rec["error"] is None, "a recovered lookup must not report an error"


def test_lookup_gives_up_after_retries(monkeypatch):
    def always(url, *, limit, timeout):
        raise TimeoutError("read timed out")

    monkeypatch.setattr(wb, "_cdx", always)
    monkeypatch.setattr(wb.time, "sleep", lambda s: None)
    rec = wb.lookup("http://example.com/a")
    assert rec["archived"] is False
    assert "read timed out" in rec["error"]


def test_build_timeline_normalizes_urls_before_dating(monkeypatch):
    """Engines bolt tracking params onto source URLs; the Archive indexes the real one.

    Observed live: Yandex returned a YouTube URL carrying
    `?utm_source=yandexsmartcamera`, which is not what the Archive has indexed.
    """
    asked = []

    def fake_lookup(url, *, timeout):
        asked.append(url)
        return {"url": url, "archived": False, "first_capture": None,
                "last_capture": None, "error": None}

    monkeypatch.setattr(wb, "lookup", fake_lookup)
    wb.build_timeline([
        "https://www.youtube.com/c/x/discussion?utm_medium=organic&utm_source=yandexsmartcamera",
        "https://www.youtube.com/c/x/discussion?fbclid=zz",     # same page, junk differs
    ])
    assert asked == ["https://www.youtube.com/c/x/discussion"], "normalize, then dedupe"


def test_lookup_skips_the_newest_capture_by_default(monkeypatch):
    """The newest-capture query doubles Archive load for a field nothing depends on."""
    limits = []

    def fake_cdx(url, *, limit, timeout):
        limits.append(limit)
        return [["20200101000000", url, "200", "X"]]

    monkeypatch.setattr(wb, "_cdx", fake_cdx)
    rec = wb.lookup("http://example.com/a")
    assert limits == [wb._OLDEST_ROWS], "one request, not two"
    assert rec["last_capture"] is None

    limits.clear()
    wb.lookup("http://example.com/a", with_last=True)
    assert limits == [wb._OLDEST_ROWS, -1]
