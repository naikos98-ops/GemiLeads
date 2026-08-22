# Technical SEO — Findings

Verified live 2026-08-22 against https://gemileads.gr

## Verified PASS
| Check | Result |
|---|---|
| HTTPS | Pass |
| HSTS preload | `max-age=31536000; includeSubDomains; preload` |
| www -> apex | 301, single hop |
| http -> https | 301, single hop |
| Redirect chains | None (max 1 hop) |
| Trailing slash | Consistent |
| 404 handling | Genuine 404, no soft-404 |
| Gated pages | 302 to login (correct, not 200) |
| Server rendering | Full SSR, no JS dependency |
| HTML compression | 25 KB -> 6.1 KB |
| Security headers | nosniff, DENY, referrer-policy, COOP |
| Mixed content | None |

## Verified FAIL
| ID | Issue | Severity | Status |
|---|---|---|---|
| T1 | robots.txt 404 | P1 | Fixed in 446a28f (undeployed) |
| T2 | sitemap.xml 404 | P1 | Fixed in 446a28f (undeployed) |
| T3 | /pricing/ orphan, 0 internal links | P0 | NOT fixed |
| T4 | Static assets max-age=60 | P2 | NOT fixed |
| T5 | No llms.txt | P3 | Optional |

## Crawl map
Indexable surface = 4 pages: `/`, `/pricing/`, `/signup/`, `/login/`
Crawl depth: all at depth 0-1. Orphan: `/pricing/`.
Internal links from homepage: 7 total, 3 unique (/signup/ x4, /login/ x2, / x1).
