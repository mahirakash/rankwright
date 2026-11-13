# Schema, IndexNow, rank tracking and publishing

Four commands added in 0.5.0. `audit`, `geo` and `fix` tell you what is wrong; these
act on it.

| Command | What it does | Writes outside your machine? |
|---|---|---|
| `rankwright schema` | Generates JSON-LD from each page's real content | Never |
| `rankwright index` | Submits new and changed URLs through IndexNow | Only with `--submit` |
| `rankwright rank` | Records keyword positions and shows movement | Never (read-only APIs) |
| `rankwright publish webflow` | Applies approved SEO fields and JSON-LD to Webflow | Only with `--apply` |
| `rankwright publish social` | Drafts posts from a page and sends approved ones | Only with `--apply` |

One rule covers all of them: **the default run is a preview.** Nothing is submitted,
written or posted until you pass the flag that says so, and publishing additionally
needs `"approved": true` on each item in a file you can read and edit first.

A complete starting config for a Webflow client site, with every section these commands
read, is in `templates/rankwright.webflow-client.example.json`.

## What was and was not tested

Read this before pointing any of it at a client site.

- `schema` and `index` were run for real against a locally served site, including
  submissions to a local stand-in for the IndexNow endpoint.
- `rank`, `publish webflow` and `publish social` are tested against **mocked** API
  responses built from each provider's published documentation. They have not been run
  against a live Search Console property, SerpAPI key, Webflow site, Facebook Page,
  Instagram account or LinkedIn organisation. Expect to find a rough edge on the first
  live run; preview first, and start with one page or one post.

## `rankwright schema`

```bash
rankwright schema --config rankwright.config.json            # every sitemap URL
rankwright schema https://example.com --page https://example.com/pricing
```

For each page it writes `<slug>.jsonld`, a paste-ready `<slug>.html` script tag, and a
`manifest.json`, into `.rankwright/schema/` (change with `--out`). It publishes nothing.

Each page gets one `@graph`:

| Node | Emitted when | Built from |
|---|---|---|
| Organization (your `site.entityType`) | A config file names the site | `site`, `nap`, `sameAs`, `identifiers` |
| WebSite | A config file names the site | `site`, default locale |
| WebPage | Always | `<title>`, meta description, `<html lang>`, `og:image`, canonical |
| BreadcrumbList | The URL has a path | URL segments; the page's `<h1>` for the last crumb |
| FAQPage | The page has 2 or more `<details>` or `<dt>`/`<dd>` pairs | That markup, verbatim |
| Article | The page says it is an article **and** has an `<h1>`, an `og:image` and a publish date | `og:type`, `<article>`, `article:published_time`, `<time datetime>`, `meta name=author` |

What it will not do:

- **Invent anything.** No ratings, reviews, prices, logos or authors unless the page or
  config states them. An article with no publish date gets no Article node, and the
  output says why.
- **Duplicate.** A type the page already declares is skipped and noted.
- **Guess silently.** A breadcrumb named from a URL segment (because the parent page
  was not in the run) is flagged so you can check the label.
- **Describe an organisation it does not know.** Without `--config` only the WebPage,
  BreadcrumbList, FAQPage and Article nodes are produced.

Two things to check by eye: a page with two or more `<details>` elements that are not
questions (a filter panel, say) will still produce an FAQPage, and Google shows FAQ rich
results only for a narrow set of sites, so treat FAQPage as an answer-engine signal
rather than a promised search feature.

Generated markup is covered by the same gate the audit uses: the test suite inserts it
into a page and asserts the `schema` gates report nothing.

## `rankwright index`

IndexNow notifies Bing, Yandex, Seznam, Naver and other participating engines that a
URL changed. **Google does not use IndexNow.** For Google, submit your sitemap in Search
Console.

```bash
rankwright index https://example.com --keygen public   # 1. create public/<key>.txt
#    deploy so https://example.com/<key>.txt is live
rankwright index https://example.com                   # 2. preview
rankwright index https://example.com --key <key> --submit
```

How it decides what to send: it hashes each page's title, description and visible text
and compares with `.rankwright/indexing/state.json`. New and changed URLs are submitted;
unchanged ones are not. Scripts and attributes are excluded from the hash, so a rotated
nonce or build id does not count as a change. `--all` overrides this.

Safeguards:

- Before sending, the key file is fetched from the site. If it is missing or wrong,
  nothing is sent and the command exits `1`.
- URLs on another host are set aside, because one of them makes IndexNow reject the
  whole batch.
- State is updated only for URLs in a batch the endpoint accepted (HTTP 200 or 202), so
  a rejected batch is retried next run.
- A 200 means *received*. Whether and when a URL is crawled is the engine's decision.

The key comes from `--key`, then the `indexnow` secret, then `indexing.indexnowKeyFile`.
The state file has to survive between runs, or every run looks like a first run and
resubmits everything. `.rankwright/` is git-ignored, so in CI either cache that
directory or point `--state` at a tracked path.

Exit codes: `0` nothing to do or accepted, `1` blocked or rejected, `2` configuration.

## `rankwright rank`

Two providers that measure different things. They are never compared with each other.

| | `gsc` | `serpapi` |
|---|---|---|
| Source | Google Search Console | SerpAPI (paid) |
| Cost | Free | One or more billed searches per keyword per run |
| Position | Average over the window | Where the domain is right now |
| Keywords | Only queries that already got an impression | Any keyword |
| Competitors | No | Yes, from `competitors` in config |
| Freshness | Latest 3 days excluded | Live |

```json
{
  "rank": {
    "provider": "gsc",
    "gscProperty": "sc-domain:example.com",
    "keywords": ["flight school las vegas", "private pilot license cost"],
    "country": "usa",
    "days": 28
  },
  "competitors": ["rival.example"],
  "secrets": { "gsc": "env:GSC_ACCESS_TOKEN", "serpapi": "env:SERPAPI_KEY" }
}
```

```bash
rankwright rank --config rankwright.config.json
rankwright rank --config rankwright.config.json --provider serpapi --json
```

Each run appends a snapshot to `.rankwright/rank/history.jsonl` and prints the change
from the previous snapshot of the same provider. `change` is previous minus current, so
`+3` means three places better.

Search Console credentials, either:

- an OAuth access token in the `gsc` secret, or
- a service-account key file named by `GOOGLE_APPLICATION_CREDENTIALS`, after
  `pip install "rankwright[gsc]"` and adding the service account as a user on the
  property. This is the option that works unattended; access tokens expire in an hour.

Notes:

- With no `keywords`, `gsc` keeps the top queries by impressions (`--top`, default 50).
- A configured keyword with no impressions in the window has position `—`. That means
  "not seen", which is not the same as "ranks nowhere".
- `country` is a three-letter code for `gsc` (`usa`) and a two-letter code for
  `serpapi` (`us`).
- `serpapi` looks `depth` results deep (default 30) in pages of 10, and stops early once
  your domain and every competitor are found. Thirty keywords at depth 30 is up to 90
  billed searches a run.

## `rankwright publish webflow`

Updates SEO title and meta description on static Webflow pages, and can attach
generated JSON-LD.

```json
{
  "publish": { "webflow": { "siteId": "<site id>" } },
  "secrets": { "webflow": "env:WEBFLOW_TOKEN" }
}
```

The token needs `pages:read` and `pages:write`; add `custom_code:read`,
`custom_code:write` for JSON-LD and `sites:read`, `sites:write` for `--publish`.

```bash
rankwright publish webflow plan  --config rankwright.config.json   # 1. read, write a plan
#    edit .rankwright/publish/webflow-plan.json                     # 2. propose + approve
rankwright publish webflow apply --config rankwright.config.json   # 3. preview
rankwright publish webflow apply --config rankwright.config.json --apply
```

A plan entry:

```json
{
  "pageId": "65f0...",
  "title": "Pricing",
  "url": "https://example.com/pricing",
  "current": { "seoTitle": "", "seoDescription": "Plans and pricing." },
  "issues": ["SEO title is empty"],
  "proposed": { "seoTitle": "Pricing — Example Studio", "seoDescription": null },
  "jsonld": ".rankwright/schema/pricing.jsonld",
  "approved": true
}
```

`null` in `proposed` means "leave as is". Rankwright does not write titles or
descriptions for you; that text is yours (or Claude's, through the `publish` skill) to
supply and approve.

Guarantees:

- Only `approved: true` entries are written.
- Each page is re-read first. If its live values differ from `current`, the entry is
  refused as `stale` rather than overwriting someone's edit. Rebuild the plan.
- `plan` refuses to overwrite an existing plan file without `--force`, so approvals are
  not lost by accident.
- Collection template pages, drafts and archived pages are skipped. A template's SEO
  fields are bound to CMS data, and a literal value would replace the binding for every
  item.
- `--apply` saves to Webflow. It is **not live** until you publish from the Designer or
  add `--publish`, which publishes only the pages just changed, one at a time, a minute
  apart (Webflow's limit). A whole-site publish is never issued, because it would also
  push whatever else is staged in the Designer.
- Every applied, failed and published item is appended to
  `.rankwright/publish/webflow-log.jsonl`.

### JSON-LD on Webflow: the trade-off

Webflow's API registers custom code as a script with a 2,000-character limit. So
`"jsonld"` attaches a small script that inserts the structured data when the page runs.

- Google runs JavaScript and reads it.
- A crawler that does not run JavaScript, which includes several AI crawlers, will not
  see it.
- Markup over the limit is not sent; the preview says so.

If either matters, paste the generated `.html` snippet into the page's custom code
(Page settings, Custom code, inside `<head>`) in the Designer instead. That is static
markup and has neither limitation.

`llms.txt` is not handled here. Upload it through Webflow's own site settings.

## `rankwright publish social`

```bash
rankwright publish social draft https://example.com/blog/launch
#    edit the files in .rankwright/social/queue/, set "approved": true
rankwright publish social send --config rankwright.config.json            # preview
rankwright publish social send --config rankwright.config.json --apply    # post
```

```json
{
  "smm": {
    "platforms": ["facebook", "instagram", "linkedin"],
    "facebookPageId": "1234567890",
    "instagramUserId": "17840000000000000",
    "linkedinAuthor": "urn:li:organization:1234567",
    "requireHumanApproval": true
  },
  "secrets": {
    "facebook": "env:FB_PAGE_TOKEN",
    "instagram": "env:IG_TOKEN",
    "linkedin": "env:LINKEDIN_TOKEN"
  }
}
```

A draft is the page's own title, its answer block or meta description, and its link. It
is a starting point for an editor. Nothing here writes a hook, picks hashtags or adds a
claim the page does not make.

Behaviour worth knowing:

- Instagram posts need an image, so a page without `og:image` gets no Instagram draft.
  Instagram captions carry no link, because links there are not clickable.
- `x` and `youtube` are accepted in `smm.platforms` but have no adapter; they are
  skipped with that reason.
- An existing draft file is never overwritten.
- A published draft is marked with its post id in its own file and is never sent twice.
- A failed post is marked `failed` with the reason and is not retried until you set its
  `status` back to `draft`.
- A missing token or account id reports `blocked` and leaves the draft untouched.
- Text over a platform's limit is `invalid`. It is never truncated for you.
- Setting `smm.requireHumanApproval` to `false` skips the per-draft approval. `--apply`
  is still required.

Tokens: a Facebook Page access token with `pages_manage_posts`; an Instagram token with
`instagram_content_publish` for a professional account; a LinkedIn token with
`w_organization_social` (or `w_member_social` for a personal `urn:li:person:` author).
Getting these means creating an app with Meta and LinkedIn, and LinkedIn's organisation
posting scope needs their approval. Tokens expire; LinkedIn's after about 60 days.

API versions default to Graph `v26.0` and LinkedIn `202610`. Both providers retire
versions on a schedule, so override with `smm.graphVersion` and `smm.linkedinVersion`
when they do.

## Secrets

Every credential is an `env:` pointer in config, never a literal. The schema rejects a
literal at validation time. Tokens are sent in headers or form bodies, are kept out of
error messages, and are never written to plan files, logs or drafts. SerpAPI is the one
exception to "not in a URL": its API takes the key as a query parameter, so Rankwright
strips the request URL from any error it reports.
