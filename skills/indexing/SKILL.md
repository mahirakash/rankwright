---
name: indexing
description: Use when asked to submit URLs for indexing, ping search engines after a deploy or content change, set up IndexNow, or get new and updated pages crawled faster by Bing and other IndexNow engines.
---

# Indexing

Submit new and changed URLs through IndexNow, tracked by content hash so unchanged
pages are not resubmitted.

## When to use

After a deploy or content update, or to set up IndexNow for a site.

## When NOT to use

- Getting pages into **Google**. Google does not use IndexNow. Say so, and point the
  user to submitting the sitemap in Search Console.
- Checking whether pages are indexable in the first place — that is `audit`.
- Any promise of ranking or indexing. A successful submission means "received".

## How to run it

```bash
rankwright index https://example.com --keygen public    # once: creates <key>.txt
rankwright index https://example.com                    # preview, sends nothing
rankwright index https://example.com --key <key> --submit
```

## Rules

1. **Preview first, every time.** Run without `--submit`, show the user the new and
   changed counts, and submit only after they agree.
2. **The key file must be live first.** `--keygen` writes it locally; the user has to
   deploy it to the site root. Submission is refused until it is reachable.
3. **Do not use `--all` routinely.** Resubmitting unchanged URLs is how a host gets
   rate-limited. Use it once after a migration at most.
4. **Keep the state file.** `.rankwright/indexing/state.json` must persist between runs,
   or everything looks new each time. `.rankwright/` is git-ignored: cache it in CI or
   pass `--state` with a tracked path.
5. Report unreachable URLs to the user. They are in the sitemap and returning errors.
