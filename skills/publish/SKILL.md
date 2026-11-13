---
name: publish
description: Use when asked to push SEO titles, meta descriptions or JSON-LD to a Webflow site, apply audit fixes to Webflow, or draft and publish Facebook, Instagram or LinkedIn posts from a published page.
---

# Publish

Apply approved changes to a Webflow site, and post approved drafts to Facebook,
Instagram and LinkedIn. This is the only skill that writes to the public internet.

## When to use

"Update the meta titles on Webflow", "put the schema on the site", "post this page to
LinkedIn".

## When NOT to use

- Sites not on Webflow. For a code repository, `rankwright fix` previews diffs.
- Finding what to change — that is `audit`.
- X or YouTube. There is no adapter.
- Webflow CMS collection templates. They are skipped by design.

## How to run it

```bash
rankwright publish webflow plan  --config rankwright.config.json
rankwright publish webflow apply --config rankwright.config.json          # preview
rankwright publish webflow apply --config rankwright.config.json --apply

rankwright publish social draft https://example.com/blog/post
rankwright publish social send --config rankwright.config.json            # preview
rankwright publish social send --config rankwright.config.json --apply
```

## Rules

These are not optional.

1. **Never set `"approved": true` yourself.** You may fill in `proposed` values or edit
   draft text when asked. Approval belongs to the user: show them the exact text and
   let them approve it, or set the flag only on their explicit instruction for those
   specific items.
2. **Always run the preview and show it** before any `--apply`.
3. **Ask before `--apply`, and again before `--publish`.** `--apply` saves to Webflow;
   `--publish` makes it live. A social `--apply` is public the moment it runs and
   cannot be previewed after the fact.
4. **A `stale` entry means someone edited the page.** Rebuild the plan. Do not edit
   `current` to force it through.
5. **Do not reset a `failed` post to `draft`** without telling the user why it failed.
6. **Proposed titles and descriptions must be true of the page.** Read the page first.
   Stay within 60 and 160 characters.
7. **JSON-LD through the Webflow API is inserted by script**, capped at 2,000
   characters, and invisible to crawlers that do not run JavaScript. Tell the user, and
   offer the paste-in-Designer route instead.
8. Tokens are `env:` pointers. Never ask the user to paste a token into the chat or a
   config file.
