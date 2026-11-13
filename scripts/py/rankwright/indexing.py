"""IndexNow submission with hash-based freshness.

IndexNow tells participating engines (Bing, Yandex, Seznam, Naver and others --
not Google) that a URL changed. Submitting every URL on every run is what gets a
key rate-limited as spam, so this module remembers a content hash per URL and
submits only what is new or has actually changed.

Nothing is sent unless the caller asks for it. `plan()` is read-only; `submit()`
is the one network write in this module and the CLI reaches it only behind
`--submit`.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup

from .config import Config, ConfigError
from .fetch import fetch, read_sitemap

ENDPOINT = "https://api.indexnow.org/indexnow"
DEFAULT_STATE = ".rankwright/indexing/state.json"
# IndexNow's documented limits: a key is 8-128 characters of a-z, A-Z, 0-9 and
# dashes, and one POST carries at most 10,000 URLs.
KEY_RE = re.compile(r"^[A-Za-z0-9-]{8,128}$")
MAX_URLS_PER_POST = 10_000
ACCEPTED = {200, 202}
STATUS_MEANING = {
    200: "received",
    202: "received; key validation pending",
    400: "bad request: invalid format",
    403: "key not valid: the key file was not found or does not contain the key",
    422: "URLs do not belong to the host, or the key does not match",
    429: "too many requests: treated as potential spam",
}


def new_key() -> str:
    return secrets.token_hex(16)


def validate_key(key: str) -> str:
    key = key.strip()
    if not KEY_RE.fullmatch(key):
        raise ConfigError(
            "IndexNow key must be 8-128 characters of letters, digits and dashes.")
    return key


def write_key_file(key: str, out_dir: str | Path) -> Path:
    """Write `<key>.txt` containing the key. It must be served from the site root."""
    key = validate_key(key)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{key}.txt"
    path.write_text(key)
    return path


def resolve_key(config: Config, explicit: str | None = None) -> str:
    """The key from --key, then the `indexnow` secret, then the configured key file."""
    if explicit:
        return validate_key(explicit)
    if "indexnow" in config.raw.get("secrets", {}):
        return validate_key(config.secret("indexnow"))
    key_file = config.raw.get("indexing", {}).get("indexnowKeyFile")
    if key_file:
        path = Path(key_file)
        if not path.is_file():
            raise ConfigError(f"indexing.indexnowKeyFile not found: {path}")
        return validate_key(path.read_text())
    raise ConfigError(
        "No IndexNow key. Pass --key, set \"secrets\": {\"indexnow\": \"env:INDEXNOW_KEY\"}, "
        "or set indexing.indexnowKeyFile. Create one with `rankwright index --keygen`.")


def content_hash(html: str) -> str:
    """A hash of what a reader sees: title, description and visible text.

    Scripts, styles and attributes are left out on purpose. A page whose only
    change is a rotated CSP nonce or a new build id in a script URL has not
    changed, and resubmitting it would be noise.
    """
    soup = BeautifulSoup(html, "lxml")
    title = soup.find("title")
    meta = soup.find("meta", attrs={
        "name": lambda v: v is not None and v.strip().lower() == "description"})
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    body = soup.find("body") or soup
    parts = [
        title.get_text(" ", strip=True) if title else "",
        (meta.get("content") or "").strip() if meta else "",
        re.sub(r"\s+", " ", body.get_text(" ", strip=True)),
    ]
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def load_state(path: str | Path) -> dict:
    p = Path(path)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text())
    except json.JSONDecodeError as exc:
        raise ConfigError(f"IndexNow state file is not valid JSON: {p}: {exc}") from exc
    return data.get("urls", {}) if isinstance(data, dict) else {}


def save_state(path: str | Path, urls: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"urls": urls}, indent=2, sort_keys=True) + "\n")


@dataclass
class Plan:
    """What a run found. `hashes` holds the current hash of every reachable URL."""

    new: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    unreachable: list[str] = field(default_factory=list)
    foreign: list[str] = field(default_factory=list)
    hashes: dict[str, str] = field(default_factory=dict)

    @property
    def to_submit(self) -> list[str]:
        return [*self.new, *self.changed]

    def to_dict(self) -> dict:
        return {"new": self.new, "changed": self.changed, "unchanged": self.unchanged,
                "unreachable": self.unreachable, "foreign": self.foreign}


def plan(config: Config, client: httpx.Client, state: dict,
         urls: list[str] | None = None) -> Plan:
    """Classify every sitemap URL against the saved hashes. Sends nothing.

    A URL on another host is set aside as `foreign`: IndexNow rejects the whole
    batch with 422 when one URL does not belong to the submitting host.
    """
    host = urlsplit(config.site_url).netloc.lower()
    targets = urls or read_sitemap(client, config.site_url, config.sample_size) or [
        config.site_url + "/"]
    result = Plan()
    for url in dict.fromkeys(targets):
        if urlsplit(url).netloc.lower() != host:
            result.foreign.append(url)
            continue
        fetched = fetch(client, url)
        if not fetched.ok:
            result.unreachable.append(url)
            continue
        digest = content_hash(fetched.text)
        result.hashes[url] = digest
        previous = state.get(url, {}).get("hash")
        if previous is None:
            result.new.append(url)
        elif previous != digest:
            result.changed.append(url)
        else:
            result.unchanged.append(url)
    return result


def key_location(config: Config, key: str, explicit: str | None = None) -> str:
    return explicit or f"{config.site_url}/{key}.txt"


def verify_key_file(client: httpx.Client, key: str, location: str) -> str | None:
    """None when the key file is live and correct, otherwise the reason it is not.

    Checked before every submission: a POST with an unverifiable key earns a 403
    at best, and repeated ones are how a host gets throttled.
    """
    fetched = fetch(client, location)
    if not fetched.ok:
        return f"{location} returned HTTP {fetched.status}"
    if fetched.text.strip() != key:
        return f"{location} does not contain the key"
    return None


@dataclass(frozen=True)
class BatchResult:
    urls: tuple[str, ...]
    status: int
    detail: str

    @property
    def accepted(self) -> bool:
        return self.status in ACCEPTED

    def to_dict(self) -> dict:
        return {"count": len(self.urls), "status": self.status, "detail": self.detail,
                "accepted": self.accepted}


def submit(client: httpx.Client, config: Config, key: str, location: str,
           urls: list[str], endpoint: str = ENDPOINT) -> list[BatchResult]:
    """POST `urls` to IndexNow in batches. A 200 means received, not indexed."""
    host = urlsplit(config.site_url).netloc
    results: list[BatchResult] = []
    for start in range(0, len(urls), MAX_URLS_PER_POST):
        batch = urls[start:start + MAX_URLS_PER_POST]
        try:
            response = client.post(
                endpoint,
                json={"host": host, "key": key, "keyLocation": location,
                      "urlList": batch},
                headers={"Content-Type": "application/json; charset=utf-8"})
            status = response.status_code
            detail = STATUS_MEANING.get(status, f"unexpected HTTP {status}")
        except httpx.HTTPError as exc:
            status, detail = 0, f"request failed: {exc}"
        results.append(BatchResult(tuple(batch), status, detail))
    return results


def record(state: dict, found: Plan, results: list[BatchResult]) -> dict:
    """State after a submission: only URLs in an accepted batch are marked sent.

    A URL in a rejected batch keeps its old hash, so the next run tries it again
    instead of believing it was delivered.
    """
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    updated = dict(state)
    for result in results:
        if not result.accepted:
            continue
        for url in result.urls:
            updated[url] = {"hash": found.hashes[url], "submittedAt": now}
    return updated
