"""Rank tracking from two sources that answer different questions.

`gsc` reads Google Search Console's Search Analytics. It is Google's own data and
free, but it only knows queries the site already earned an impression for, its
positions are averages over the window, and the newest two or three days are
missing. It cannot say anything about a competitor.

`serpapi` asks a paid results API where a domain sits for a keyword right now, so
it works for keywords the site does not rank for yet and for competitors. Each
keyword costs at least one billed search.

Every run appends one snapshot to a JSON-lines history file, and the report is the
difference from the previous snapshot of the same provider. The two providers are
never compared with each other: an averaged position and a point-in-time position
are different measurements.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx

from .config import Config, ConfigError

GSC_ENDPOINT = "https://www.googleapis.com/webmasters/v3/sites/{site}/searchAnalytics/query"
GSC_SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"
# Search Console data for the most recent days is incomplete, so the window ends
# this many days before today.
GSC_LAG_DAYS = 3
SERPAPI_ENDPOINT = "https://serpapi.com/search.json"
SERP_PAGE_SIZE = 10
DEFAULT_HISTORY = ".rankwright/rank/history.jsonl"
PROVIDERS = ("gsc", "serpapi")


@dataclass
class Row:
    keyword: str
    position: float | None
    url: str | None = None
    clicks: int | None = None
    impressions: int | None = None
    ctr: float | None = None
    competitors: dict[str, int | None] = field(default_factory=dict)

    def to_dict(self) -> dict:
        d: dict = {"keyword": self.keyword, "position": self.position}
        for key in ("url", "clicks", "impressions", "ctr"):
            value = getattr(self, key)
            if value is not None:
                d[key] = value
        if self.competitors:
            d["competitors"] = self.competitors
        return d


@dataclass
class Snapshot:
    provider: str
    site: str
    taken_at: str
    rows: list[Row]
    window: dict | None = None

    def to_dict(self) -> dict:
        d: dict = {"provider": self.provider, "site": self.site,
                   "takenAt": self.taken_at, "rows": [r.to_dict() for r in self.rows]}
        if self.window:
            d["window"] = self.window
        return d


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def rank_config(config: Config) -> dict:
    return config.raw.get("rank") or {}


def keywords(config: Config) -> list[str]:
    return [k.strip() for k in rank_config(config).get("keywords", []) if k.strip()]


# --- Google Search Console ---------------------------------------------------

def gsc_property(config: Config) -> str:
    """The Search Console property: `sc-domain:example.com` or a URL-prefix."""
    explicit = (rank_config(config).get("gscProperty")
                or config.raw.get("indexing", {}).get("gscProperty"))
    return explicit or config.site_url + "/"


def gsc_token(config: Config) -> str:
    """An OAuth access token for the Search Console API.

    Either a ready token in the `gsc` secret, or a service-account key file named
    by GOOGLE_APPLICATION_CREDENTIALS (needs `pip install "rankwright[gsc]"`, and
    the service account must be added as a user on the property).
    """
    if "gsc" in config.raw.get("secrets", {}):
        return config.secret("gsc")
    key_file = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if key_file:
        try:
            from google.auth.transport.requests import Request
            from google.oauth2 import service_account
        except ImportError as exc:
            raise ConfigError(
                'Service-account auth needs google-auth: pip install "rankwright[gsc]".'
            ) from exc
        credentials = service_account.Credentials.from_service_account_file(
            key_file, scopes=[GSC_SCOPE])
        credentials.refresh(Request())
        return credentials.token
    raise ConfigError(
        'No Search Console credentials. Set "secrets": {"gsc": "env:GSC_ACCESS_TOKEN"} '
        "or point GOOGLE_APPLICATION_CREDENTIALS at a service-account key file.")


def gsc_window(days: int, today: date | None = None) -> tuple[str, str]:
    end = (today or datetime.now(UTC).date()) - timedelta(days=GSC_LAG_DAYS)
    start = end - timedelta(days=days - 1)
    return start.isoformat(), end.isoformat()


def fetch_gsc(client: httpx.Client, config: Config, token: str, *, days: int = 28,
              top: int = 50, today: date | None = None) -> Snapshot:
    start, end = gsc_window(days, today)
    body: dict = {"startDate": start, "endDate": end, "dimensions": ["query", "page"],
                  "rowLimit": 5000}
    country = rank_config(config).get("country")
    if country:
        body["dimensionFilterGroups"] = [{"filters": [{
            "dimension": "country", "operator": "equals", "expression": country}]}]
    url = GSC_ENDPOINT.format(site=quote(gsc_property(config), safe=""))
    try:
        response = client.post(url, json=body,
                               headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as exc:
        raise ConfigError(
            f"Search Console request failed: {type(exc).__name__}") from None
    if response.status_code != 200:
        raise ConfigError(
            f"Search Console returned HTTP {response.status_code} for "
            f"{gsc_property(config)}: {_error_text(response)}")

    # One query can rank with several pages. Keep the page with the most
    # impressions as the row; that is the page Google mostly shows for it.
    best: dict[str, dict] = {}
    for item in response.json().get("rows", []):
        query, page = item["keys"][0], item["keys"][1]
        current = best.get(query)
        if current is None or item.get("impressions", 0) > current.get("impressions", 0):
            best[query] = {**item, "page": page}

    def to_row(query: str, item: dict) -> Row:
        return Row(keyword=query, position=round(item["position"], 1), url=item["page"],
                   clicks=int(item.get("clicks", 0)),
                   impressions=int(item.get("impressions", 0)),
                   ctr=round(item.get("ctr", 0.0), 4))

    wanted = keywords(config)
    if wanted:
        lowered = {q.lower(): q for q in best}
        rows = []
        for keyword in wanted:
            match = lowered.get(keyword.lower())
            # No row means no impression in the window, which is not the same as
            # "ranks nowhere": position stays None rather than an invented number.
            rows.append(to_row(keyword, best[match]) if match else Row(keyword, None))
    else:
        ordered = sorted(best.items(), key=lambda kv: -kv[1].get("impressions", 0))
        rows = [to_row(query, item) for query, item in ordered[:top]]
    return Snapshot("gsc", config.site_url, _now(), rows,
                    window={"start": start, "end": end})


# --- SerpAPI -----------------------------------------------------------------

def _domain(value: str) -> str:
    host = urlsplit(value).netloc if "://" in value else value.split("/")[0]
    return host.lower().removeprefix("www.")


def _matches(link: str, domain: str) -> bool:
    host = _domain(link)
    return host == domain or host.endswith("." + domain)


def fetch_serpapi(client: httpx.Client, config: Config, api_key: str, *,
                  depth: int | None = None) -> Snapshot:
    wanted = keywords(config)
    if not wanted:
        raise ConfigError(
            'The serpapi provider needs keywords: set "rank": {"keywords": [...]}.')
    settings = rank_config(config)
    depth = depth or settings.get("depth", 30)
    site = _domain(config.site_url)
    competitors = [_domain(c) for c in config.raw.get("competitors", [])]

    rows: list[Row] = []
    for keyword in wanted:
        found: dict[str, tuple[int, str]] = {}
        seen = 0
        while seen < depth:
            params = {"engine": "google", "q": keyword, "start": seen,
                      "num": SERP_PAGE_SIZE, "api_key": api_key}
            if settings.get("country"):
                params["gl"] = settings["country"].lower()
            if settings.get("language"):
                params["hl"] = settings["language"]
            if settings.get("location"):
                params["location"] = settings["location"]
            try:
                response = client.get(SERPAPI_ENDPOINT, params=params)
            except httpx.HTTPError as exc:
                # `from None` and the bare class name on purpose: the request URL
                # carries the API key, and httpx puts the URL in its messages.
                raise ConfigError(
                    f"SerpAPI request failed: {type(exc).__name__}") from None
            if response.status_code != 200:
                raise ConfigError(
                    f"SerpAPI returned HTTP {response.status_code} for {keyword!r}: "
                    f"{_error_text(response)}")
            organic = response.json().get("organic_results", [])
            if not organic:
                break
            for offset, result in enumerate(organic, start=1):
                link = result.get("link", "")
                for domain in (site, *competitors):
                    if domain not in found and _matches(link, domain):
                        found[domain] = (seen + offset, link)
            seen += len(organic)
            if site in found and all(c in found for c in competitors):
                break
        own = found.get(site)
        rows.append(Row(
            keyword=keyword, position=own[0] if own else None,
            url=own[1] if own else None,
            competitors={c: (found[c][0] if c in found else None) for c in competitors}))
    return Snapshot("serpapi", config.site_url, _now(), rows,
                    window={"depth": depth})


def _error_text(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return response.text[:200]
    error = data.get("error", data) if isinstance(data, dict) else data
    if isinstance(error, dict):
        return str(error.get("message", error))[:200]
    return str(error)[:200]


# --- history and comparison --------------------------------------------------

def load_history(path: str | Path) -> list[dict]:
    p = Path(path)
    if not p.is_file():
        return []
    entries = []
    for line in p.read_text().splitlines():
        if line.strip():
            entries.append(json.loads(line))
    return entries


def previous_snapshot(history: list[dict], provider: str, site: str) -> dict | None:
    for entry in reversed(history):
        if entry.get("provider") == provider and entry.get("site") == site:
            return entry
    return None


def append_history(path: str | Path, snapshot: Snapshot) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as handle:
        handle.write(json.dumps(snapshot.to_dict()) + "\n")


def compare(snapshot: Snapshot, previous: dict | None) -> list[dict]:
    """Each row with its movement since `previous`.

    `change` is previous minus current, so a positive number is an improvement
    (position 8 to position 5 is +3). `status` is one of new, lost, up, down,
    same, absent or first-run.
    """
    before = {r["keyword"]: r.get("position") for r in (previous or {}).get("rows", [])}
    out = []
    for row in snapshot.rows:
        entry = row.to_dict()
        if previous is None:
            entry["status"] = "first-run"
        elif row.keyword not in before:
            entry["status"] = "new" if row.position is not None else "absent"
        else:
            old = before[row.keyword]
            if row.position is None:
                entry["status"] = "lost" if old is not None else "absent"
            elif old is None:
                entry["status"] = "new"
            else:
                change = round(old - row.position, 1)
                entry["previous"] = old
                entry["change"] = change
                entry["status"] = "up" if change > 0 else "down" if change < 0 else "same"
        out.append(entry)
    return out


def format_table(snapshot: Snapshot, rows: list[dict]) -> str:
    lines = [f"{snapshot.provider} · {snapshot.site} · {len(rows)} keyword(s)"]
    if snapshot.provider == "gsc" and snapshot.window:
        lines.append(f"  window {snapshot.window['start']} to {snapshot.window['end']} "
                     "(average position; latest days excluded)")
    for row in rows:
        position = "—" if row["position"] is None else f"{row['position']:g}"
        if "change" in row:
            sign = "+" if row["change"] > 0 else ""
            move = f"{sign}{row['change']:g}"
        else:
            move = row["status"]
        extra = ""
        if "impressions" in row:
            extra = f"  {row['clicks']} clicks / {row['impressions']} impr"
        if row.get("competitors"):
            extra += "  vs " + ", ".join(
                f"{domain} {'—' if pos is None else pos}"
                for domain, pos in row["competitors"].items())
        lines.append(f"  {position:>5}  {move:>9}  {row['keyword']}{extra}")
    return "\n".join(lines)
