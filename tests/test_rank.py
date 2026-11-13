import json
from datetime import date

import httpx
import pytest
import respx

from rankwright import rank
from rankwright.config import Config, ConfigError
from rankwright.fetch import make_client

SITE = "https://x.example"
GSC_URL = ("https://www.googleapis.com/webmasters/v3/sites/"
           "sc-domain%3Ax.example/searchAnalytics/query")


def cfg(**rank_settings) -> Config:
    return Config({
        "site": {"name": "X", "url": SITE, "entityType": "Organization"},
        "rank": {"gscProperty": "sc-domain:x.example", **rank_settings},
        "competitors": ["rival.example"],
        "secrets": {"gsc": "env:GSC_ACCESS_TOKEN", "serpapi": "env:SERPAPI_KEY"},
    })


GSC_ROWS = {"rows": [
    {"keys": ["flight school", f"{SITE}/a"], "clicks": 5, "impressions": 100,
     "ctr": 0.05, "position": 7.36},
    {"keys": ["flight school", f"{SITE}/b"], "clicks": 1, "impressions": 20,
     "ctr": 0.05, "position": 14.0},
    {"keys": ["pilot lessons", f"{SITE}/a"], "clicks": 2, "impressions": 300,
     "ctr": 0.0066, "position": 12.04},
]}


def test_gsc_window_excludes_the_incomplete_latest_days():
    assert rank.gsc_window(28, date(2026, 11, 14)) == ("2026-10-15", "2026-11-11")


def test_gsc_token_needs_a_credential(monkeypatch):
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    bare = Config({"site": {"name": "X", "url": SITE, "entityType": "Organization"}})
    with pytest.raises(ConfigError, match="No Search Console credentials"):
        rank.gsc_token(bare)
    monkeypatch.setenv("GSC_ACCESS_TOKEN", "tok")
    assert rank.gsc_token(cfg()) == "tok"


def test_gsc_property_falls_back_to_the_url_prefix():
    bare = Config({"site": {"name": "X", "url": SITE, "entityType": "Organization"}})
    assert rank.gsc_property(bare) == f"{SITE}/"


@respx.mock
def test_gsc_top_queries_keep_the_page_with_most_impressions():
    route = respx.post(GSC_URL).mock(return_value=httpx.Response(200, json=GSC_ROWS))
    with make_client() as client:
        snap = rank.fetch_gsc(client, cfg(country="usa"), "tok",
                              today=date(2026, 11, 14))
    request = route.calls.last.request
    assert request.headers["authorization"] == "Bearer tok"
    body = json.loads(request.content)
    assert body["startDate"] == "2026-10-15" and body["endDate"] == "2026-11-11"
    assert body["dimensionFilterGroups"][0]["filters"][0]["expression"] == "usa"
    assert [(r.keyword, r.position, r.url) for r in snap.rows] == [
        ("pilot lessons", 12.0, f"{SITE}/a"), ("flight school", 7.4, f"{SITE}/a")]
    assert snap.window == {"start": "2026-10-15", "end": "2026-11-11"}


@respx.mock
def test_gsc_configured_keyword_without_impressions_has_no_position():
    respx.post(GSC_URL).mock(return_value=httpx.Response(200, json=GSC_ROWS))
    with make_client() as client:
        snap = rank.fetch_gsc(client, cfg(keywords=["Flight School", "helicopter tour"]),
                              "tok")
    assert [(r.keyword, r.position) for r in snap.rows] == [
        ("Flight School", 7.4), ("helicopter tour", None)]


@respx.mock
def test_gsc_error_is_reported_with_its_status():
    respx.post(GSC_URL).mock(return_value=httpx.Response(
        403, json={"error": {"message": "User does not have permission"}}))
    with make_client() as client, pytest.raises(ConfigError, match="HTTP 403.*permission"):
        rank.fetch_gsc(client, cfg(), "tok")


def _serp(start: int, links: list[str]) -> httpx.Response:
    return httpx.Response(200, json={"organic_results": [
        {"position": start + i + 1, "link": link} for i, link in enumerate(links)]})


@respx.mock
def test_serpapi_pages_until_the_site_and_competitors_are_found():
    filler = [f"https://site{i}.example/" for i in range(9)]
    pages = {0: [*filler, "https://www.rival.example/p"],
             10: ["https://blog.x.example/post", *filler]}
    route = respx.get(rank.SERPAPI_ENDPOINT).mock(
        side_effect=lambda request: _serp(int(request.url.params["start"]),
                                          pages[int(request.url.params["start"])]))
    with make_client() as client:
        snap = rank.fetch_serpapi(client, cfg(keywords=["flight school"], country="US"),
                                  "key")
    assert route.call_count == 2                       # stopped once both were found
    assert route.calls[0].request.url.params["gl"] == "us"
    row = snap.rows[0]
    assert (row.position, row.url) == (11, "https://blog.x.example/post")
    assert row.competitors == {"rival.example": 10}


@respx.mock
def test_serpapi_not_found_within_depth_is_none():
    respx.get(rank.SERPAPI_ENDPOINT).mock(
        side_effect=lambda request: _serp(int(request.url.params["start"]),
                                          ["https://nope.example/"] * 10))
    with make_client() as client:
        snap = rank.fetch_serpapi(client, cfg(keywords=["k"], depth=20), "key")
    assert snap.rows[0].position is None
    assert snap.rows[0].competitors == {"rival.example": None}


def test_serpapi_requires_keywords():
    with make_client() as client, pytest.raises(ConfigError, match="needs keywords"):
        rank.fetch_serpapi(client, cfg(), "key")


@respx.mock
def test_serpapi_failure_never_leaks_the_api_key():
    respx.get(rank.SERPAPI_ENDPOINT).mock(side_effect=httpx.ConnectError("boom"))
    with make_client() as client, pytest.raises(ConfigError) as info:
        rank.fetch_serpapi(client, cfg(keywords=["k"]), "SECRETKEY")
    assert "SECRETKEY" not in str(info.value)
    assert info.value.__cause__ is None


def test_similar_domain_is_not_a_match():
    assert rank._matches("https://www.x.example/a", "x.example")
    assert rank._matches("https://blog.x.example/a", "x.example")
    assert not rank._matches("https://notx.example/a", "x.example")


def snapshot(*rows) -> rank.Snapshot:
    return rank.Snapshot("serpapi", SITE, "2026-11-14T00:00:00Z",
                         [rank.Row(k, p) for k, p in rows])


def test_compare_statuses():
    previous = snapshot(("up", 8), ("down", 3), ("same", 5), ("lost", 9),
                        ("found", None), ("still-absent", None)).to_dict()
    current = snapshot(("up", 5), ("down", 6), ("same", 5), ("lost", None),
                       ("found", 12), ("still-absent", None), ("brand-new", 4))
    by_keyword = {r["keyword"]: r for r in rank.compare(current, previous)}
    assert (by_keyword["up"]["status"], by_keyword["up"]["change"]) == ("up", 3)
    assert (by_keyword["down"]["status"], by_keyword["down"]["change"]) == ("down", -3)
    assert by_keyword["same"]["status"] == "same"
    assert by_keyword["lost"]["status"] == "lost"
    assert by_keyword["found"]["status"] == "new"
    assert by_keyword["still-absent"]["status"] == "absent"
    assert by_keyword["brand-new"]["status"] == "new"
    assert all(r["status"] == "first-run" for r in rank.compare(current, None))


def test_history_is_per_provider_and_site(tmp_path):
    path = tmp_path / "rank" / "history.jsonl"
    assert rank.load_history(path) == []
    rank.append_history(path, snapshot(("k", 3)))
    other = rank.Snapshot("gsc", SITE, "t", [rank.Row("k", 9.0)])
    rank.append_history(path, other)
    history = rank.load_history(path)
    assert len(history) == 2
    assert rank.previous_snapshot(history, "serpapi", SITE)["rows"][0]["position"] == 3
    assert rank.previous_snapshot(history, "gsc", "https://else.example") is None


def test_table_shows_movement_and_competitors():
    current = rank.Snapshot("serpapi", SITE, "t", [
        rank.Row("flight school", 5, competitors={"rival.example": 2}),
        rank.Row("nope", None)])
    previous = snapshot(("flight school", 8), ("nope", None)).to_dict()
    text = rank.format_table(current, rank.compare(current, previous))
    assert "+3" in text and "vs rival.example 2" in text
    assert "—" in text and "absent" in text
