import json

import httpx
import respx

from rankwright.config import Config
from rankwright.fetch import Fetched
from rankwright.gates import jsonld
from rankwright.page import PageData
from rankwright.schema_gen import (build_for_page, faq_pairs, generate,
                                   organization_node, render_script)

SITE = "https://x.example"


def cfg(**extra) -> Config:
    return Config({
        "site": {"name": "X Studio", "legalName": "X Studio LLC", "url": SITE,
                 "entityType": "Organization",
                 "locales": [{"code": "en", "path": "/", "default": True}]},
        "nap": {"street": "1 Main St", "city": "Las Vegas", "region": "NV",
                "country": "US", "phone": "+1 702 555 0100",
                "geo": {"lat": 36.1, "lng": -115.1}},
        "sameAs": {"linkedin": "https://linkedin.com/company/x", "x": None},
        "identifiers": {"duns": "123"},
        **extra,
    })


def page(url: str, body: str, head: str = "") -> PageData:
    html = (f'<!doctype html><html lang="en"><head><title>Pricing | X</title>'
            f'<meta name="description" content="What it costs.">{head}</head>'
            f"<body>{body}</body></html>")
    return PageData.from_fetched(Fetched(url, 200, {}, html, 5))


def types(result) -> list[str]:
    return [n["@type"] for n in result.graph["@graph"]]


def node(result, type_name: str) -> dict:
    return next(n for n in result.graph["@graph"] if n["@type"] == type_name)


def test_organization_uses_only_configured_facts():
    org, notes = organization_node(cfg())
    assert org["@id"] == f"{SITE}/#organization"
    assert org["address"] == {"@type": "PostalAddress", "streetAddress": "1 Main St",
                              "addressLocality": "Las Vegas", "addressRegion": "NV",
                              "addressCountry": "US"}
    assert org["sameAs"] == ["https://linkedin.com/company/x"]      # null dropped
    assert org["identifier"] == [
        {"@type": "PropertyValue", "propertyID": "duns", "value": "123"}]
    assert "aggregateRating" not in org and "logo" not in org
    assert notes == []


def test_local_business_without_address_is_noted_not_invented():
    config = Config({"site": {"name": "X", "url": SITE, "entityType": "LocalBusiness"}})
    org, notes = organization_node(config)
    assert "address" not in org
    assert any("address" in n for n in notes)


def test_inner_page_gets_webpage_and_breadcrumb():
    result = build_for_page(cfg(), page(f"{SITE}/services/pricing", "<h1>Pricing</h1>"),
                            titles={f"{SITE}/services": "Services"})
    assert types(result) == ["Organization", "WebSite", "WebPage", "BreadcrumbList"]
    crumbs = node(result, "BreadcrumbList")["itemListElement"]
    assert [(c["position"], c["name"]) for c in crumbs] == [
        (1, "Home"), (2, "Services"), (3, "Pricing")]
    assert result.notes == []
    webpage = node(result, "WebPage")
    assert webpage["description"] == "What it costs."
    assert webpage["isPartOf"] == {"@id": f"{SITE}/#website"}


def test_breadcrumb_named_from_slug_says_so():
    result = build_for_page(cfg(), page(f"{SITE}/our-work/case", "<h1>Case</h1>"))
    crumbs = node(result, "BreadcrumbList")["itemListElement"]
    assert crumbs[1]["name"] == "Our work"
    assert any("named from the URL segment" in n for n in result.notes)


def test_home_page_has_no_breadcrumb_and_is_about_the_organization():
    result = build_for_page(cfg(), page(f"{SITE}/", "<h1>X</h1>"))
    assert "BreadcrumbList" not in types(result)
    assert node(result, "WebPage")["about"] == {"@id": f"{SITE}/#organization"}


def test_canonical_is_used_for_ids():
    result = build_for_page(cfg(), page(
        f"{SITE}/p?utm=1", "<h1>P</h1>", f'<link rel="canonical" href="{SITE}/p">'))
    assert node(result, "WebPage")["@id"] == f"{SITE}/p#webpage"


def test_faq_only_from_real_markup():
    body = ("<h1>FAQ</h1><details><summary>Is it free?</summary><p>Yes, it is.</p></details>"
            "<dl><dt>Who made it?</dt><dd>We did.</dd></dl>")
    result = build_for_page(cfg(), page(f"{SITE}/faq", body))
    questions = node(result, "FAQPage")["mainEntity"]
    assert [(q["name"], q["acceptedAnswer"]["text"]) for q in questions] == [
        ("Is it free?", "Yes, it is."), ("Who made it?", "We did.")]


def test_single_details_is_not_an_faq():
    body = "<h1>P</h1><details><summary>More</summary><p>Text</p></details>"
    assert "FAQPage" not in types(build_for_page(cfg(), page(f"{SITE}/p", body)))
    assert len(faq_pairs(page(f"{SITE}/p", body).soup())) == 1


def test_article_needs_headline_image_and_date():
    head = ('<meta property="og:type" content="article">'
            '<meta property="og:image" content="https://x.example/a.png">'
            '<meta property="article:published_time" content="2026-10-01T09:00:00Z">'
            '<meta name="author" content="Jo Writer">')
    result = build_for_page(cfg(), page(f"{SITE}/blog/post", "<h1>Post</h1>", head))
    article = node(result, "Article")
    assert article["headline"] == "Post"
    assert article["datePublished"] == "2026-10-01T09:00:00Z"
    assert article["author"] == {"@type": "Person", "name": "Jo Writer"}


def test_article_without_a_date_is_skipped_with_the_reason():
    head = ('<meta property="og:type" content="article">'
            '<meta property="og:image" content="https://x.example/a.png">')
    result = build_for_page(cfg(), page(f"{SITE}/blog/post", "<h1>Post</h1>", head))
    assert "Article" not in types(result)
    assert any("publish date" in n and "skipped" in n for n in result.notes)


def test_types_already_on_the_page_are_not_duplicated():
    head = ('<script type="application/ld+json">{"@context":"https://schema.org",'
            '"@type":"Organization","name":"X","url":"https://x.example"}</script>')
    result = build_for_page(cfg(), page(f"{SITE}/", "<h1>X</h1>", head))
    assert "Organization" not in types(result)
    assert any("already declares Organization" in n for n in result.notes)


def test_existing_organization_blocks_a_different_subtype_too():
    head = ('<script type="application/ld+json">{"@context":"https://schema.org",'
            '"@type":"Organization","name":"X","url":"https://x.example"}</script>')
    config = Config({"site": {"name": "X", "url": SITE, "entityType": "LocalBusiness"}})
    result = build_for_page(config, page(f"{SITE}/", "<h1>X</h1>", head))
    assert "LocalBusiness" not in types(result)


def test_generated_markup_passes_the_audit_schema_gate():
    head = ('<meta property="og:type" content="article">'
            '<meta property="og:image" content="https://x.example/a.png">'
            '<meta property="article:published_time" content="2026-10-01">')
    body = ("<article><h1>Post</h1></article>"
            "<details><summary>Q1?</summary>A1</details>"
            "<details><summary>Q2?</summary>A2</details>")
    source = page(f"{SITE}/blog/post", body, head)
    result = build_for_page(cfg(), source)
    assert set(types(result)) == {"Organization", "WebSite", "WebPage",
                                  "BreadcrumbList", "FAQPage", "Article"}
    html = source.html.replace("</head>", render_script(result.graph) + "</head>")
    assert jsonld.run(html, source.url) == []


def test_script_cannot_be_closed_by_page_text():
    graph = {"@context": "https://schema.org",
             "@graph": [{"@type": "WebPage", "name": "a </script><script>alert(1)"}]}
    snippet = render_script(graph)
    assert snippet.count("</script>") == 1
    inner = snippet.split("\n", 1)[1].rsplit("</script>", 1)[0]
    assert json.loads(inner) == graph             # still the same JSON


@respx.mock
def test_generate_writes_files_and_reports_unreachable(tmp_path):
    respx.get(f"{SITE}/sitemap.xml").mock(return_value=httpx.Response(200, text=(
        f"<urlset><url><loc>{SITE}/</loc></url><url><loc>{SITE}/gone</loc></url>"
        "</urlset>")))
    respx.get(f"{SITE}/").mock(return_value=httpx.Response(
        200, text="<html><head><title>X</title></head><body><h1>X</h1></body></html>"))
    respx.get(f"{SITE}/gone").mock(return_value=httpx.Response(404))
    results, unreachable = generate(cfg(), tmp_path)
    assert [r.url for r in results] == [f"{SITE}/"]
    assert unreachable == [f"{SITE}/gone"]
    graph = json.loads((tmp_path / "index.jsonld").read_text())
    assert graph["@context"] == "https://schema.org"
    assert (tmp_path / "index.html").read_text().startswith(
        '<script type="application/ld+json">')
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["unreachable"] == [f"{SITE}/gone"]
    assert manifest["pages"][0]["snippet"] == "index.html"


def test_relative_og_image_is_made_absolute():
    result = build_for_page(cfg(), page(
        f"{SITE}/a/b", "<h1>B</h1>", '<meta property="og:image" content="/img/og.png">'))
    assert node(result, "WebPage")["primaryImageOfPage"]["url"] == f"{SITE}/img/og.png"


def test_zero_config_run_does_not_invent_an_organization():
    from rankwright.audit import default_config
    result = build_for_page(default_config(SITE), page(f"{SITE}/", "<h1>X</h1>"))
    assert types(result) == ["WebPage"]
    webpage = node(result, "WebPage")
    assert "isPartOf" not in webpage and "about" not in webpage
    assert any("--config" in n for n in result.notes)
