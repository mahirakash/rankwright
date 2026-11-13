"""Generate JSON-LD from what a page and the site config actually say.

The rule this module lives by is the same one the audit's schema-fabrication gate
enforces from the other side: nothing is emitted that the page or the config does
not state. There is no AggregateRating, Review, price or author here unless a real
value was found, and a node whose Google-required properties cannot be filled from
real data is skipped with a note rather than emitted half-empty -- a half-empty
Article is exactly what `seo.schema-required.missing-property` exists to flag.

Every page gets one `@graph` holding the site-level entity and WebSite (declared
once, by stable `@id`) plus the page's own WebPage, and a BreadcrumbList, FAQPage
or article node only when the page has the markup to back it.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup

from .config import Config
from .fetch import fetch, make_client, read_sitemap
from .gates.jsonld import extract_blocks
from .html import find_meta, find_rel
from .page import PageData

CONTEXT = "https://schema.org"
# Below this a list of <details>/<dt> pairs is more likely a single disclosure
# widget or a glossary entry than an FAQ section.
MIN_FAQ_PAIRS = 2
ARTICLE_TYPES = ("Article", "NewsArticle", "BlogPosting")


@dataclass
class SchemaResult:
    """What was generated for one URL, and what was deliberately left out."""

    url: str
    graph: dict
    types: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"url": self.url, "types": self.types, "notes": self.notes}


def _text(node) -> str:
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)) if node else ""


def _meta_property(soup: BeautifulSoup, prop: str) -> str:
    tag = soup.find("meta", attrs={
        "property": lambda v: v is not None and v.strip().lower() == prop})
    return (tag.get("content") or "").strip() if tag else ""


def _meta_name(soup: BeautifulSoup, name: str) -> str:
    tag = find_meta(soup, name)
    return (tag.get("content") or "").strip() if tag else ""


def org_id(config: Config) -> str:
    return f"{config.site_url}/#organization"


def website_id(config: Config) -> str:
    return f"{config.site_url}/#website"


def _address(nap: dict) -> dict | None:
    mapping = (("street", "streetAddress"), ("city", "addressLocality"),
               ("region", "addressRegion"), ("postalCode", "postalCode"),
               ("country", "addressCountry"))
    address = {target: nap[source] for source, target in mapping if nap.get(source)}
    return {"@type": "PostalAddress", **address} if address else None


def organization_node(config: Config) -> tuple[dict, list[str]]:
    site = config.raw["site"]
    nap = config.raw.get("nap") or {}
    notes: list[str] = []
    node: dict = {"@type": config.entity_type, "@id": org_id(config),
                  "name": site["name"], "url": config.site_url + "/"}
    if site.get("legalName"):
        node["legalName"] = site["legalName"]
    address = _address(nap)
    if address:
        node["address"] = address
    elif config.entity_type in {"LocalBusiness", "ProfessionalService",
                                "MedicalOrganization"}:
        notes.append(
            f"{config.entity_type} has no address: add nap.street/city/country to "
            "the config. Google requires an address for a local-business result.")
    if nap.get("phone"):
        node["telephone"] = nap["phone"]
    if nap.get("email"):
        node["email"] = nap["email"]
    if nap.get("geo"):
        node["geo"] = {"@type": "GeoCoordinates", "latitude": nap["geo"]["lat"],
                       "longitude": nap["geo"]["lng"]}
    if nap.get("openingHours"):
        node["openingHours"] = nap["openingHours"]
    same_as = [v for v in (config.raw.get("sameAs") or {}).values() if v]
    if same_as:
        node["sameAs"] = same_as
    identifiers = config.raw.get("identifiers") or {}
    if identifiers:
        node["identifier"] = [
            {"@type": "PropertyValue", "propertyID": key, "value": value}
            for key, value in identifiers.items()]
    if site.get("parentOrganization"):
        node["parentOrganization"] = {"@type": "Organization",
                                      "name": site["parentOrganization"]}
    return node, notes


def website_node(config: Config) -> dict:
    node: dict = {"@type": "WebSite", "@id": website_id(config),
                  "url": config.site_url + "/", "name": config.raw["site"]["name"],
                  "publisher": {"@id": org_id(config)}}
    locales = config.raw["site"].get("locales") or []
    default = next((loc["code"] for loc in locales if loc.get("default")), None)
    if default:
        node["inLanguage"] = default
    return node


def _canonical(soup: BeautifulSoup, url: str) -> str:
    tag = find_rel(soup, "link", "canonical")
    href = (tag.get("href") or "").strip() if tag else ""
    return href if href.startswith(("http://", "https://")) else url


def _is_home(config: Config, url: str) -> bool:
    return url.rstrip("/") == config.site_url


def webpage_node(config: Config, page: PageData, canonical: str) -> dict:
    soup = page.soup()
    node: dict = {"@type": "WebPage", "@id": f"{canonical}#webpage", "url": canonical,
                  "isPartOf": {"@id": website_id(config)}}
    title = _text(soup.find("title"))
    if title:
        node["name"] = title
    description = _meta_name(soup, "description")
    if description:
        node["description"] = description
    if page.lang:
        node["inLanguage"] = page.lang
    image = _meta_property(soup, "og:image")
    if image:
        node["primaryImageOfPage"] = {"@type": "ImageObject", "url": image}
    modified = _meta_property(soup, "article:modified_time")
    if modified:
        node["dateModified"] = modified
    if _is_home(config, canonical):
        node["about"] = {"@id": org_id(config)}
    return node


def _humanise(segment: str) -> str:
    words = re.sub(r"[-_]+", " ", segment).strip()
    return words[:1].upper() + words[1:] if words else segment


def breadcrumb_node(config: Config, page: PageData, canonical: str,
                    titles: dict[str, str]) -> tuple[dict | None, list[str]]:
    """A trail built from the URL path. Home pages have none.

    The last crumb is named by the page's own <h1> or <title>. An ancestor is named
    by its real title when that page was fetched in the same run, and by its URL
    segment otherwise -- which the note says, because a slug is a guess at a label.
    """
    parts = urlsplit(canonical)
    if not canonical.startswith(config.site_url):
        return None, []
    segments = [s for s in parts.path.split("/") if s]
    if not segments:
        return None, []
    soup = page.soup()
    items = [{"@type": "ListItem", "position": 1, "name": "Home",
              "item": config.site_url + "/"}]
    notes: list[str] = []
    trail = f"{parts.scheme}://{parts.netloc}"
    for index, segment in enumerate(segments):
        trail = f"{trail}/{segment}"
        last = index == len(segments) - 1
        if last:
            name = _text(soup.find("h1")) or _text(soup.find("title")) or _humanise(segment)
            item_url = canonical
        else:
            name = titles.get(trail) or titles.get(trail + "/")
            if not name:
                name = _humanise(segment)
                notes.append(
                    f"breadcrumb '{name}' is named from the URL segment: {trail} was "
                    "not in this run, so its real title is unknown. Check the label.")
            item_url = trail
        items.append({"@type": "ListItem", "position": index + 2, "name": name,
                      "item": item_url})
    return ({"@type": "BreadcrumbList", "@id": f"{canonical}#breadcrumb",
             "itemListElement": items}, notes)


def faq_pairs(soup: BeautifulSoup) -> list[tuple[str, str]]:
    """Question/answer pairs that exist as markup: <details> and <dl><dt><dd>."""
    pairs: list[tuple[str, str]] = []
    for details in soup.find_all("details"):
        summary = details.find("summary")
        question = _text(summary)
        if summary is not None:
            answer = _text_without(details, summary)
        else:
            answer = ""
        if question and answer:
            pairs.append((question, answer))
    for dt in soup.find_all("dt"):
        dd = dt.find_next_sibling("dd")
        question, answer = _text(dt), _text(dd)
        if question and answer:
            pairs.append((question, answer))
    return pairs


def _text_without(parent, child) -> str:
    """`parent`'s text with `child`'s text removed, without mutating the soup."""
    skip = {id(s) for s in child.strings}
    kept = " ".join(str(s).strip() for s in parent.strings
                    if id(s) not in skip and str(s).strip())
    return re.sub(r"\s+", " ", kept).strip()


def faq_node(page: PageData, canonical: str) -> dict | None:
    pairs = faq_pairs(page.soup())
    if len(pairs) < MIN_FAQ_PAIRS:
        return None
    return {"@type": "FAQPage", "@id": f"{canonical}#faq", "mainEntity": [
        {"@type": "Question", "name": question,
         "acceptedAnswer": {"@type": "Answer", "text": answer}}
        for question, answer in pairs]}


def article_node(config: Config, page: PageData,
                 canonical: str) -> tuple[dict | None, list[str]]:
    """An Article only when the page says it is one and carries Google's required
    headline, image and datePublished. Anything less is skipped with the reason.
    """
    soup = page.soup()
    is_article = (_meta_property(soup, "og:type").lower() == "article"
                  or soup.find("article") is not None)
    if not is_article:
        return None, []
    headline = _text(soup.find("h1"))
    image = _meta_property(soup, "og:image")
    published = _meta_property(soup, "article:published_time")
    if not published:
        scope = soup.find("article") or soup
        time_tag = scope.find("time", attrs={"datetime": True})
        published = (time_tag.get("datetime") or "").strip() if time_tag else ""
    missing = [label for label, value in (
        ("an <h1> headline", headline), ("an og:image", image),
        ("a publish date (article:published_time or <time datetime>)", published),
    ) if not value]
    if missing:
        return None, [
            "looks like an article but has no " + ", no ".join(missing)
            + ": Article markup skipped, because Google requires headline, image "
            "and datePublished and none of them may be invented."]
    node: dict = {"@type": "Article", "@id": f"{canonical}#article",
                  "headline": headline, "image": [image], "datePublished": published,
                  "mainEntityOfPage": {"@id": f"{canonical}#webpage"},
                  "publisher": {"@id": org_id(config)}}
    modified = _meta_property(soup, "article:modified_time")
    if modified:
        node["dateModified"] = modified
    author = _meta_name(soup, "author")
    if author:
        node["author"] = {"@type": "Person", "name": author}
    return node, []


def _existing_types(html: str) -> set[str]:
    found: set[str] = set()
    for block in extract_blocks(html):
        raw = block.get("@type", "")
        for value in raw if isinstance(raw, list) else [raw]:
            if isinstance(value, str):
                found.add(value)
    return found


def build_for_page(config: Config, page: PageData,
                   titles: dict[str, str] | None = None) -> SchemaResult:
    """The graph for one page, minus any type the page already declares."""
    soup = page.soup()
    canonical = _canonical(soup, page.url)
    existing = _existing_types(page.html)
    notes: list[str] = []
    nodes: list[dict] = []

    org, org_notes = organization_node(config)
    candidates: list[tuple[dict | None, list[str]]] = [
        (org, org_notes), (website_node(config), []),
        (webpage_node(config, page, canonical), []),
        breadcrumb_node(config, page, canonical, titles or {}),
        (faq_node(page, canonical), []),
        article_node(config, page, canonical),
    ]
    for node, node_notes in candidates:
        notes.extend(node_notes)
        if node is None:
            continue
        node_type = node["@type"]
        clash = node_type in existing or (
            node_type == "Article" and existing & set(ARTICLE_TYPES))
        if clash:
            notes.append(f"page already declares {node_type}: not generated again.")
            continue
        nodes.append(node)

    graph = {"@context": CONTEXT, "@graph": nodes}
    return SchemaResult(url=page.url, graph=graph,
                        types=[n["@type"] for n in nodes], notes=notes)


def render_json(graph: dict) -> str:
    return json.dumps(graph, indent=2, ensure_ascii=False)


def render_script(graph: dict) -> str:
    """A paste-ready <script> tag.

    `</` is escaped so a title or answer containing "</script>" cannot close the
    tag early -- that is valid JSON (`\\/` is an allowed escape) and it is the
    difference between structured data and an injection point.
    """
    body = render_json(graph).replace("</", "<\\/")
    return f'<script type="application/ld+json">\n{body}\n</script>\n'


def _slug(config: Config, url: str) -> str:
    path = urlsplit(url).path.strip("/") if url.startswith(config.site_url) else url
    slug = re.sub(r"[^A-Za-z0-9]+", "-", path).strip("-").lower()
    return slug or "index"


def generate(config: Config, out_dir: str | Path, client: httpx.Client | None = None,
             urls: list[str] | None = None) -> tuple[list[SchemaResult], list[str]]:
    """Fetch pages, write `<slug>.jsonld`, `<slug>.html` and `manifest.json`.

    Returns the results and the URLs that could not be fetched. An unreachable URL
    is reported, never skipped silently.
    """
    owns_client = client is None
    client = client or make_client()
    try:
        targets = urls or read_sitemap(client, config.site_url, config.sample_size) or [
            config.site_url + "/"]
        targets = list(dict.fromkeys(targets))
        pages: list[PageData] = []
        unreachable: list[str] = []
        for url in targets:
            result = fetch(client, url)
            if result.ok:
                pages.append(PageData.from_fetched(result))
            else:
                unreachable.append(url)
        titles = {p.url: (_text(p.soup().find("h1")) or _text(p.soup().find("title")))
                  for p in pages}

        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        results: list[SchemaResult] = []
        manifest: list[dict] = []
        used: set[str] = set()
        for page in pages:
            result = build_for_page(config, page, titles)
            slug = base = _slug(config, page.url)
            suffix = 2
            while slug in used:
                slug = f"{base}-{suffix}"
                suffix += 1
            used.add(slug)
            (out / f"{slug}.jsonld").write_text(render_json(result.graph) + "\n")
            (out / f"{slug}.html").write_text(render_script(result.graph))
            manifest.append({**result.to_dict(), "jsonld": f"{slug}.jsonld",
                             "snippet": f"{slug}.html"})
            results.append(result)
        (out / "manifest.json").write_text(json.dumps(
            {"site": config.site_url, "pages": manifest, "unreachable": unreachable},
            indent=2) + "\n")
        return results, unreachable
    finally:
        if owns_client:
            client.close()
