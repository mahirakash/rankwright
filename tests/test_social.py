import json
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from rankwright import social
from rankwright.config import Config
from rankwright.fetch import make_client

SITE = "https://x.example"
PAGE = f"{SITE}/blog/launch"
HTML = ('<html><head><title>Launch | X</title>'
        '<meta name="description" content="We launched a thing.">'
        '<meta property="og:title" content="Launch day">'
        '<meta property="og:image" content="https://x.example/og.png"></head>'
        '<body><h1>Launch</h1><div class="answer-block">The thing is live today.</div>'
        "</body></html>")
GRAPH = f"{social.GRAPH}/{social.DEFAULT_GRAPH_VERSION}"


def cfg(tmp_path, **smm) -> Config:
    return Config({
        "site": {"name": "X", "url": SITE, "entityType": "Organization"},
        "smm": {"queueDir": str(tmp_path / "queue"), "facebookPageId": "fb1",
                "instagramUserId": "ig1", "linkedinAuthor": "urn:li:organization:9",
                **smm},
        "secrets": {"facebook": "env:FB_TOKEN", "instagram": "env:IG_TOKEN",
                    "linkedin": "env:LI_TOKEN"},
    })


@pytest.fixture(autouse=True)
def tokens(monkeypatch):
    for name in ("FB_TOKEN", "IG_TOKEN", "LI_TOKEN"):
        monkeypatch.setenv(name, f"secret-{name}")


def make_drafts(config, html=HTML, **kwargs):
    respx.get(PAGE).mock(return_value=httpx.Response(200, text=html))
    with make_client() as client:
        return social.draft(config, client, PAGE, today="2026-11-14", **kwargs)


def approve(path):
    data = json.loads(path.read_text())
    data["approved"] = True
    path.write_text(json.dumps(data))


@respx.mock
def test_drafts_use_the_pages_own_words_and_start_unapproved(tmp_path):
    written, skipped = make_drafts(cfg(tmp_path))
    assert [p.name for p in written] == [
        "2026-11-14-blog-launch-facebook.json", "2026-11-14-blog-launch-instagram.json",
        "2026-11-14-blog-launch-linkedin.json"]
    assert skipped == []
    fb, ig, _ = (json.loads(p.read_text()) for p in written)
    assert fb["text"] == f"Launch day\n\nThe thing is live today.\n\n{PAGE}"
    assert fb["approved"] is False and fb["status"] == "draft"
    assert PAGE not in ig["text"] and ig["link"] is None
    assert ig["imageUrl"] == "https://x.example/og.png"


@respx.mock
def test_instagram_without_an_image_and_unsupported_platforms_are_skipped(tmp_path):
    html = HTML.replace('<meta property="og:image" content="https://x.example/og.png">', "")
    written, skipped = make_drafts(cfg(tmp_path), html=html,
                                   only=["instagram", "x", "linkedin"])
    assert [p.name.rsplit("-", 1)[1] for p in written] == ["linkedin.json"]
    assert any(s.startswith("instagram:") and "og:image" in s for s in skipped)
    assert "x: no publishing adapter" in skipped


@respx.mock
def test_existing_draft_is_never_overwritten(tmp_path):
    config = cfg(tmp_path)
    written, _ = make_drafts(config, only=["facebook"])
    approve(written[0])
    again, skipped = make_drafts(config, only=["facebook"])
    assert again == [] and "already exists" in skipped[0]
    assert json.loads(written[0].read_text())["approved"] is True


@respx.mock
def test_unreachable_page_cannot_be_drafted(tmp_path):
    respx.get(PAGE).mock(return_value=httpx.Response(404))
    with make_client() as client, pytest.raises(social.SocialError, match="HTTP 404"):
        social.draft(cfg(tmp_path), client, PAGE)


@respx.mock
def test_preview_posts_nothing_and_changes_no_file(tmp_path):
    config = cfg(tmp_path)
    written, _ = make_drafts(config)
    approve(written[0])
    before = [p.read_text() for p in written]
    calls_before = respx.calls.call_count
    with make_client() as client:
        results = social.publish(config, client, write=False)
    assert respx.calls.call_count == calls_before
    assert [r["status"] for r in results] == [
        "would-publish", "not-approved", "not-approved"]
    assert [p.read_text() for p in written] == before


@respx.mock
def test_approved_drafts_are_posted_once(tmp_path):
    config = cfg(tmp_path)
    written, _ = make_drafts(config)
    for path in written:
        approve(path)
    fb = respx.post(f"{GRAPH}/fb1/feed").mock(
        return_value=httpx.Response(200, json={"id": "fb1_77"}))
    media = respx.post(f"{GRAPH}/ig1/media").mock(
        return_value=httpx.Response(200, json={"id": "container1"}))
    ig = respx.post(f"{GRAPH}/ig1/media_publish").mock(
        return_value=httpx.Response(200, json={"id": "ig_88"}))
    li = respx.post(social.LINKEDIN_POSTS).mock(
        return_value=httpx.Response(201, headers={"x-restli-id": "urn:li:share:99"}))
    with make_client() as client:
        results = social.publish(config, client, write=True)
        again = social.publish(config, client, write=True)

    assert [(r["status"], r["postId"]) for r in results] == [
        ("published", "fb1_77"), ("published", "ig_88"), ("published", "urn:li:share:99")]
    assert [r["status"] for r in again] == ["published"] * 3
    assert fb.call_count == media.call_count == ig.call_count == li.call_count == 1

    fb_request = fb.calls.last.request
    assert "access_token" not in str(fb_request.url)           # token stays out of URLs
    form = parse_qs(fb_request.content.decode())
    assert form["access_token"] == ["secret-FB_TOKEN"] and form["link"] == [PAGE]
    assert parse_qs(ig.calls.last.request.content.decode())["creation_id"] == ["container1"]

    li_request = li.calls.last.request
    assert li_request.headers["linkedin-version"] == social.DEFAULT_LINKEDIN_VERSION
    assert li_request.headers["x-restli-protocol-version"] == "2.0.0"
    body = json.loads(li_request.content)
    assert body["author"] == "urn:li:organization:9"
    assert body["lifecycleState"] == "PUBLISHED"
    assert body["content"]["article"] == {"source": PAGE, "title": "Launch day"}

    saved = json.loads(written[0].read_text())
    assert saved["status"] == "published" and saved["postId"] == "fb1_77"
    assert "secret-" not in "".join(p.read_text() for p in written)


@respx.mock
def test_platform_failure_is_recorded_and_not_retried(tmp_path):
    config = cfg(tmp_path)
    written, _ = make_drafts(config, only=["facebook"])
    approve(written[0])
    route = respx.post(f"{GRAPH}/fb1/feed").mock(return_value=httpx.Response(
        400, json={"error": {"message": "Invalid OAuth access token"}}))
    with make_client() as client:
        first = social.publish(config, client, write=True)
        second = social.publish(config, client, write=True)
    assert first[0]["status"] == "failed" and "Invalid OAuth" in first[0]["error"]
    assert second[0]["status"] == "failed" and route.call_count == 1
    assert "secret-FB_TOKEN" not in written[0].read_text()


@respx.mock
def test_missing_setup_blocks_without_touching_the_draft(tmp_path, monkeypatch):
    config = cfg(tmp_path)
    written, _ = make_drafts(config, only=["facebook"])
    approve(written[0])
    before = written[0].read_text()
    monkeypatch.delenv("FB_TOKEN")
    with make_client() as client:
        results = social.publish(config, client, write=True)
    assert results[0]["status"] == "blocked" and "FB_TOKEN" in results[0]["error"]
    assert written[0].read_text() == before


@respx.mock
def test_overlong_text_is_invalid_not_truncated(tmp_path):
    config = cfg(tmp_path)
    written, _ = make_drafts(config, only=["linkedin"])
    data = json.loads(written[0].read_text())
    data.update(approved=True, text="x" * 3001)
    written[0].write_text(json.dumps(data))
    with make_client() as client:
        results = social.publish(config, client, write=True)
    assert results[0]["status"] == "invalid" and "3001 characters" in results[0]["error"]


@respx.mock
def test_approval_can_be_switched_off_explicitly(tmp_path):
    config = cfg(tmp_path, requireHumanApproval=False)
    make_drafts(config, only=["facebook"])
    with make_client() as client:
        results = social.publish(config, client, write=False)
    assert results[0]["status"] == "would-publish"
