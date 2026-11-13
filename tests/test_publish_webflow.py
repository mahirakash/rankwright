import json

import httpx
import pytest
import respx

from rankwright import publish_webflow as wf
from rankwright.config import Config, ConfigError

SITE = "https://x.example"
API = wf.API
SID = "site1"


def cfg() -> Config:
    return Config({"site": {"name": "X", "url": SITE, "entityType": "Organization"},
                   "publish": {"webflow": {"siteId": SID}},
                   "secrets": {"webflow": "env:WEBFLOW_TOKEN"}})


@pytest.fixture(autouse=True)
def token(monkeypatch):
    monkeypatch.setenv("WEBFLOW_TOKEN", "wf-secret")


def wf_page(pid, title="", description="", **extra):
    return {"id": pid, "title": f"Page {pid}", "slug": pid,
            "publishedPath": f"/{pid}", "seo": {"title": title, "description": description},
            **extra}


PAGES = [wf_page("p1", "", "Fine description."),
         wf_page("p2", "T" * 70, ""),
         wf_page("p3", "Good", "Good"),
         wf_page("tpl", "{{name}}", "", collectionId="c1"),
         wf_page("old", archived=True)]


def mock_pages():
    return respx.get(f"{API}/sites/{SID}/pages").mock(return_value=httpx.Response(
        200, json={"pages": PAGES, "pagination": {"limit": 100, "offset": 0,
                                                  "total": len(PAGES)}}))


def approved_plan(tmp_path, **entry_overrides):
    with wf.make_client(cfg()) as client:
        plan = wf.build_plan(cfg(), client)
    entry = plan["entries"][0]
    entry["proposed"] = {"seoTitle": "New title", "seoDescription": None}
    entry["approved"] = True
    entry.update(entry_overrides)
    return plan


def test_site_id_is_required():
    bare = Config({"site": {"name": "X", "url": SITE, "entityType": "Organization"}})
    with pytest.raises(ConfigError, match="siteId"):
        wf.site_id(bare)


@respx.mock
def test_plan_lists_static_pages_with_issues_and_changes_nothing(tmp_path):
    route = mock_pages()
    with wf.make_client(cfg()) as client:
        plan = wf.build_plan(cfg(), client)
    assert route.calls.last.request.headers["authorization"] == "Bearer wf-secret"
    assert [e["pageId"] for e in plan["entries"]] == ["p1", "p2", "p3"]
    assert [s["pageId"] for s in plan["skipped"]] == ["tpl", "old"]
    assert plan["entries"][0]["issues"] == ["SEO title is empty"]
    assert plan["entries"][1]["issues"] == [
        "SEO title is 70 characters (limit 60)", "meta description is empty"]
    assert plan["entries"][2]["issues"] == []
    assert plan["entries"][0]["url"] == f"{SITE}/p1"
    assert all(e["approved"] is False for e in plan["entries"])
    assert all(call.request.method == "GET" for call in respx.calls)
    path = wf.write_plan(plan, tmp_path / "deep" / "plan.json")
    assert wf.load_plan(path) == plan


@respx.mock
def test_pages_are_read_across_pagination():
    def handler(request):
        offset = int(request.url.params["offset"])
        batch = [wf_page(f"p{offset}")]
        return httpx.Response(200, json={"pages": batch, "pagination": {"total": 3}})
    respx.get(f"{API}/sites/{SID}/pages").mock(side_effect=handler)
    with wf.make_client(cfg()) as client:
        assert [p["id"] for p in wf.list_pages(client, SID)] == ["p0", "p1", "p2"]


def test_load_plan_rejects_missing_and_foreign_files(tmp_path):
    with pytest.raises(ConfigError, match="No plan"):
        wf.load_plan(tmp_path / "nope.json")
    other = tmp_path / "other.json"
    other.write_text('{"hello": 1}')
    with pytest.raises(ConfigError, match="not a Rankwright Webflow plan"):
        wf.load_plan(other)


@respx.mock
def test_preview_sends_nothing(tmp_path):
    mock_pages()
    plan = approved_plan(tmp_path)
    calls_before = respx.calls.call_count
    results = wf.apply_plan(plan, None, write=False)
    assert respx.calls.call_count == calls_before
    assert results == [{"pageId": "p1", "title": "Page p1", "approved": True,
                        "changes": {"seoTitle": {"from": "", "to": "New title"}},
                        "notes": [], "status": "would-apply"}]


@respx.mock
def test_unapproved_entry_is_never_written(tmp_path):
    mock_pages()
    plan = approved_plan(tmp_path, approved=False)
    put = respx.put(f"{API}/pages/p1").mock(return_value=httpx.Response(200, json={}))
    with wf.make_client(cfg()) as client:
        results = wf.apply_plan(plan, client, write=True)
    assert results[0]["status"] == "not-approved"
    assert not put.called


@respx.mock
def test_apply_writes_only_the_changed_field(tmp_path):
    mock_pages()
    plan = approved_plan(tmp_path)
    respx.get(f"{API}/pages/p1").mock(return_value=httpx.Response(200, json=PAGES[0]))
    put = respx.put(f"{API}/pages/p1").mock(return_value=httpx.Response(200, json={}))
    with wf.make_client(cfg()) as client:
        results = wf.apply_plan(plan, client, write=True)
    assert results[0]["status"] == "applied"
    assert json.loads(put.calls.last.request.content) == {"seo": {"title": "New title"}}


@respx.mock
def test_stale_plan_is_refused(tmp_path):
    mock_pages()
    plan = approved_plan(tmp_path)
    respx.get(f"{API}/pages/p1").mock(return_value=httpx.Response(
        200, json=wf_page("p1", "Someone edited this", "Fine description.")))
    put = respx.put(f"{API}/pages/p1").mock(return_value=httpx.Response(200, json={}))
    with wf.make_client(cfg()) as client:
        results = wf.apply_plan(plan, client, write=True)
    assert results[0]["status"] == "stale"
    assert not put.called


@respx.mock
def test_api_error_is_a_failed_entry_without_the_token(tmp_path):
    mock_pages()
    plan = approved_plan(tmp_path)
    respx.get(f"{API}/pages/p1").mock(return_value=httpx.Response(200, json=PAGES[0]))
    respx.put(f"{API}/pages/p1").mock(return_value=httpx.Response(
        403, json={"message": "Missing scope pages:write"}))
    with wf.make_client(cfg()) as client:
        results = wf.apply_plan(plan, client, write=True)
    assert results[0]["status"] == "failed"
    assert "HTTP 403" in results[0]["error"] and "pages:write" in results[0]["error"]
    assert "wf-secret" not in json.dumps(results)


def write_jsonld(tmp_path, name="A </script> B", pad=0):
    path = tmp_path / "p1.jsonld"
    path.write_text(json.dumps({"@context": "https://schema.org", "@graph": [
        {"@type": "WebPage", "name": name, "description": "d" * pad}]}))
    return path


def test_jsonld_script_is_safe_and_round_trips(tmp_path):
    script = wf.jsonld_script(write_jsonld(tmp_path))
    assert "</script>" not in script
    literal = script.split("s.text=", 1)[1].rsplit(";document.head", 1)[0]
    injected = json.loads(literal)          # the string the browser will assign
    assert json.loads(injected)["@graph"][0]["name"] == "A </script> B"


@respx.mock
def test_jsonld_is_attached_and_other_scripts_are_kept(tmp_path):
    mock_pages()
    plan = approved_plan(tmp_path, jsonld=str(write_jsonld(tmp_path)),
                         proposed={"seoTitle": None, "seoDescription": None})
    respx.get(f"{API}/pages/p1").mock(return_value=httpx.Response(200, json=PAGES[0]))
    register = respx.post(f"{API}/sites/{SID}/registered_scripts/inline").mock(
        return_value=httpx.Response(201, json={"id": "rwjsonldabc"}))
    respx.get(f"{API}/pages/p1/custom_code").mock(return_value=httpx.Response(
        200, json={"scripts": [
            {"id": "analytics", "location": "header", "version": "2.0.0"},
            {"id": "rwjsonldold", "location": "header", "version": "1.0.0"}]}))
    upsert = respx.put(f"{API}/pages/p1/custom_code").mock(
        return_value=httpx.Response(200, json={}))
    seo_put = respx.put(f"{API}/pages/p1").mock(return_value=httpx.Response(200, json={}))
    with wf.make_client(cfg()) as client:
        results = wf.apply_plan(plan, client, write=True)
    assert results[0]["status"] == "applied" and results[0]["scriptId"] == "rwjsonldabc"
    assert not seo_put.called                       # no SEO change was proposed
    registered = json.loads(register.calls.last.request.content)
    assert registered["displayName"].isalnum() and len(registered["displayName"]) <= 50
    assert len(registered["sourceCode"]) <= wf.INLINE_SCRIPT_LIMIT
    assert json.loads(upsert.calls.last.request.content) == {"scripts": [
        {"id": "analytics", "location": "header", "version": "2.0.0"},
        {"id": "rwjsonldabc", "location": "header", "version": "1.0.0"}]}


@respx.mock
def test_oversized_jsonld_is_reported_not_sent(tmp_path):
    mock_pages()
    plan = approved_plan(tmp_path, jsonld=str(write_jsonld(tmp_path, pad=3000)),
                         proposed={"seoTitle": None, "seoDescription": None})
    respx.get(f"{API}/pages/p1").mock(return_value=httpx.Response(200, json=PAGES[0]))
    register = respx.post(f"{API}/sites/{SID}/registered_scripts/inline").mock(
        return_value=httpx.Response(201, json={"id": "x"}))
    with wf.make_client(cfg()) as client:
        results = wf.apply_plan(plan, client, write=True)
    assert not register.called
    assert results[0]["jsonld"]["fits"] is False
    assert any("inline limit" in n for n in results[0]["notes"])


@respx.mock
def test_publish_goes_page_by_page_and_waits_between_them():
    respx.get(f"{API}/sites/{SID}/custom_domains").mock(return_value=httpx.Response(
        200, json={"customDomains": [{"id": "d1", "url": "x.example"}]}))
    route = respx.post(f"{API}/sites/{SID}/publish").mock(side_effect=[
        httpx.Response(202, json={}), httpx.Response(429, json={"message": "slow down"})])
    waits = []
    with wf.make_client(cfg()) as client:
        results = wf.publish_pages(client, SID, ["p1", "p2"], pause=61,
                                   sleep=waits.append)
    assert waits == [61]
    assert [json.loads(c.request.content) for c in route.calls] == [
        {"customDomains": ["d1"], "pageId": "p1"},
        {"customDomains": ["d1"], "pageId": "p2"}]
    assert [r["status"] for r in results] == ["queued", "failed"]


@respx.mock
def test_publish_falls_back_to_the_webflow_subdomain():
    respx.get(f"{API}/sites/{SID}/custom_domains").mock(
        return_value=httpx.Response(200, json={"customDomains": []}))
    route = respx.post(f"{API}/sites/{SID}/publish").mock(
        return_value=httpx.Response(202, json={}))
    with wf.make_client(cfg()) as client:
        wf.publish_pages(client, SID, ["p1"], pause=0)
    assert json.loads(route.calls.last.request.content) == {
        "publishToWebflowSubdomain": True, "pageId": "p1"}
