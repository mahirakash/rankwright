"""The schema, index, rank and publish subcommands, end to end through main()."""
import json

import httpx
import pytest
import respx

from rankwright import indexing, publish_webflow, rank, social
from rankwright.cli import main

SITE = "https://x.example"
KEY = "abcdef0123456789abcdef0123456789"
PAGE_HTML = ('<html lang="en"><head><title>Home</title>'
             '<meta name="description" content="Welcome.">'
             '<meta property="og:image" content="https://x.example/og.png"></head>'
             "<body><h1>Home</h1></body></html>")


def mock_site():
    respx.get(f"{SITE}/sitemap.xml").mock(return_value=httpx.Response(
        200, text=f"<urlset><url><loc>{SITE}/</loc></url></urlset>"))
    respx.get(f"{SITE}/").mock(return_value=httpx.Response(200, text=PAGE_HTML))


def write_config(tmp_path, **extra):
    path = tmp_path / "rankwright.config.json"
    path.write_text(json.dumps({
        "site": {"name": "X", "url": SITE, "entityType": "Organization"}, **extra}))
    return str(path)


@pytest.fixture(autouse=True)
def in_tmp(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


def writes():
    return [c for c in respx.calls if c.request.method != "GET"]


# --- schema ------------------------------------------------------------------

@respx.mock
def test_schema_command_writes_snippets_and_publishes_nothing(tmp_path, capsys):
    mock_site()
    assert main(["schema", SITE, "--out", str(tmp_path / "out")]) == 0
    out = capsys.readouterr().out
    assert "Organization, WebSite, WebPage" in out and "Nothing was published" in out
    assert (tmp_path / "out" / "index.jsonld").exists()
    assert writes() == []


@respx.mock
def test_schema_command_exits_one_when_a_page_is_unreachable(tmp_path, capsys):
    respx.get(f"{SITE}/missing").mock(return_value=httpx.Response(500))
    assert main(["schema", SITE, "--out", str(tmp_path / "o"),
                 "--page", f"{SITE}/missing"]) == 1
    assert "UNREACHABLE" in capsys.readouterr().out


# --- index -------------------------------------------------------------------

@respx.mock
def test_index_defaults_to_a_preview(tmp_path, capsys):
    mock_site()
    post = respx.post(indexing.ENDPOINT).mock(return_value=httpx.Response(200))
    assert main(["index", SITE]) == 0
    out = capsys.readouterr().out
    assert "1 new" in out and "Preview only" in out and "--submit" in out
    assert not post.called
    assert not (tmp_path / indexing.DEFAULT_STATE).exists()


@respx.mock
def test_index_submit_then_second_run_has_nothing_to_send(tmp_path, capsys):
    mock_site()
    respx.get(f"{SITE}/{KEY}.txt").mock(return_value=httpx.Response(200, text=KEY))
    post = respx.post(indexing.ENDPOINT).mock(return_value=httpx.Response(200))
    assert main(["index", SITE, "--key", KEY, "--submit"]) == 0
    assert "HTTP 200" in capsys.readouterr().out
    assert main(["index", SITE, "--key", KEY, "--submit"]) == 0
    assert "Nothing to submit" in capsys.readouterr().out
    assert post.call_count == 1


@respx.mock
def test_index_refuses_to_submit_with_an_unverified_key(tmp_path, capsys):
    mock_site()
    respx.get(f"{SITE}/{KEY}.txt").mock(return_value=httpx.Response(404))
    post = respx.post(indexing.ENDPOINT).mock(return_value=httpx.Response(200))
    assert main(["index", SITE, "--key", KEY, "--submit", "--json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert "HTTP 404" in data["blocked"] and data["submitted"] is False
    assert not post.called


@respx.mock
def test_index_rejected_submission_exits_one_and_keeps_urls_pending(tmp_path, capsys):
    mock_site()
    respx.get(f"{SITE}/{KEY}.txt").mock(return_value=httpx.Response(200, text=KEY))
    respx.post(indexing.ENDPOINT).mock(return_value=httpx.Response(429))
    assert main(["index", SITE, "--key", KEY, "--submit"]) == 1
    assert indexing.load_state(indexing.DEFAULT_STATE) == {}


def test_index_submit_without_a_key_is_a_config_error(capsys):
    assert main(["index", SITE, "--submit"]) == 2
    assert "No IndexNow key" in capsys.readouterr().err


def test_index_keygen_writes_a_valid_key_file(tmp_path, capsys):
    assert main(["index", SITE, "--keygen", str(tmp_path / "public")]) == 0
    files = list((tmp_path / "public").glob("*.txt"))
    assert len(files) == 1 and files[0].read_text() == files[0].stem
    assert indexing.KEY_RE.fullmatch(files[0].stem)


# --- rank --------------------------------------------------------------------

@respx.mock
def test_rank_serpapi_records_history_and_reports_movement(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("SERPAPI_KEY", "k")
    config = write_config(tmp_path, rank={"provider": "serpapi", "keywords": ["kw"],
                                          "depth": 10},
                          secrets={"serpapi": "env:SERPAPI_KEY"})
    positions = iter([4, 2])

    def handler(request):
        mine = next(positions)
        links = [f"https://o{i}.example/" for i in range(10)]
        links[mine - 1] = f"{SITE}/page"
        return httpx.Response(200, json={"organic_results": [
            {"link": link} for link in links]})
    respx.get(rank.SERPAPI_ENDPOINT).mock(side_effect=handler)

    assert main(["rank", "--config", config]) == 0
    assert "first-run" in capsys.readouterr().out
    assert main(["rank", "--config", config, "--json"]) == 0
    row = json.loads(capsys.readouterr().out)["rows"][0]
    assert (row["position"], row["previous"], row["change"], row["status"]) == (
        2, 4, 2, "up")
    assert len(rank.load_history(rank.DEFAULT_HISTORY)) == 2


def test_rank_without_credentials_is_a_config_error(capsys, monkeypatch):
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    assert main(["rank", SITE]) == 2
    assert "No Search Console credentials" in capsys.readouterr().err


# --- publish webflow ---------------------------------------------------------

API = publish_webflow.API
WF_PAGE = {"id": "p1", "title": "Home", "publishedPath": "/",
           "seo": {"title": "", "description": "Welcome."}}


def webflow_config(tmp_path, monkeypatch):
    monkeypatch.setenv("WEBFLOW_TOKEN", "wf-secret")
    respx.get(f"{API}/sites/s1/pages").mock(return_value=httpx.Response(
        200, json={"pages": [WF_PAGE], "pagination": {"total": 1}}))
    return write_config(tmp_path, publish={"webflow": {"siteId": "s1"}},
                        secrets={"webflow": "env:WEBFLOW_TOKEN"})


def approve_plan(path):
    plan = json.loads(path.read_text())
    plan["entries"][0]["proposed"]["seoTitle"] = "X — design studio"
    plan["entries"][0]["approved"] = True
    path.write_text(json.dumps(plan))


@respx.mock
def test_webflow_plan_preview_apply_and_publish(tmp_path, capsys, monkeypatch):
    config = webflow_config(tmp_path, monkeypatch)
    plan_path = tmp_path / publish_webflow.DEFAULT_PLAN

    assert main(["publish", "webflow", "plan", "--config", config]) == 0
    assert "1 with issues" in capsys.readouterr().out
    assert writes() == []

    # A second `plan` must not silently wipe approvals.
    approve_plan(plan_path)
    assert main(["publish", "webflow", "plan", "--config", config]) == 2
    assert "--force" in capsys.readouterr().err

    assert main(["publish", "webflow", "apply", "--config", config]) == 0
    out = capsys.readouterr().out
    assert "preview (writes nothing)" in out and "[would-apply]" in out
    assert writes() == []

    respx.get(f"{API}/pages/p1").mock(return_value=httpx.Response(200, json=WF_PAGE))
    put = respx.put(f"{API}/pages/p1").mock(return_value=httpx.Response(200, json={}))
    assert main(["publish", "webflow", "apply", "--config", config, "--apply"]) == 0
    out = capsys.readouterr().out
    assert "[applied]" in out and "Saved in Webflow, not live" in out
    assert put.call_count == 1
    log = (tmp_path / publish_webflow.DEFAULT_LOG).read_text()
    assert '"status": "applied"' in log and "wf-secret" not in log


@respx.mock
def test_webflow_publish_flag_requires_apply(tmp_path, capsys, monkeypatch):
    config = webflow_config(tmp_path, monkeypatch)
    assert main(["publish", "webflow", "apply", "--config", config, "--publish"]) == 2
    assert "--apply" in capsys.readouterr().err


@respx.mock
def test_webflow_apply_and_publish_makes_only_the_changed_page_live(
        tmp_path, capsys, monkeypatch):
    config = webflow_config(tmp_path, monkeypatch)
    assert main(["publish", "webflow", "plan", "--config", config]) == 0
    approve_plan(tmp_path / publish_webflow.DEFAULT_PLAN)
    capsys.readouterr()
    respx.get(f"{API}/pages/p1").mock(return_value=httpx.Response(200, json=WF_PAGE))
    respx.put(f"{API}/pages/p1").mock(return_value=httpx.Response(200, json={}))
    respx.get(f"{API}/sites/s1/custom_domains").mock(
        return_value=httpx.Response(200, json={"customDomains": [{"id": "d1"}]}))
    live = respx.post(f"{API}/sites/s1/publish").mock(
        return_value=httpx.Response(202, json={}))
    assert main(["publish", "webflow", "apply", "--config", config,
                 "--apply", "--publish", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["published"] == [{"pageId": "p1", "status": "queued"}]
    assert json.loads(live.calls.last.request.content) == {
        "customDomains": ["d1"], "pageId": "p1"}


def test_webflow_without_a_token_is_a_config_error(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("WEBFLOW_TOKEN", raising=False)
    config = write_config(tmp_path, publish={"webflow": {"siteId": "s1"}},
                          secrets={"webflow": "env:WEBFLOW_TOKEN"})
    assert main(["publish", "webflow", "plan", "--config", config]) == 2
    assert "WEBFLOW_TOKEN" in capsys.readouterr().err


# --- publish social ----------------------------------------------------------

@respx.mock
def test_social_draft_needs_no_config_and_send_previews(tmp_path, capsys):
    respx.get(f"{SITE}/").mock(return_value=httpx.Response(200, text=PAGE_HTML))
    assert main(["publish", "social", "draft", f"{SITE}/",
                 "--platform", "facebook"]) == 0
    assert "wrote" in capsys.readouterr().out
    drafts = list((tmp_path / social.DEFAULT_QUEUE).glob("*.json"))
    assert len(drafts) == 1

    assert main(["publish", "social", "send"]) == 2          # no config, no site
    config = write_config(tmp_path)
    assert main(["publish", "social", "send", "--config", config]) == 0
    out = capsys.readouterr().out
    assert "preview (posts nothing)" in out and "[not-approved]" in out
    assert writes() == []


@respx.mock
def test_social_send_apply_posts_an_approved_draft(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("FB_TOKEN", "t")
    respx.get(f"{SITE}/").mock(return_value=httpx.Response(200, text=PAGE_HTML))
    config = write_config(tmp_path, smm={"platforms": ["facebook"],
                                         "facebookPageId": "fb1"},
                          secrets={"facebook": "env:FB_TOKEN"})
    assert main(["publish", "social", "draft", f"{SITE}/", "--config", config]) == 0
    draft = next((tmp_path / social.DEFAULT_QUEUE).glob("*.json"))
    data = json.loads(draft.read_text())
    data["approved"] = True
    draft.write_text(json.dumps(data))
    respx.post(f"{social.GRAPH}/{social.DEFAULT_GRAPH_VERSION}/fb1/feed").mock(
        return_value=httpx.Response(200, json={"id": "post1"}))
    assert main(["publish", "social", "send", "--config", config, "--apply"]) == 0
    assert "[published] facebook" in capsys.readouterr().out
    assert json.loads(draft.read_text())["postId"] == "post1"


# --- config schema -----------------------------------------------------------

def test_new_config_sections_validate(tmp_path):
    from rankwright.config import ConfigError, load_config
    good = write_config(
        tmp_path,
        rank={"provider": "gsc", "keywords": ["a"], "days": 28, "depth": 30},
        publish={"webflow": {"siteId": "s"}},
        smm={"linkedinAuthor": "urn:li:organization:1", "graphVersion": "v26.0",
             "linkedinVersion": "202610"})
    load_config(good)
    for bad in ({"rank": {"provider": "bing"}}, {"publish": {"webflow": {}}},
                {"smm": {"graphVersion": "26"}}, {"smm": {"linkedinAuthor": "me"}},
                {"secrets": {"webflow": "literal-token"}}):
        with pytest.raises(ConfigError):
            load_config(write_config(tmp_path, **bad))
