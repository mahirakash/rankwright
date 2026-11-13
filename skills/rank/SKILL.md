---
name: rank
description: Use when asked to track keyword rankings, check where a site ranks, report ranking changes over time, compare positions against competitors, or pull Search Console query performance.
---

# Rank

Record keyword positions and report movement since the previous run.

## When to use

"Where do we rank for X", "how did rankings change", a monthly client report, or a
competitor position check.

## When NOT to use

- Explaining *why* a page ranks poorly — run `audit` on that page.
- Testing whether AI assistants cite the brand. Nothing in Rankwright measures that.
- Keyword research or search volume. This reports positions only.

## How to run it

```bash
rankwright rank --config rankwright.config.json                      # gsc by default
rankwright rank --config rankwright.config.json --provider serpapi
```

Needs `rank` in the config and credentials: the `gsc` secret or a service-account key
for Search Console, the `serpapi` secret for SerpAPI.

## Rules

1. **Say which provider the numbers came from.** `gsc` is an average over a window with
   the latest days missing; `serpapi` is a single live reading. Never put the two in one
   table or compute a change between them.
2. **`—` is "not seen", not "position 100+".** For `gsc` it means no impressions in the
   window; for `serpapi`, not within `depth` results.
3. **Warn about cost before a `serpapi` run.** It is one or more billed searches per
   keyword. State the count and get agreement first.
4. **One run is a reading, not a trend.** The first run has nothing to compare with.
   Do not call a one-run difference a result of any change the user made.
5. Use `--no-save` for an ad-hoc look that should not enter the history.
