"""Publish approved SEO changes to a Webflow site through the Data API v2.

The flow is two steps with a human in the middle:

1. `build_plan()` reads every static page from Webflow and writes a plan file
   listing each page's current SEO title and description, what is wrong with
   them, and empty `proposed` fields. It changes nothing.
2. Someone fills in `proposed` and sets `approved: true` on the entries they
   want. `apply_plan()` then previews those entries, and writes them only when
   called with `write=True`.

Three guarantees hold for every write:

- Only entries marked `approved: true` are touched.
- Each page is re-read first, and an entry whose live values no longer match the
  values recorded in the plan is refused as stale instead of overwriting whatever
  changed in between.
- A page update is saved in Webflow, not live. Going live is a separate call
  (`publish_pages`) behind its own flag.

JSON-LD is the awkward case. Webflow's API registers custom code as a script,
capped at 2,000 characters, so structured data can only be attached as a small
script that inserts the JSON-LD when the page runs. Google reads that; a crawler
that does not run JavaScript will not. When the markup is too long for the cap, or
that trade-off is not acceptable, paste the generated snippet into the page's
custom code in the Designer instead.
"""
from __future__ import annotations

import hashlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

from .config import Config, ConfigError
from .gates.seo import MAX_DESCRIPTION, MAX_TITLE

API = "https://api.webflow.com/v2"
DEFAULT_PLAN = ".rankwright/publish/webflow-plan.json"
DEFAULT_LOG = ".rankwright/publish/webflow-log.jsonl"
INLINE_SCRIPT_LIMIT = 2000
SCRIPT_PREFIX = "rwjsonld"
SCRIPT_VERSION = "1.0.0"
PAGE_LIMIT = 100
STALE_NOTE = ("the page changed in Webflow after the plan was made; "
              "rebuild the plan")


class WebflowError(Exception):
    """A Webflow API call failed. Carries the status, never the token."""


def settings(config: Config) -> dict:
    return (config.raw.get("publish") or {}).get("webflow") or {}


def site_id(config: Config) -> str:
    value = settings(config).get("siteId")
    if not value:
        raise ConfigError(
            'No Webflow site: set "publish": {"webflow": {"siteId": "<site id>"}}.')
    return value


def make_client(config: Config, timeout: float = 30.0) -> httpx.Client:
    token = config.secret("webflow")
    return httpx.Client(timeout=timeout, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/json"})


def _call(client: httpx.Client, method: str, path: str, **kwargs) -> dict:
    try:
        response = client.request(method, f"{API}{path}", **kwargs)
    except httpx.HTTPError as exc:
        raise WebflowError(f"{method} {path} failed: {type(exc).__name__}") from None
    if response.status_code >= 400:
        try:
            message = response.json().get("message", "")
        except ValueError:
            message = response.text[:200]
        raise WebflowError(
            f"{method} {path} returned HTTP {response.status_code}: {message}")
    if not response.content:
        return {}
    return response.json()


def list_pages(client: httpx.Client, site: str) -> list[dict]:
    pages: list[dict] = []
    offset = 0
    while True:
        data = _call(client, "GET", f"/sites/{site}/pages",
                     params={"limit": PAGE_LIMIT, "offset": offset})
        batch = data.get("pages", [])
        pages.extend(batch)
        total = data.get("pagination", {}).get("total", len(pages))
        offset += len(batch)
        if not batch or offset >= total:
            return pages


def _seo(page: dict) -> dict:
    seo = page.get("seo") or {}
    return {"seoTitle": seo.get("title") or "",
            "seoDescription": seo.get("description") or ""}


def issues_for(current: dict) -> list[str]:
    found = []
    title, description = current["seoTitle"], current["seoDescription"]
    if not title:
        found.append("SEO title is empty")
    elif len(title) > MAX_TITLE:
        found.append(f"SEO title is {len(title)} characters (limit {MAX_TITLE})")
    if not description:
        found.append("meta description is empty")
    elif len(description) > MAX_DESCRIPTION:
        found.append(f"meta description is {len(description)} characters "
                     f"(limit {MAX_DESCRIPTION})")
    return found


def is_static(page: dict) -> bool:
    """A page whose SEO fields are plain values.

    Collection template pages bind their SEO fields to CMS data, so a literal
    value written through the API would replace the binding for every item.
    """
    return not (page.get("collectionId") or page.get("archived") or page.get("draft"))


def build_plan(config: Config, client: httpx.Client) -> dict:
    site = site_id(config)
    entries = []
    skipped = []
    for page in list_pages(client, site):
        if not is_static(page):
            skipped.append({"pageId": page.get("id"), "title": page.get("title"),
                            "reason": "collection template, draft or archived"})
            continue
        current = _seo(page)
        path = page.get("publishedPath") or ""
        entries.append({
            "pageId": page["id"],
            "title": page.get("title", ""),
            "url": config.site_url + path if path else None,
            "current": current,
            "issues": issues_for(current),
            "proposed": {"seoTitle": None, "seoDescription": None},
            "jsonld": None,
            "approved": False,
        })
    return {"tool": "rankwright", "target": "webflow", "siteId": site,
            "site": config.site_url,
            "createdAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "entries": entries, "skipped": skipped}


def write_plan(plan: dict, path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n")
    return p


def load_plan(path: str | Path) -> dict:
    p = Path(path)
    if not p.is_file():
        raise ConfigError(
            f"No plan at {p}. Create one with `rankwright publish webflow plan`.")
    try:
        plan = json.loads(p.read_text())
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Plan is not valid JSON: {p}: {exc}") from exc
    if plan.get("target") != "webflow" or not isinstance(plan.get("entries"), list):
        raise ConfigError(f"{p} is not a Rankwright Webflow plan.")
    return plan


def _changes(entry: dict) -> dict:
    proposed = entry.get("proposed") or {}
    return {key: value for key, value in proposed.items()
            if key in {"seoTitle", "seoDescription"} and isinstance(value, str)
            and value.strip() and value != entry["current"].get(key)}


def jsonld_script(snippet_path: str | Path) -> str:
    """JavaScript that inserts the JSON-LD from a generated `.jsonld` file."""
    graph = json.loads(Path(snippet_path).read_text())
    payload = json.dumps(json.dumps(graph, separators=(",", ":"), ensure_ascii=False),
                         ensure_ascii=False).replace("</", "<\\/")
    return ('(function(){var s=document.createElement("script");'
            's.type="application/ld+json";'
            f"s.text={payload};document.head.appendChild(s);}})();")


def preview(plan: dict) -> list[dict]:
    """What `apply_plan` would do, entry by entry, with nothing sent anywhere."""
    actions = []
    for entry in plan["entries"]:
        changes = _changes(entry)
        wants_jsonld = bool(entry.get("jsonld"))
        if not changes and not wants_jsonld:
            continue
        action = {"pageId": entry["pageId"], "title": entry.get("title", ""),
                  "approved": bool(entry.get("approved")), "changes": {}, "notes": []}
        for key, value in changes.items():
            action["changes"][key] = {"from": entry["current"].get(key, ""), "to": value}
            limit = MAX_TITLE if key == "seoTitle" else MAX_DESCRIPTION
            if len(value) > limit:
                action["notes"].append(
                    f"{key} is {len(value)} characters (limit {limit})")
        if wants_jsonld:
            try:
                size = len(jsonld_script(entry["jsonld"]))
            except (OSError, ValueError) as exc:
                action["notes"].append(f"JSON-LD file unusable: {exc}")
                size = None
            if size is not None:
                action["jsonld"] = {"file": entry["jsonld"], "scriptChars": size,
                                    "fits": size <= INLINE_SCRIPT_LIMIT}
                if size > INLINE_SCRIPT_LIMIT:
                    action["notes"].append(
                        f"JSON-LD script is {size} characters; Webflow's inline limit "
                        f"is {INLINE_SCRIPT_LIMIT}. Paste the snippet into the page's "
                        "custom code in the Designer instead.")
        actions.append(action)
    return actions


def _attach_jsonld(client: httpx.Client, site: str, page_id: str, source: str) -> str:
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:10]
    name = f"{SCRIPT_PREFIX}{digest}"
    registered = _call(client, "POST", f"/sites/{site}/registered_scripts/inline", json={
        "sourceCode": source, "version": SCRIPT_VERSION, "displayName": name})
    script_id = registered.get("id", name)
    # The page endpoint replaces the whole list, so every script already on the
    # page is sent back, minus an earlier Rankwright one that this replaces.
    existing = _call(client, "GET", f"/pages/{page_id}/custom_code").get("scripts", [])
    kept = [s for s in existing if not str(s.get("id", "")).startswith(SCRIPT_PREFIX)]
    kept.append({"id": script_id, "location": "header", "version": SCRIPT_VERSION})
    _call(client, "PUT", f"/pages/{page_id}/custom_code", json={"scripts": kept})
    return script_id


def apply_plan(plan: dict, client: httpx.Client, *, write: bool = False) -> list[dict]:
    """Apply approved entries. With `write=False` this is `preview()` plus status."""
    site = plan["siteId"]
    by_id = {entry["pageId"]: entry for entry in plan["entries"]}
    results = []
    for action in preview(plan):
        entry = by_id[action["pageId"]]
        result = {**action, "status": "would-apply"}
        if not action["approved"]:
            result["status"] = "not-approved"
        elif write:
            try:
                live = _seo(_call(client, "GET", f"/pages/{entry['pageId']}"))
                if live != {k: entry["current"].get(k, "") for k in live}:
                    result["status"] = "stale"
                    result["notes"] = [*action["notes"], STALE_NOTE]
                else:
                    changes = _changes(entry)
                    if changes:
                        seo = {}
                        if "seoTitle" in changes:
                            seo["title"] = changes["seoTitle"]
                        if "seoDescription" in changes:
                            seo["description"] = changes["seoDescription"]
                        _call(client, "PUT", f"/pages/{entry['pageId']}",
                              json={"seo": seo})
                    jsonld = action.get("jsonld")
                    if jsonld and jsonld["fits"]:
                        result["scriptId"] = _attach_jsonld(
                            client, site, entry["pageId"], jsonld_script(jsonld["file"]))
                    result["status"] = "applied"
            except WebflowError as exc:
                result["status"] = "failed"
                result["error"] = str(exc)
        results.append(result)
    return results


PUBLISH_INTERVAL_SECONDS = 61


def publish_pages(client: httpx.Client, site: str, page_ids: list[str], *,
                  pause: float = PUBLISH_INTERVAL_SECONDS,
                  sleep=time.sleep) -> list[dict]:
    """Make the given pages live, one page at a time.

    Pages are published individually on purpose: a whole-site publish would also
    push every unrelated change someone has staged in the Designer. Webflow allows
    one publish per minute per site, so this waits `pause` seconds between pages.
    A failure is reported, never retried in a loop.
    """
    domains = [d["id"] for d in _call(
        client, "GET", f"/sites/{site}/custom_domains").get("customDomains", [])]
    body: dict = {"customDomains": domains} if domains else {
        "publishToWebflowSubdomain": True}
    results = []
    for index, page_id in enumerate(page_ids):
        if index and pause:
            sleep(pause)
        try:
            _call(client, "POST", f"/sites/{site}/publish",
                  json={**body, "pageId": page_id})
            results.append({"pageId": page_id, "status": "queued"})
        except WebflowError as exc:
            results.append({"pageId": page_id, "status": "failed", "error": str(exc)})
    return results


def append_log(path: str | Path, results: list[dict]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    with p.open("a") as handle:
        for result in results:
            handle.write(json.dumps({"at": now, **result}, ensure_ascii=False) + "\n")
