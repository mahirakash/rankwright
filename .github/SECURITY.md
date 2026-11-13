# Security Policy

## Supported versions

| Version | Supported |
|---|---|
| 0.5.x | Yes |

## Reporting a vulnerability

Please do **not** open a public issue for a security problem.

Open a [private vulnerability report](https://github.com/mahirakash/rankwright/security/advisories/new) with a description, reproduction steps, and the impact
you have assessed. You can expect an acknowledgement within 72 hours and an assessment
within seven days.

## Scope

As of 0.5.0 Rankwright reads credentials and makes network writes: `index --submit`
posts to IndexNow, `rank` calls Search Console or SerpAPI, and `publish` writes to
Webflow, Facebook, Instagram and LinkedIn. The areas most worth your attention:

- **Credential leakage** — a secret appearing in a report, plan file, draft, log, error
  message or committed file. Config holds `env:` pointers only; a literal secret is
  rejected at schema validation.
- **A write without consent** — any path by which `index`, `publish webflow` or
  `publish social` sends, saves or posts something without `--submit` / `--apply`, or
  publishes an item not marked `approved: true` while approval is required.
- **SSRF via configured URLs** — the crawler follows URLs from config and sitemaps.
- **Script injection through generated JSON-LD** — page text escaping the generated
  `<script>` block or the Webflow inline script.

## Out of scope

Rate limits imposed by third-party APIs, and findings that require an attacker to already
control the machine running Rankwright.
