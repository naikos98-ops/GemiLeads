# Post-Implementation SEO Audit — gemileads.gr

**Date:** 2026-08-22 · **Audited against:** `FINAL-SEO-IMPLEMENTATION-PLAN.md`
**Live production commit:** `340c26b` · **Local HEAD:** `d32b197` · **Unpushed: 2 commits**

---

## ⚠ Overriding Finding

**Nothing has been deployed. Production is byte-for-byte unchanged.**

```
origin/main : 340c26b
local  HEAD : d32b197
unpushed    : 2 commits (446a28f, d32b197)
```

Live verification of the homepage (HTTP 200, 24,966 bytes — identical to the pre-implementation
audit):

```
/robots.txt                 404          (expected 200)
/sitemap.xml                404          (expected 200)
cdn.tailwindcss.com present   1          (expected 0)
links to /pricing/            0          (expected ≥3)
canonical tag                 0          (expected 1)
JSON-LD blocks                0          (expected 1)
og:image                      0          (expected 1)
meta description              0          (expected 1)
favicon                 132,331 bytes    (expected ~22,000)
```

The instruction was explicit: **do not mark something FIXED based only on source code changes.**
Applying that rule honestly, **every P0 and P1 item is NOT FIXED in production**, despite all of them
being correctly implemented and verified locally.

To keep this useful rather than merely pedantic, the table below reports **two** columns: the live
production state (which governs the status) and the verified local state (which governs deployment
confidence).

---

## BEFORE vs AFTER

| # | Issue | Previous state | Current state (LIVE) | Verification method | Status | Remaining risk |
|---|---|---|---|---|---|---|
| **P0-1** | Tailwind Play CDN: 120 KB render-blocking JIT compiler | Present on all pages | **Still present** — `grep -c cdn.tailwindcss.com` = **1**. Local build verified: 0, compiled `app.css` served instead | Live `curl` + local render under production settings | **NOT FIXED** | Deploy must run `npm ci && npm run build:css` or the site ships **unstyled**. Build chain simulated end-to-end and **succeeds**; `package.json`, `package-lock.json`, `tailwind.config.js`, `static/src/input.css` all tracked; generated `app.css` correctly gitignored. Risk materially reduced but non-zero until observed in the real build log. |
| **P0-2** | Pricing page advertises two capabilities the product lacks | "Daily & Weekly Digest"; "08:00 - 00:00" | **Both still live** — `Weekly Digest` = 1, `08:00 - 00:00` = 1, `08:00 - 23:00` = 0. Local: 0 / 0 / 1 | Live `curl` on `/pricing/`; local render; cross-checked against `services.py:363` and `tasks.py:51` | **NOT FIXED** | **Highest-severity item.** A €49/month tier is currently sold on a feature the code raises `ValueError` on. Consumer-protection and chargeback exposure, live right now. |
| **P0-3** | `/pricing/` orphaned — zero inbound internal links | 0 links from any public page | **Still 0 live.** Local: **3** (desktop nav, mobile menu, footer) | Crawled live public pages; local render count | **NOT FIXED** | None in the fix itself. Verified no anchors were removed — the 2 "removed" diff lines are the same lines re-emitted with the pricing link inserted. |
| **P1-1** | Canonical echoed query strings; params self-canonicalised | No canonical at all live | **Still absent live.** Local verified: `/?utm_source=test&page=2` → `https://gemileads.gr/` | Live `grep`; local render of 3 param variants | **NOT FIXED** | Low. `{% block canonical %}` retained so a future paginated view can override deliberately. |
| **P1-2** | Uncached `COUNT(*)` over 17,789 rows on every request | ~0.83 s TTFB, 3 aggregates/request | **Unchanged live** (cannot isolate remotely). Local measured: **3 queries cold → 0 warm**, values identical (`today_count=15`, `global=15`) | `CaptureQueriesContext` test, cold vs warm | **NOT FIXED** *(live)* / verified locally | **Falsifiable claim:** if live TTFB stays ≥0.8 s after deploy, the cause is the Render instance tier, not these queries — a different fix. Do not assume success. |
| **P1-3** | Zero structured data in any format | 0 JSON-LD / Microdata / RDFa | **Still 0 live.** Local: 1 block on `/`, 2 on `/pricing/`; all parse; `@id` graph connected | `json.loads` on rendered output + explicit `@id` resolution assertion | **NOT FIXED** | Low. The silent-failure mode (each block validates in isolation while `@id` refs dangle) is covered by a dedicated test. No fabricated properties — `legalName`, `address`, `vatID`, `sameAs`, `aggregateRating`, `review` all absent by assertion. |
| **P1-4** | `og:image` root-relative; scrapers cannot resolve | No `og:image` live | **Still absent live.** Local: `https://gemileads.gr/static/images/logo.png` | Live `grep`; local render | **NOT FIXED** | Low. Note `logo.png` is still 382 KB live; `446a28f` reduces it to 105 KB in the same deploy. |

### Status summary

| Status | Count |
|---|---|
| FIXED | **0** |
| PARTIALLY FIXED | 0 |
| **NOT FIXED** | **7** |
| UNABLE TO VERIFY | 0 |

Nothing was marked UNABLE TO VERIFY: every item was decisively verifiable against live production.

---

## Remaining P0 Issues

All three, unchanged in production:

1. **P0-1** — Tailwind CDN still shipping 120 KB of render-blocking JavaScript to every visitor.
2. **P0-2** — Pricing page still sells a deleted feature. **This is the one with non-SEO legal exposure.**
3. **P0-3** — `/pricing/` still unreachable by crawlers and logged-out visitors.

**Single remediation for all three: `git push origin main` and deploy.**

## Remaining P1 Issues

All four (P1-1 … P1-4), unchanged in production. Same single remediation.

---

## Newly Introduced Regressions

**None found.** Checks performed:

| Check | Result |
|---|---|
| Full test suite | **130 passed** (was 113 pre-implementation; +17 new) |
| `manage.py check` | No issues |
| `makemigrations --check` | No changes detected |
| `check --deploy` (production env) | **No issues** |
| pyflakes (all app modules) | Clean |
| Route integrity (13 routes) | All correct — 200s, 302s on gated pages, genuine 404 |
| Business logic diff (`services.py`, `tasks.py`, `models.py`, `apps.py`, `billing.py`) | **Zero changes** |
| New dependencies | **None** |
| Visible markup changes | 3 added anchors only; **0 anchors removed** |
| CSS build from scratch | Reproduces at 40,679 bytes |
| Full Render build chain (`npm ci` → `build:css` → `collectstatic`) | **Succeeds** |

One behavioural change worth naming explicitly, as it is intentional rather than a regression:
`home()` aggregates are now cached for 600 s and the global count for 3600 s. The homepage
"today_count" figure may therefore lag a fresh import by up to 10 minutes. This is acceptable for a
marketing counter and does not affect the digest pipeline, which reads the database directly.

---

## Recommended P2 Items

Unchanged from the plan, with one promotion:

| # | Item | Note |
|---|---|---|
| **P2-4** | **Correct the ΚΑΔ figure** — homepage claims "9.744", catalogue holds 9,651, DB holds 10,463. **Still live.** | **Recommend promoting to the P0 deploy.** It is the same class of factual inaccuracy as P0-2, it is a one-line change, and it is the site's single most quotable statistic — an AI engine citing it would propagate a wrong figure attributed to the brand. |
| P2-1 | WhiteNoise `CompressedManifestStaticFilesStorage` + 1-year immutable caching | Live assets still `max-age=60` with `cf-cache-status: DYNAMIC`. **Risk:** manifest storage hard-fails the build on any missing `{% static %}` reference — run `collectstatic` locally first (it currently succeeds). |
| P2-2 | Three self-contained 134–167 word answer blocks on the homepage | Largest current passage is 39 words; zero definitional sentences. The main GEO lever. |
| P2-3 | `aria-label` on the icon-only menu button | Scoped to one control; blanket SVG `aria-hidden` was cut as cosmetic. |

---

## Is the site technically ready for production, from an SEO perspective?

**The codebase is ready. The live site is not — because the codebase has not been deployed.**

Distinguishing the two:

**Ready — verified:**
- All 7 P0/P1 fixes implemented correctly and confirmed against rendered output under production settings
- 130 tests pass on a clean database; lint, system check and `check --deploy` all clean
- No regressions; business logic, routes and visual design untouched; no new dependencies
- Build chain reproduces end-to-end, including a clean `npm ci`
- Structured data validates and the entity graph resolves, with no fabricated properties

**Not ready — live production:**
- Zero of the fixes are serving. `robots.txt` and `sitemap.xml` 404; no canonical, no metadata, no
  structured data; 120 KB render-blocking compiler; orphaned pricing page; **two false commercial
  claims on a paid tier**

**Verdict: NOT production-ready as of this audit**, on the single ground that the work is undeployed.
One `git push` plus a successful Render build converts every NOT FIXED row above to FIXED. No further
code work is required for P0/P1.

### Post-deploy verification (re-run before claiming FIXED)

```bash
# 1. In the Render build log, confirm this line appears:
#      "Building the Tailwind stylesheet..."
#    If absent, roll back — the site will render unstyled.

curl -s -o /dev/null -w "%{http_code}\n" https://gemileads.gr/robots.txt    # 200
curl -s -o /dev/null -w "%{http_code}\n" https://gemileads.gr/sitemap.xml   # 200
curl -s https://gemileads.gr | grep -c "cdn.tailwindcss.com"                # 0
curl -s https://gemileads.gr | grep -c 'href="/pricing/"'                   # >= 3
curl -s https://gemileads.gr | grep -c "application/ld+json"                # 1
curl -s https://gemileads.gr/pricing/ | grep -c "Weekly Digest"             # 0
curl -s "https://gemileads.gr/?utm_source=x" | grep -o 'canonical[^>]*'     # no query string
curl -s -o /dev/null -w "%{size_download}\n" https://gemileads.gr/static/images/favicon.png  # ~22000
```

Then submit the sitemap in Search Console.

---

## Methodology & Limits

- Live production verified by direct HTTP on 2026-08-22; local implementation verified by rendering
  through Django under production settings (`DEBUG=0`, proxy SSL header), not by reading source.
- **No Core Web Vitals field data** (no CrUX/GSC credentials). **LCP, INP and CLS are deliberately not
  quantified.** Performance claims rest on measured TTFB, transfer weight and render-blocking analysis.
- **No keyword, traffic, ranking, backlink or competitor data** was available and none was invented.
- P1-2's live effect cannot be isolated remotely; its status reflects deployment, and its local effect
  is measured rather than asserted.
