---
name: schema
description: Use when asked to write, generate or fix JSON-LD or structured data for a site or page, add Organization, WebPage, BreadcrumbList, FAQPage or Article markup, or clear a seo.schema finding from an audit.
---

# Schema

Generate JSON-LD from what each page and the site config actually state.

## When to use

"Write our JSON-LD", "add structured data", "fix the schema errors", or an audit that
reports `seo.schema.absent`.

## When NOT to use

- Diagnosing what is wrong with existing markup — that is `audit`.
- `llms.txt` or `facts.json` — that is `geo-artifacts`.
- Putting the markup live on Webflow — generate here, then use `publish`.
- Product, Review, Event or Recipe markup. This skill does not generate those types,
  and their required values (price, rating, date) must come from the owner.

## How to run it

```bash
rankwright schema --config rankwright.config.json
rankwright schema https://example.com --page https://example.com/pricing
```

Output goes to `.rankwright/schema/`: `<slug>.jsonld`, a paste-ready `<slug>.html`, and
`manifest.json`. Nothing is published.

Without `--config` the organisation is unknown, so only page-level nodes are produced.
For Organization and WebSite nodes, fill `site`, `nap` and `sameAs` in the config first,
and ask the user for any value you do not have. Never guess an address or phone number.

## Rules

1. **Do not add what the command left out.** A missing Article node means the page has
   no publish date, image or headline. Read the `note:` lines and tell the user what the
   page needs; do not hand-write the node with invented values.
2. **Never add AggregateRating or Review** unless the user supplies real, countable
   reviews. The audit flags unbacked ratings as an error.
3. **Relay every note.** A breadcrumb "named from the URL segment" is a guess at a
   label and needs a human check.
4. **Check FAQPage by eye.** Any two `<details>` elements produce one, including ones
   that are not questions.
5. After the markup is on the page, re-run `rankwright audit` and confirm the `schema`
   gates are clean.
