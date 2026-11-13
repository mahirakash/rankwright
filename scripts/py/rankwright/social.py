"""Draft social posts from a published page, and publish the approved ones.

Drafting and publishing are separate on purpose. `draft()` reads a page and writes
one JSON file per platform into a queue directory, each marked `approved: false`.
A person edits the text and flips the flag. `publish()` then sends only approved
drafts, and only when called with `write=True`; without it the run is a preview.

The draft text is assembled from the page's own title, description and answer
block. It is a starting point for an editor, not finished copy: nothing here
writes a hook, picks a hashtag or invents a claim the page does not make.

Adapters exist for Facebook Pages, Instagram and LinkedIn. `x` and `youtube` are
valid config values with no adapter; a draft for them is refused with that reason
rather than being queued for a publish that can never happen.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup

from .config import Config, ConfigError
from .fetch import fetch

DEFAULT_QUEUE = ".rankwright/social/queue"
DEFAULT_GRAPH_VERSION = "v26.0"
DEFAULT_LINKEDIN_VERSION = "202610"
GRAPH = "https://graph.facebook.com"
LINKEDIN_POSTS = "https://api.linkedin.com/rest/posts"
ADAPTERS = ("facebook", "instagram", "linkedin")
# Platform caption limits. A draft over the limit is still written; publish()
# refuses it, so the editor sees the problem instead of a silent truncation.
LIMITS = {"facebook": 63206, "instagram": 2200, "linkedin": 3000}


class SocialError(Exception):
    """A platform call failed. Carries the status, never the token."""


@dataclass(frozen=True)
class Source:
    url: str
    title: str
    description: str
    answer: str
    image: str


def smm(config: Config) -> dict:
    return config.raw.get("smm") or {}


def platforms(config: Config, explicit: list[str] | None = None) -> list[str]:
    chosen = explicit or smm(config).get("platforms") or list(ADAPTERS)
    return list(dict.fromkeys(chosen))


def queue_dir(config: Config, explicit: str | None = None) -> Path:
    return Path(explicit or smm(config).get("queueDir") or DEFAULT_QUEUE)


def needs_approval(config: Config) -> bool:
    return smm(config).get("requireHumanApproval", True) is not False


def _text(node) -> str:
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)) if node else ""


def _meta(soup: BeautifulSoup, attr: str, value: str) -> str:
    tag = soup.find("meta", attrs={
        attr: lambda v: v is not None and v.strip().lower() == value})
    return (tag.get("content") or "").strip() if tag else ""


def read_source(client: httpx.Client, config: Config, url: str) -> Source:
    fetched = fetch(client, url)
    if not fetched.ok:
        raise SocialError(f"{url} returned HTTP {fetched.status}; nothing to draft from")
    soup = BeautifulSoup(fetched.text, "lxml")
    return Source(
        url=url,
        title=_meta(soup, "property", "og:title") or _text(soup.find("h1"))
        or _text(soup.find("title")),
        description=_meta(soup, "name", "description")
        or _meta(soup, "property", "og:description"),
        answer=_text(soup.select_one(config.answer_block_selector)),
        image=_meta(soup, "property", "og:image"),
    )


def compose(platform: str, source: Source) -> str:
    """Draft text from the page's own words. Instagram gets no link in the body,
    because links in captions are not clickable there."""
    body = source.answer or source.description
    parts = [p for p in (source.title, body) if p]
    if platform != "instagram":
        parts.append(source.url)
    return "\n\n".join(parts)


def _slug(url: str) -> str:
    path = urlsplit(url).path.strip("/")
    return re.sub(r"[^A-Za-z0-9]+", "-", path).strip("-").lower() or "home"


def draft(config: Config, client: httpx.Client, url: str, *,
          only: list[str] | None = None, out: str | None = None,
          today: str | None = None) -> tuple[list[Path], list[str]]:
    """Write one draft file per platform. Returns the files and the skip reasons."""
    source = read_source(client, config, url)
    directory = queue_dir(config, out)
    directory.mkdir(parents=True, exist_ok=True)
    day = today or datetime.now(UTC).date().isoformat()
    written: list[Path] = []
    skipped: list[str] = []
    for platform in platforms(config, only):
        if platform not in ADAPTERS:
            skipped.append(f"{platform}: no publishing adapter")
            continue
        if platform == "instagram" and not source.image:
            skipped.append("instagram: the page has no og:image, and Instagram "
                           "cannot publish a post without an image")
            continue
        if not source.title and not source.description and not source.answer:
            skipped.append(f"{platform}: the page has no title, description or "
                           "answer block to draft from")
            continue
        path = directory / f"{day}-{_slug(url)}-{platform}.json"
        if path.exists():
            skipped.append(f"{platform}: {path} already exists; not overwritten")
            continue
        path.write_text(json.dumps({
            "platform": platform,
            "sourceUrl": url,
            "title": source.title,
            "text": compose(platform, source),
            "link": None if platform == "instagram" else url,
            "imageUrl": source.image or None,
            "approved": False,
            "status": "draft",
            "createdAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        }, indent=2, ensure_ascii=False) + "\n")
        written.append(path)
    return written, skipped


def load_queue(directory: str | Path) -> list[tuple[Path, dict]]:
    items = []
    for path in sorted(Path(directory).glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            raise ConfigError(f"Draft is not valid JSON: {path}: {exc}") from exc
        items.append((path, data))
    return items


def _post(client: httpx.Client, url: str, *, label: str, **kwargs) -> httpx.Response:
    try:
        response = client.post(url, **kwargs)
    except httpx.HTTPError as exc:
        raise SocialError(f"{label} failed: {type(exc).__name__}") from None
    if response.status_code >= 400:
        try:
            data = response.json()
            error = data.get("error", data) if isinstance(data, dict) else data
            message = error.get("message", error) if isinstance(error, dict) else error
        except ValueError:
            message = response.text
        raise SocialError(f"{label} returned HTTP {response.status_code}: "
                          f"{str(message)[:200]}")
    return response


def _require(config: Config, key: str, hint: str) -> str:
    value = smm(config).get(key)
    if not value:
        raise ConfigError(f'Missing "smm": {{"{key}": ...}} ({hint}).')
    return value


def publish_facebook(client: httpx.Client, config: Config, item: dict) -> str:
    page_id = _require(config, "facebookPageId", "the Facebook Page id")
    version = smm(config).get("graphVersion", DEFAULT_GRAPH_VERSION)
    # The token goes in the form body, not the query string, so it does not end up
    # in URLs that proxies and logs record.
    data = {"message": item["text"], "access_token": config.secret("facebook")}
    if item.get("link"):
        data["link"] = item["link"]
    response = _post(client, f"{GRAPH}/{version}/{page_id}/feed", data=data,
                     label="Facebook post")
    return response.json()["id"]


def publish_instagram(client: httpx.Client, config: Config, item: dict) -> str:
    user_id = _require(config, "instagramUserId", "the Instagram professional account id")
    version = smm(config).get("graphVersion", DEFAULT_GRAPH_VERSION)
    token = config.secret("instagram")
    container = _post(client, f"{GRAPH}/{version}/{user_id}/media", label="Instagram media",
                      data={"image_url": item["imageUrl"], "caption": item["text"],
                            "access_token": token})
    published = _post(client, f"{GRAPH}/{version}/{user_id}/media_publish",
                      label="Instagram publish",
                      data={"creation_id": container.json()["id"], "access_token": token})
    return published.json()["id"]


def publish_linkedin(client: httpx.Client, config: Config, item: dict) -> str:
    author = _require(config, "linkedinAuthor",
                      "an author URN such as urn:li:organization:123")
    body: dict = {
        "author": author,
        "commentary": item["text"],
        "visibility": "PUBLIC",
        "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [],
                         "thirdPartyDistributionChannels": []},
        "lifecycleState": "PUBLISHED",
        "isReshareDisabledByAuthor": False,
    }
    if item.get("link"):
        # Without an article block LinkedIn shows the URL as plain text, with no
        # preview card.
        body["content"] = {"article": {"source": item["link"],
                                       "title": item.get("title") or item["link"]}}
    response = _post(client, LINKEDIN_POSTS, json=body, label="LinkedIn post", headers={
        "Authorization": f"Bearer {config.secret('linkedin')}",
        "LinkedIn-Version": smm(config).get("linkedinVersion", DEFAULT_LINKEDIN_VERSION),
        "X-Restli-Protocol-Version": "2.0.0",
    })
    return response.headers.get("x-restli-id", "")


PUBLISHERS = {"facebook": publish_facebook, "instagram": publish_instagram,
              "linkedin": publish_linkedin}


def _problem(item: dict) -> str | None:
    platform = item.get("platform")
    if platform not in PUBLISHERS:
        return f"no publishing adapter for {platform!r}"
    text = item.get("text") or ""
    if not text.strip():
        return "text is empty"
    if len(text) > LIMITS[platform]:
        return f"text is {len(text)} characters (limit {LIMITS[platform]})"
    if platform == "instagram" and not item.get("imageUrl"):
        return "Instagram needs an imageUrl"
    return None


def publish(config: Config, client: httpx.Client, *, out: str | None = None,
            write: bool = False) -> list[dict]:
    """Publish approved drafts. With `write=False` nothing is sent or changed.

    A published draft is marked in its own file with the post id, so a second run
    cannot post it twice. A failed one is marked failed with the reason and is
    not retried until someone resets its status to draft.
    """
    results = []
    for path, item in load_queue(queue_dir(config, out)):
        status = item.get("status", "draft")
        result = {"file": str(path), "platform": item.get("platform"), "status": status}
        if status != "draft":
            results.append(result)
            continue
        problem = _problem(item)
        if problem:
            result.update(status="invalid", error=problem)
        elif needs_approval(config) and item.get("approved") is not True:
            result["status"] = "not-approved"
        elif not write:
            result["status"] = "would-publish"
        else:
            now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            try:
                post_id = PUBLISHERS[item["platform"]](client, config, item)
                item.update(status="published", postId=post_id, publishedAt=now)
                result.update(status="published", postId=post_id)
            except ConfigError as exc:
                # A missing id or token is a setup problem, not a failed post:
                # the draft is left untouched so it publishes once setup is fixed.
                result.update(status="blocked", error=str(exc))
                results.append(result)
                continue
            except SocialError as exc:
                item.update(status="failed", error=str(exc), failedAt=now)
                result.update(status="failed", error=str(exc))
            path.write_text(json.dumps(item, indent=2, ensure_ascii=False) + "\n")
        results.append(result)
    return results
