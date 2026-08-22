# Technical SEO Deep Dive — gemileads.gr

**Date:** 2026-08-22 · **Live commit:** `340c26b` · **Local HEAD:** `446a28f` (**1 commit unpushed**)
**Technical Score: 52/100**

All findings below were verified by direct measurement against the live site or by reading the
working tree. Nothing was modified.

---

## Category Breakdown

| Category | Status | Score | Basis |
|---|---|---|---|
| Crawlability | **fail** | 30 | No robots.txt, no sitemap, orphan page |
| Indexability | **warn** | 45 | No canonical; param URLs duplicate |
| Security | **pass** | 85 | HSTS preload, full header set, no CSP |
| URL Structure | **pass** | 90 | Clean, consistent, no duplicate routes |
| Mobile | **pass** | 80 | Perfect parity; nav height bug pending |
| Core Web Vitals | **warn** | 35 | 0.83 s TTFB, 120 KB blocking JS |
| Structured Data | **fail** | 0 | Zero markup |
| JS Rendering | **pass** | 95 | Genuine SSR, no JS dependency |
| IndexNow | **warn** | 0 | Not implemented (optional) |

---

# P0 — CRITICAL

## P0-1 · Tailwind Play CDN: 120 KB render-blocking JIT compiler
**File:** `templates/base.html:10` (live) · **Status:** fixed in unpushed `446a28f`
**Confidence:** Verified

```
live head: <script src="https://cdn.tailwindcss.com"></script>   120.4 KB, blocking
```

Not a stylesheet — Tailwind's **compiler**. It downloads, parses, executes, scans the DOM and
generates CSS on the visitor's device before first paint. Tailwind documents it as development-only.
This is the dominant render-blocking cost and the most likely LCP driver.

**Fix:** deploy `446a28f` (compiled stylesheet, 7.7 KB gzipped).
**Deploy risk:** the Render build must run `npm ci && npm run build:css`. If that step fails the site
ships **entirely unstyled**. Confirm `Building the Tailwind stylesheet...` appears in the build log.

## P0-2 · `/pricing/` is an orphan — zero inbound internal links
**Files:** `templates/base.html` (nav inside `{% if user.is_authenticated %}`), `templates/home.html`
**Confidence:** Verified

Crawled all 5 public pages and resolved every internal link:

```
200  /                      (linked 5x)
200  /login/                (linked 11x)
200  /password_reset/       (linked 1x)
200  /resend-verification/  (linked 1x)
200  /signup/               (linked 13x)
```

`/pricing/` **does not appear**. Every `{% url 'pricing' %}` in the repo sits inside a login-gated
template (`dashboard.html`, `settings.html`, `radars/list.html`, `companies/detail.html`).

The site's highest commercial-intent page has no crawlable path and receives no internal PageRank.
The sitemap in `446a28f` aids discovery but **does not** substitute for internal links.

**Fix:** add `/pricing/` to the public nav and footer in `templates/base.html`, plus a homepage CTA.

---

# P1 — HIGH IMPACT

## P1-1 · No robots.txt / no sitemap.xml
**File:** `config/urls.py` · **Status:** fixed in unpushed `446a28f` · **Confidence:** Verified

```
GET /robots.txt   -> 404
GET /sitemap.xml  -> 404
```

The site has a large authenticated area (`/dashboard/`, `/leads/`, `/radars/`, `/superadmin/`,
`/api/`) that crawlers repeatedly request and receive 302s from — wasted crawl budget on a site
whose entire indexable surface is 4 pages.

**Fix:** deploy `446a28f` (`gemiapp/seo.py`).

## P1-2 · Query parameters create unlimited duplicate URLs with no canonical
**File:** `templates/base.html` (no canonical live) · **Confidence:** Verified

```
/                     md5 ee97e734044284048042fa1639673220   200
/?utm_source=test     md5 ee97e734044284048042fa1639673220   200   <- byte-identical
/?page=2              200
/?foo=bar             200
```

Byte-identical content on unlimited URLs, no canonical tag, no robots.txt to constrain crawling.
Any shared campaign link creates an indexable duplicate.

### P1-2a · The pending fix does not fully solve this
**File:** `templates/base.html:18` and `:26` (in `446a28f`) · **Confidence:** Verified

```django
<link rel="canonical" href="{{ request.build_absolute_uri|slice:':512' }}">
<meta property="og:url" content="{{ request.build_absolute_uri|slice:':512' }}">
```

`build_absolute_uri()` **includes the query string**. Verified against the real Django request object:

```
{}                        -> http://gemileads.gr/
{'utm_source': 'test'}    -> http://gemileads.gr/?utm_source=test
{'page': '2'}             -> http://gemileads.gr/?page=2
```

So `/?utm_source=test` **self-canonicalises** rather than consolidating to `/`. The canonical is
present but does not do the one job it is needed for here.

**Fix:** emit a parameter-free canonical:

```django
<link rel="canonical" href="{% block canonical %}{{ request.scheme }}://{{ request.get_host }}{{ request.path }}{% endblock %}">
```

Keep the `{% block %}` so paginated or filtered views can override deliberately.

**Scheme note (verified, not an issue):** `SECURE_PROXY_SSL_HEADER` is set at `config/settings.py:36`
under `if not DEBUG`, so production correctly emits `https://`. The `http://` seen in local testing
is a DEBUG-only artefact.

## P1-3 · TTFB 0.83 s — isolated to Django view + DB, not cold start
**File:** `gemiapp/context_processors.py:6`, `gemiapp/views.py` (`home`) · **Confidence:** Verified

Six consecutive runs, no cold-start outlier (0.844 / 0.822 / 0.817 / 0.868 / 0.837 / 0.870 s) — this
is **steady-state latency**, not spin-up.

Isolating the server cost by comparing a static file (no Django, no DB) against HTML:

```
static /static/js/app.js   ttfb 0.271 / 0.266 / 0.246 s
HTML   /                   ttfb 0.818 / 0.820 / 0.853 s
                           ------------------------------
                           ~0.57 s = Django view + DB + template render
```

**Root causes in code:**
1. `gemiapp/context_processors.py` — uncached `Company.objects.count()` runs on **every request
   site-wide** (17,789 rows).
2. `gemiapp/views.py::home` — 3 further uncached aggregates per homepage load
   (`today_count`, `latest_companies`, `recent_count`).

TTFB gates LCP; ~0.83 s consumes most of a good LCP budget before a byte renders.

**Fix:**
```python
# gemiapp/context_processors.py
from django.core.cache import cache

def global_stats(request):
    return {"global_company_count": cache.get_or_set(
        "global_company_count", lambda: Company.objects.count(), 3600)}
```
Apply the same caching to the three `home()` aggregates. Note `LocMemCache` is per-process; with
multiple gunicorn workers each caches separately — acceptable for a marketing counter.

**Confidence:** the ~0.57 s gap is **verified**; the exact share attributable to the count query
specifically is **probable** — isolating it needs server-side timing in production.

## P1-4 · No structured data of any kind
**File:** `templates/base.html` · **Confidence:** Verified

`application/ld+json` occurrences across all public pages: **0**. No entity definition for Google's
Knowledge Graph or AI answer engines.

**Fix:** `Organization` + `WebSite` site-wide, `SoftwareApplication` with `offers` on `/pricing/`.
Draft markup in `findings/schema.md`. **Blocked by** missing contact/legal data.

**Do NOT add:** `FAQPage` (Google retired FAQ rich results for all sites 2026-05-07) or `HowTo`
(deprecated 2023).

---

# P2 — MEDIUM IMPACT

## P2-1 · Static assets `max-age=60` — Cloudflare is not caching them
**File:** `config/settings.py` (no `STORAGES` block) · **Confidence:** Verified

```
/static/images/favicon.png   Cache-Control: max-age=60, public   cf-cache-status: DYNAMIC
/static/js/app.js            Cache-Control: max-age=60, public   cf-cache-status: DYNAMIC
```

Two compounding problems:
1. **`cf-cache-status: DYNAMIC`** — Cloudflare is serving assets straight from origin, not edge-caching
   them, because a 60-second TTL is not worth caching. You are paying origin latency for every static
   asset on every visit.
2. Filenames are unhashed (`app.js`, not `app.<hash>.js`), so a long TTL is not currently safe.

WhiteNoise runs with defaults — no manifest hashing, no pre-compression. (Brotli *is* being applied,
but by Cloudflare at the edge, not by WhiteNoise.)

**Fix:**
```python
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}
WHITENOISE_MAX_AGE = 31536000
```
Manifest hashing makes the 1-year immutable TTL safe and lets Cloudflare edge-cache properly.

**How it fails:** `CompressedManifestStaticFilesStorage` **hard-fails the build** on any `{% static %}`
reference to a missing file. Run `collectstatic` locally before deploying.

## P2-2 · favicon.png is 129 KB — largest asset on the critical path
**File:** `static/images/favicon.png` · **Status:** fixed in unpushed `446a28f` · **Confidence:** Verified

Live: 132,331 bytes at 353×353, rendered at 48×48 (emails) and ~32 px (tab). Requested on every page.
Larger than the gzipped Tailwind payload. `446a28f` reduces it to 22 KB.

## P2-3 · No Content-Security-Policy / Permissions-Policy
**File:** `config/settings.py` · **Confidence:** Verified

Present: `strict-transport-security` (with **preload**), `x-frame-options: DENY`,
`x-content-type-options: nosniff`, `referrer-policy`, `cross-origin-opener-policy`.
Absent: `content-security-policy`, `permissions-policy`.

**Weighting note:** HTTPS is a confirmed but lightweight ranking signal affecting <1% of queries, and
the standalone Page Experience report was removed from Search Console. CSP is genuine security
hardening but should **not** be prioritised above P0/P1 items on SEO grounds.

## P2-4 · Render-blocking Google Fonts, 5 weights
**File:** `templates/base.html` · **Confidence:** Verified

Blocking third-party stylesheet requesting weights 400,500,600,700,800. `preconnect` is correctly
present and `&display=swap` correctly prevents invisible text — both already right. Remaining cost is
one third-party origin on the critical path.

**Fix:** self-host Inter as woff2 alongside `app.css`, or trim to the 2–3 weights actually used.

---

# P3 — OPTIONAL

## P3-1 · No IndexNow
`GET /indexnow.txt` → 404; no implementation in repo. Affects Bing/Yandex/Naver only, not Google.
Low value for a 4-page Greek-market site. Revisit if a content cluster ships.

## P3-2 · No `llms.txt`
Emerging convention, **not** consumed by Google Search. Optional.

## P3-3 · Nav height bug `h-18`
**File:** `templates/base.html` · **Status:** fixed in unpushed `446a28f` · **Confidence:** Verified

`h-18` is not in Tailwind's default spacing scale and was never generated
(`grep -c "\.h-18" static/css/app.css` → `0`). `<main>` compensated with a hardcoded `pt-[72px]`.
When CTAs wrap on narrow viewports the nav exceeds 72 px and overlaps content — a plausible CLS
contributor.

---

# VERIFIED PASSING — no action required

| Check | Evidence |
|---|---|
| **JS rendering / SSR** | 321 words + `<h1>` present in raw HTML under `User-Agent: Googlebot`. No CSR dependency. |
| **Googlebot 2 MB fetch cap** | Homepage 23.1 KB. Far within limit. 1 base64 blob, 3 script tags. |
| **Mobile/desktop parity** | Googlebot Smartphone vs Desktop: **identical** — 321 words, 14 headings both. |
| **Trailing slash** | `APPEND_SLASH` works: `/pricing` → 301 → `/pricing/`. Consistent. |
| **Case / index duplicates** | `/PRICING/`, `/pricing//`, `/index.html`, `/home`, `//` all 404. No duplicate routes. |
| **Host canonicalisation** | `www` → apex and `http` → `https`, both single-hop 301. No chains. |
| **HTTP status codes** | Gated pages 302 to login (not 200). Unknown URLs genuine 404, no soft-404s. |
| **Broken links** | 5 unique internal targets crawled, **all 200**. Zero broken. |
| **Mixed content** | None on HTTPS. |
| **Back-button hijacking** | `pushState`/`replaceState` count: **0**. No spam-policy exposure. |
| **AJAX content negotiation** | `/dashboard/` serves JSON on `?page=2`/`?format=json`, but is login-gated (302) so crawlers never reach the dual-response route. Not an SEO risk. |
| **Intrusive interstitials** | None. |
| **HSTS preload** | `max-age=31536000; includeSubDomains; preload`. |

---

# Prioritised Technical Implementation Checklist

### Step 0 — Deploy what exists (Day 1)
- [ ] `git push origin main` → deploy `446a28f`
- [ ] Confirm build log shows `Building the Tailwind stylesheet...` — **roll back if absent**
- [ ] Verify: `curl -s https://gemileads.gr | grep -c cdn.tailwindcss.com` → `0`
- [ ] Verify: `/robots.txt` → 200, `/sitemap.xml` → 200

*Closes P0-1, P1-1, P2-2, P3-3.*

### Step 1 — Not yet built (Week 1)
- [ ] **P0-2** Link `/pricing/` from public nav + footer (`templates/base.html`) and homepage CTA
      Verify: `curl -s https://gemileads.gr | grep -c 'href="/pricing/"'` → `≥1`
- [ ] **P1-2a** Change canonical + `og:url` to `{{ request.scheme }}://{{ request.get_host }}{{ request.path }}`
      Verify: `/?utm_source=x` canonical points to `/`

### Step 2 — Performance (Weeks 2–3)
- [ ] **P1-3** Cache `global_stats` count (3600 s) + the three `home()` aggregates
      Verify: HTML TTFB approaches the 0.26 s static baseline
      *If TTFB stays ≥0.8 s, the cause is Render instance tier, not the query — different fix*
- [ ] **P2-1** `STORAGES` + `CompressedManifestStaticFilesStorage` + `WHITENOISE_MAX_AGE`
      Run `collectstatic` locally first — manifest storage hard-fails on missing refs
      Verify: `cf-cache-status: HIT` on a second static request

### Step 3 — Structured data (Weeks 2–3)
- [ ] **P1-4** `Organization` + `WebSite` site-wide; `SoftwareApplication` on `/pricing/`
      *Blocked by* adding real contact/legal data first
      Validate in Rich Results Test; confirm `@id` references resolve between blocks

### Step 4 — Backlog
- [ ] **P2-4** Self-host Inter / trim weights
- [ ] **P2-3** CSP + Permissions-Policy (security value; low SEO weight)
- [ ] **P3-1** IndexNow, only if a content cluster ships

---

## Methodology & Limits

- Live checks by direct HTTP on 2026-08-22; source attributions read from the working tree at `446a28f`.
- **No Core Web Vitals field data.** No CrUX/GSC credentials configured, and a site this size is
  unlikely to have a CrUX record. **LCP, INP and CLS are deliberately not quantified.** Performance
  findings rest on measured TTFB, transfer weight and render-blocking analysis only.
- **No traffic, ranking, keyword or backlink data** was available and none was invented.
- Crawl scope: 5 public pages + 4 gated paths = the complete indexable surface, not a sample.
