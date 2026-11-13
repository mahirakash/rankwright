import json

import httpx
import pytest
import respx

from rankwright import indexing
from rankwright.config import Config, ConfigError
from rankwright.fetch import make_client

SITE = "https://x.example"
KEY = "abcdef0123456789abcdef0123456789"
SITEMAP = (f"<urlset><url><loc>{SITE}/a</loc></url><url><loc>{SITE}/b</loc></url>"
           "<url><loc>https://other.example/c</loc></url>"
           f"<url><loc>{SITE}/gone</loc></url></urlset>")


def cfg(**extra) -> Config:
    return Config({"site": {"name": "X", "url": SITE, "entityType": "Organization"},
                   **extra})


def html(text: str, script: str = "") -> str:
    return (f"<html><head><title>T</title><script>{script}</script></head>"
            f"<body><p>{text}</p></body></html>")


def mock_site(a: str = "alpha", b: str = "beta"):
    respx.get(f"{SITE}/sitemap.xml").mock(return_value=httpx.Response(200, text=SITEMAP))
    respx.get(f"{SITE}/a").mock(return_value=httpx.Response(200, text=html(a)))
    respx.get(f"{SITE}/b").mock(return_value=httpx.Response(200, text=html(b)))
    respx.get(f"{SITE}/gone").mock(return_value=httpx.Response(404))


def test_hash_ignores_scripts_but_not_visible_text():
    assert indexing.content_hash(html("x", "nonce1")) == indexing.content_hash(
        html("x", "nonce2"))
    assert indexing.content_hash(html("x")) != indexing.content_hash(html("y"))


def test_key_rules():
    assert indexing.validate_key(f" {KEY}\n") == KEY
    for bad in ("short", "has space in it", "x" * 129, "under_score_key"):
        with pytest.raises(ConfigError):
            indexing.validate_key(bad)
    assert indexing.KEY_RE.fullmatch(indexing.new_key())


def test_write_key_file(tmp_path):
    path = indexing.write_key_file(KEY, tmp_path / "public")
    assert path.name == f"{KEY}.txt" and path.read_text() == KEY


def test_resolve_key_order(tmp_path, monkeypatch):
    assert indexing.resolve_key(cfg(), KEY) == KEY
    monkeypatch.setenv("INDEXNOW_KEY", "fromsecret-123")
    assert indexing.resolve_key(
        cfg(secrets={"indexnow": "env:INDEXNOW_KEY"})) == "fromsecret-123"
    key_file = tmp_path / "k.txt"
    key_file.write_text(KEY + "\n")
    assert indexing.resolve_key(cfg(indexing={"indexnowKeyFile": str(key_file)})) == KEY
    with pytest.raises(ConfigError, match="No IndexNow key"):
        indexing.resolve_key(cfg())


@respx.mock
def test_plan_classifies_new_changed_unchanged_unreachable_and_foreign():
    mock_site()
    state = {f"{SITE}/a": {"hash": indexing.content_hash(html("alpha"))},
             f"{SITE}/b": {"hash": "stale"}}
    with make_client() as client:
        found = indexing.plan(cfg(), client, state)
    assert found.unchanged == [f"{SITE}/a"]
    assert found.changed == [f"{SITE}/b"]
    assert found.new == []
    assert found.unreachable == [f"{SITE}/gone"]
    assert found.foreign == ["https://other.example/c"]
    assert found.to_submit == [f"{SITE}/b"]


@respx.mock
def test_first_run_treats_every_reachable_url_as_new():
    mock_site()
    with make_client() as client:
        found = indexing.plan(cfg(), client, {})
    assert found.new == [f"{SITE}/a", f"{SITE}/b"]


@respx.mock
def test_verify_key_file():
    location = f"{SITE}/{KEY}.txt"
    with make_client() as client:
        respx.get(location).mock(return_value=httpx.Response(200, text=KEY + "\n"))
        assert indexing.verify_key_file(client, KEY, location) is None
        respx.get(location).mock(return_value=httpx.Response(200, text="other"))
        assert "does not contain" in indexing.verify_key_file(client, KEY, location)
        respx.get(location).mock(return_value=httpx.Response(404))
        assert "HTTP 404" in indexing.verify_key_file(client, KEY, location)


@respx.mock
def test_submit_sends_the_documented_body_and_records_accepted_urls():
    mock_site()
    route = respx.post(indexing.ENDPOINT).mock(return_value=httpx.Response(202))
    with make_client() as client:
        found = indexing.plan(cfg(), client, {})
        results = indexing.submit(client, cfg(), KEY, f"{SITE}/{KEY}.txt",
                                  found.to_submit)
    assert json.loads(route.calls.last.request.content) == {
        "host": "x.example", "key": KEY, "keyLocation": f"{SITE}/{KEY}.txt",
        "urlList": [f"{SITE}/a", f"{SITE}/b"]}
    assert results[0].accepted and "pending" in results[0].detail
    state = indexing.record({}, found, results)
    assert state[f"{SITE}/a"]["hash"] == found.hashes[f"{SITE}/a"]
    assert state[f"{SITE}/a"]["submittedAt"].endswith("Z")


@respx.mock
def test_rejected_batch_leaves_state_untouched():
    mock_site()
    respx.post(indexing.ENDPOINT).mock(return_value=httpx.Response(403))
    with make_client() as client:
        found = indexing.plan(cfg(), client, {})
        results = indexing.submit(client, cfg(), KEY, f"{SITE}/{KEY}.txt",
                                  found.to_submit)
    assert not results[0].accepted and "key not valid" in results[0].detail
    assert indexing.record({"keep": {"hash": "h"}}, found, results) == {
        "keep": {"hash": "h"}}


@respx.mock
def test_network_failure_is_a_failed_batch_not_a_crash():
    respx.post(indexing.ENDPOINT).mock(side_effect=httpx.ConnectError("down"))
    with make_client() as client:
        results = indexing.submit(client, cfg(), KEY, "loc", [f"{SITE}/a"])
    assert results[0].status == 0 and not results[0].accepted


def test_state_round_trip_and_corrupt_file(tmp_path):
    path = tmp_path / "nested" / "state.json"
    assert indexing.load_state(path) == {}
    indexing.save_state(path, {"u": {"hash": "h"}})
    assert indexing.load_state(path) == {"u": {"hash": "h"}}
    path.write_text("{not json")
    with pytest.raises(ConfigError):
        indexing.load_state(path)
