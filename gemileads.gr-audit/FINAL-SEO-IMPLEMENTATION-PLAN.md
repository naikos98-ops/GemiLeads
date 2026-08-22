# FINAL SEO IMPLEMENTATION PLAN — gemileads.gr

**Date:** 2026-08-22 · **Live commit:** `340c26b` · **Local HEAD:** `446a28f` (**unpushed**)
**Supersedes** all prior audit reports in this directory.

This is a critical re-review of the 29 recommendations produced across five earlier audits.
**17 were cut. 12 survive.** Every surviving task was re-verified live on 2026-08-22.

---

## What Was Cut, and Why

Removing these matters as much as the plan itself — each was a plausible-sounding recommendation that
does not survive an evidence test.

| Cut recommendation | Reason |
|---|---|
| Fix H1→H4 heading order | Google has repeatedly stated heading **order** is not a ranking factor. Real but purely semantic/accessibility. Not a ranking, CTR or indexing lever. |
| Expand `/signup/` and `/login/` content | **Would actively harm conversion.** Auth pages are correctly thin at 83/67 words. Applying a "thin content" threshold here was a mechanical error. |
| IndexNow implementation | Bing/Yandex only. 4 URLs, no content velocity. Negligible. |
| `llms.txt` / RSL | Google explicitly states Search **ignores** `llms.txt` and it "won't harm (nor help)". Recommending it implies a benefit the evidence denies. |
| CSP + Permissions-Policy headers | Genuine security value, **negligible SEO value**. HTTPS affects <1% of queries; the Page Experience report was removed from Search Console. |
| `aria-hidden` on 36 decorative SVGs | Accessibility is not a ranking factor. Cosmetic at this scale. *(One exception retained — P2-3.)* |
| `alt="Gemi Leads"` in email templates | Emails are not crawled. Zero SEO impact. |
| `og:image:width` / `:height` / `:alt` | Marginal; affects only first-share render. |
| Produce a 1200×630 social share image | Design task, speculative value. |
| Add `WebPage` schema | Adds almost nothing over `Organization` + `SoftwareApplication`. **Unnecessary schema.** |
| Add `BreadcrumbList`, `Service`, `Product`, `FAQPage`, `LocalBusiness` | All verified non-applicable. `FAQPage` additionally has **no SERP feature** since 2026-05-07. |
| `oxipng`/`pngquant` extra compression | ~10–30% on an asset already reduced to 21 KB. Negligible. |
| Split sitemap / sitemap index | 4 URLs against a 50,000 limit. Pointless. |
| Remove `priority`/`changefreq` from sitemap | Ignored by Google. **Zero** impact — pure cosmetics. |
| Segment landing pages (`/gia-logistes/` etc.) | **Speculative.** No keyword or demand data exists. Cannot justify build cost. |
| Full informational content cluster | Same: gated on validation that has not happened. Retained only as the P3 *validation* step, not as a build instruction. |
| Off-site brand presence (Reddit/YouTube/Wikipedia) | Not a site change; unfalsifiable as an SEO task here. |

**Also merged:** the orphan-`/pricing/` finding appeared in **7** reports, the Tailwind CDN in 3,
favicon in 4, `max-age=60` in 4, canonical in 4. Each is now **one** task.

---

# P0 — MUST FIX IMMEDIATELY

## P0-1 · Deploy commit `446a28f`

**Problem** — Eight verified defects are already fixed in code that has never been deployed. The live
site runs `340c26b`.

**Evidence** (re-verified 2026-08-22):
```
git log origin/main..HEAD          -> 1 unpushed commit
GET /robots.txt                    -> 404
GET /sitemap.xml                   -> 404
grep cdn.tailwindcss.com (live)    -> 1   (120.4 KB render-blocking JIT compiler)
grep 'name="description"' (live)   -> 0
favicon.png live                   -> 132,331 bytes @ 353×353, rendered ~32px
```

**Affected URL** — entire site
**Affected file** — n/a (deployment action)

**Implementation**
```bash
git push origin main
```

**Expected impact** — Closes: no robots.txt, no sitemap, no meta description, no canonical, no Open
Graph, 120 KB render-blocking JS, 129 KB favicon, 382 KB logo, broken nav height class.
**−386 KB** first-load weight; removes all client-side CSS compilation. This is the single largest
crawling + Core Web Vitals gain available.

**Risk** — **Medium, and specific.** The Render build must execute `npm ci && npm run build:css`.
If that step fails, the site deploys **with no CSS at all**.

**Dependencies** — none. Must precede P0-2, P1-1, P1-3, P1-4.

**Validation**
```bash
# In the Render build log, confirm this line appears:
#   "Building the Tailwind stylesheet..."
curl -s -o /dev/null -w "%{http_code}\n" https://gemileads.gr/robots.txt   # 200
curl -s -o /dev/null -w "%{http_code}\n" https://gemileads.gr/sitemap.xml  # 200
curl -s https://gemileads.gr | grep -c "cdn.tailwindcss.com"              # 0
curl -s https://gemileads.gr | grep -c 'name="description"'               # 1
```
Roll back immediately if the homepage renders unstyled.

---

## P0-2 · Remove two false feature claims from the pricing page

**Problem** — `/pricing/` advertises two capabilities the shipped product does not provide. One is a
**paid feature on a €49/month tier**.

**Evidence** — exact line numbers, both sides:
```
templates/pricing.html:100   "Daily & Weekly Digest"          (Business tier)
gemiapp/services.py:363      raise ValueError("Το εβδομαδιαίο digest έχει καταργηθεί.")

templates/pricing.html:142   "Ενημέρωση ανά 3 ώρες (08:00 - 00:00)"   (Enterprise tier)
gemiapp/tasks.py:51          if not 8 <= current_hour <= 23:
```

**Affected URL** — `https://gemileads.gr/pricing/`
**Affected file** — `templates/pricing.html` lines 100, 142

**Implementation**
- Line 100: `Daily & Weekly Digest` → `Daily Email Digest`
- Line 142: `(08:00 - 00:00)` → `(08:00 - 23:00)`

**Expected impact** — SEO impact is indirect but real: accuracy is a Trustworthiness signal, the
heaviest E-E-A-T factor, and this page is the primary commercial target. The **primary** reason is
non-SEO: selling a deleted feature is a consumer-protection and chargeback exposure.

**Risk** — None.

**Dependencies** — **Blocks P1-3.** Structured data restates on-page content; marking up false claims
would propagate the error into machine-readable form.

**Validation**
```bash
curl -s https://gemileads.gr/pricing/ | grep -c "Weekly Digest"   # 0
curl -s https://gemileads.gr/pricing/ | grep -c "00:00"           # 0
```

---

## P0-3 · Link `/pricing/` from public pages

**Problem** — The highest commercial-intent page has **zero inbound internal links**. Every
`{% url 'pricing' %}` reference sits inside a login-gated template, and the nav link is inside
`{% if user.is_authenticated %}`. Only logged-in users can reach it.

**Evidence** — crawled all 5 public pages, resolved every internal link:
```
/signup/ 13x   /login/ 11x   /  5x   /password_reset/ 1x   /resend-verification/ 1x
/pricing/  0x   <-- orphan
```
Repo confirms: `dashboard.html`, `settings.html`, `radars/list.html`, `companies/detail.html` — all
`@login_required`.

**Affected URL** — `https://gemileads.gr/pricing/`
**Affected file** — `templates/base.html` (public nav + footer), `templates/home.html` (CTA)

**Implementation** — Add a `Τιμές` link to the **unauthenticated** nav branch and to the footer in
`templates/base.html`; add a pricing CTA section to `templates/home.html`.

**Expected impact** — Site architecture and indexing. An orphan page receives no internal PageRank
and has no crawl path. A sitemap entry (from P0-1) aids discovery but is a **far weaker** signal than
internal linking. Also removes a conversion dead-end for logged-out visitors.

**Risk** — None.

**Dependencies** — Independent, but pairs naturally with P0-1.

**Validation**
```bash
curl -s https://gemileads.gr | grep -c 'href="/pricing/"'   # >= 1
```
Then: Search Console → Pages should begin reporting impressions for `/pricing/` within ~2 weeks.

---

# P1 — HIGH IMPACT

## P1-1 · Fix the canonical tag to strip query parameters

**Problem** — The canonical shipping in `446a28f` echoes the query string, so every parameter URL
**self-canonicalises** instead of consolidating. The site serves byte-identical content on unlimited
parameter URLs.

**Evidence**
```
/                     md5 ee97e734044284048042fa1639673220
/?utm_source=test     md5 ee97e734044284048042fa1639673220   <-- identical, 200

templates/base.html:18  {{ request.build_absolute_uri|slice:':512' }}
Rendered with ?utm_source=x  ->  http://gemileads.gr/?utm_source=x
```

**Affected URL** — all pages
**Affected file** — `templates/base.html` line 18 (canonical), line 26 (`og:url`)

**Implementation**
```django
<link rel="canonical" href="{% block canonical %}{{ request.scheme }}://{{ request.get_host }}{{ request.path }}{% endblock %}">
<meta property="og:url" content="{{ request.scheme }}://{{ request.get_host }}{{ request.path }}">
```
Keep the `{% block %}` so future paginated views can override deliberately.

**Expected impact** — Indexing. Consolidates every campaign/tracking URL to one canonical, preventing
duplicate-content dilution. This is the one fix that makes the canonical tag actually do its job.

**Risk** — Low. Any future page genuinely needing a parameterised canonical must override the block.

**Dependencies** — Requires P0-1 (canonical does not exist live yet).

**Validation**
```bash
curl -s "https://gemileads.gr/?utm_source=test" | grep -o '<link rel="canonical"[^>]*>'
# must output href="https://gemileads.gr/" with NO query string
```
*Scheme note: `SECURE_PROXY_SSL_HEADER` is set under `if not DEBUG`, so production emits `https://`. Verified.*

---

## P1-2 · Cache the site-wide company count

**Problem** — An uncached `COUNT(*)` over 17,789 rows executes on **every request site-wide** via a
context processor, plus 3 further aggregates on the homepage.

**Evidence** — server cost isolated by comparing a static file (no Django, no DB) against HTML:
```
static /static/js/app.js   ttfb 0.275 / 0.289 / 0.240 s
HTML   /                   ttfb 0.835 / 0.836 / 0.823 s
                           ~0.56 s = Django view + DB + template render
```
Six consecutive runs showed no cold-start outlier — this is **steady-state** latency.

```python
# gemiapp/context_processors.py
def global_stats(request):
    return {"global_company_count": Company.objects.count()}   # uncached, every request
```

**Affected URL** — all pages
**Affected file** — `gemiapp/context_processors.py`; also `gemiapp/views.py::home`

**Implementation**
```python
from django.core.cache import cache

def global_stats(request):
    return {"global_company_count": cache.get_or_set(
        "global_company_count", lambda: Company.objects.count(), 3600)}
```
Apply the same to the three `home()` aggregates.

**Expected impact** — Core Web Vitals. TTFB gates LCP; ~0.83 s consumes most of a good LCP budget
before a byte renders.

**Risk** — Low. `LocMemCache` is per-process, so each gunicorn worker caches separately — acceptable
for a marketing counter that need not be exact.

**Dependencies** — None.

**Validation**
```bash
for i in 1 2 3; do curl -s -o /dev/null -w "%{time_starttransfer}\n" https://gemileads.gr/; done
# should move toward the ~0.27 s static baseline
```
**Falsifiable:** if TTFB stays ≥0.8 s after this, the cause is the Render instance tier, not the
queries — a different fix entirely. Do not assume success.

---

## P1-3 · Add Organization + WebSite + SoftwareApplication JSON-LD

**Problem** — Zero structured data in any format across all 5 public pages. No entity definition
exists for Google's Knowledge Graph or for AI answer engines.

**Evidence**
```
JSON-LD / Microdata / RDFa on every public page:  0 / 0 / 0
grep -rn "ld+json|schema.org|itemscope" templates/ gemiapp/ config/  ->  no matches
```

**Affected URL** — all pages (Organization/WebSite); `/pricing/` (SoftwareApplication)
**Affected file** — `templates/base.html` line 30 (before `{% block head %}`);
`templates/pricing.html` (needs a new `{% block head %}`)

**Implementation** — Validated JSON-LD is ready in `generated-schema.json`. Two blocks:
`Organization` + `WebSite` in a `@graph` site-wide, and `SoftwareApplication` with three `Offer`s on
`/pricing/`.

Encode **only** verified facts: name, url, logo, `email` (`config/settings.py:120`), prices from
`PLAN_PRICES`. **Omit** `legalName`, `address`, `vatID`, `sameAs`, `aggregateRating`, `review` — none
are verifiable. The only "GEMILEADS IKE" string on the site is inside a **fake demo record**
(`templates/home.html:132`) and must not be used as a legal declaration.

Exclude the Custom tier from `offers` — it is "Κατόπιν Επικοινωνίας" (`PLAN_PRICES["custom"] = 0`);
an `Offer` with `price: 0` would state the product is free.

**Expected impact** — Structured data + GEO. Defines the business entity for search and AI engines.

**Risk** — Low, with one trap: `SoftwareApplication` references `#organization` by `@id`. If the
Organization block is missing, the reference dangles **while every block still validates in
isolation** — a silent failure.

**Dependencies** — **Requires P0-2** (do not mark up false claims). Ship the Organization block
before or with the SoftwareApplication block.

**Validation** — Rich Results Test + Schema Markup Validator on `/` and `/pricing/`. Explicitly
confirm the `@id` cross-references resolve **between** blocks, not merely that each block passes.

---

## P1-4 · Make `og:image` an absolute URL

**Problem** — `og:image` renders a root-relative path. The Open Graph spec requires absolute, and
scrapers fetch it out of page context — so **every shared link produces a preview with no image**.

**Evidence**
```
templates/base.html:27   {% static 'images/logo.png' %}
Rendered:                /static/images/logo.png     <-- relative
```

**Affected URL** — all pages
**Affected file** — `templates/base.html` line 27

**Implementation**
```django
<meta property="og:image" content="{{ request.scheme }}://{{ request.get_host }}{% static 'images/logo.png' %}">
```

**Expected impact** — Organic CTR on shared links (LinkedIn, Slack, WhatsApp, X). Not a ranking
factor; a genuine click-through and referral-traffic factor.

**Risk** — None.

**Dependencies** — Requires P0-1 (`og:image` does not exist live yet).

**Validation** — Paste a page URL into the LinkedIn Post Inspector or Slack. An image must render.

---

# P2 — WORTH IMPLEMENTING

## P2-1 · WhiteNoise immutable caching

**Problem** — Static assets are served with `max-age=60`, which is too short for Cloudflare to edge-cache.

**Evidence**
```
/static/images/favicon.png   Cache-Control: max-age=60, public   cf-cache-status: DYNAMIC
/static/js/app.js            Cache-Control: max-age=60, public   cf-cache-status: DYNAMIC
```
`DYNAMIC` = served from origin every time. Filenames are unhashed, so a long TTL is not currently safe.

**Affected URL** — all static assets · **Affected file** — `config/settings.py`

**Implementation**
```python
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}
WHITENOISE_MAX_AGE = 31536000
```

**Expected impact** — Core Web Vitals on repeat visits. Hashing makes a 1-year immutable TTL safe and
lets Cloudflare edge-cache.

**Risk** — **Medium.** `CompressedManifestStaticFilesStorage` **hard-fails the build** on any
`{% static %}` reference to a missing file. Run `collectstatic` locally first.

**Dependencies** — Requires P0-1 (the build must already produce `app.css`).

**Validation**
```bash
python manage.py collectstatic --noinput          # locally, must succeed first
curl -sD- -o/dev/null https://gemileads.gr/static/css/app.<hash>.css | grep -i cache-control
# expect: max-age=31536000, immutable   and cf-cache-status: HIT on a second request
```

---

## P2-2 · Add three self-contained answer blocks to the homepage

**Problem** — No passage on the site is extractable as an answer, and the core value proposition is
never stated. AI engines cite self-contained passages of roughly 134–167 words.

**Evidence** — homepage segmented by heading:
```
passages under headings: 14      in optimal 134-167w band: 0      longest: 39 words
```
Term frequency on the live homepage — the product's actual CSV fields:
```
αφμ 0    επωνυμ 0    διεύθυνση 0    νομική μορφή 0    τηλέφων 0
```
Yet `gemiapp/views.py::export_csv` delivers 9 fields **including ΑΦΜ, Email and Website**.

**Affected URL** — `https://gemileads.gr/` · **Affected file** — `templates/home.html`

**Implementation** — Three sections, each opening with a direct definitional sentence:
1. *Τι είναι το ΓΕΜΗ;* — "Το ΓΕΜΗ (Γενικό Εμπορικό Μητρώο) είναι…"
2. *Τι περιλαμβάνει κάθε lead;* — name the real 9 fields explicitly
3. *Πόσο συχνά ενημερώνονται τα δεδομένα;* — daily 09:00; Enterprise every 3h, 08:00–23:00

Write naturally. **Do not** repeat keywords for density — that is keyword stuffing and is
counterproductive.

**Expected impact** — Content relevance, topical authority, GEO. Creates the first citable passages
and states the strongest B2B selling point, which is currently invisible.

**Risk** — Low. Failure mode: word count rises but no passage stands alone without context.

**Dependencies** — Must be consistent with P0-2 (same facts) and the corrected ΚΑΔ figure (P2-4).

**Validation** — Each new section should read as a complete answer when copied out of the page in
isolation. Leading indicator: `/` begins appearing for "τι είναι το ΓΕΜΗ"-type queries.

---

## P2-3 · Add an accessible name to the icon-only menu button

**Problem** — The mobile hamburger button (`#menuButton`) is icon-only with no accessible name.

**Evidence** — `aria-label` count on the homepage: **1** (site-wide); the menu button has none.

**Affected URL** — all pages · **Affected file** — `templates/base.html`

**Implementation** — `aria-label="Άνοιγμα μενού"` on the button; `aria-hidden="true"` on its inner SVG.

**Expected impact** — GEO/agentic browsing. AI agents increasingly read the **accessibility tree**;
an unnamed interactive control is genuinely ambiguous to them. Also a real accessibility fix.

**Risk** — None. **Dependencies** — None.

**Validation** — DevTools → Accessibility pane: the button must expose a name.

> Scoped deliberately to **one** control. Blanket `aria-hidden` on all 36 decorative SVGs was cut as
> cosmetic.

---

## P2-4 · Correct the ΚΑΔ count on the homepage

**Problem** — The homepage states "9.744 ΚΑΔ 2025". The figure matches neither source of truth.

**Evidence**
```
homepage claim         : 9.744
kad_2025.json entries  : 9,651
ActivityCode DB rows   : 10,463
```

**Affected URL** — `https://gemileads.gr/` · **Affected file** — `templates/home.html` line 48

**Implementation** — Replace with the figure you can defend. The catalogue file (**9.651**) is the
stable, quotable number; the DB count drifts as GEMI-only codes are appended.

**Expected impact** — GEO/trust. This is the site's most quotable statistic; an AI engine citing it
would propagate a wrong figure attributed to your brand.

**Risk** — None. **Dependencies** — Pairs with P2-2.

**Validation** — `curl -s https://gemileads.gr | grep -c "9.744"` → `0`

---

# P3 — OPTIONAL

## P3-1 · Add truthful `lastmod` to the sitemap
**File:** `gemiapp/seo.py` — `StaticViewSitemap` has no `lastmod`.
**Implementation:** hardcoded per-page dates, bumped only on genuine content change.
**Do not** wire to `timezone.now()` or `Company.updated_at` — an always-fresh `lastmod` is the pattern
Google learns to distrust, which is worse than omitting it.
**Impact:** minor crawl-scheduling signal. **Validation:** `curl -s .../sitemap.xml | grep -c lastmod`.

## P3-2 · Self-host Inter, or reduce weights
**File:** `templates/base.html`
Render-blocking third-party stylesheet requesting 5 weights (400–800). `preconnect` and
`display=swap` are already correct, so this is the residual cost only.
**Impact:** removes one third-party origin from the critical path. **Risk:** low.
**Do after** P0-1 and P1-2 — measure before spending effort here.

## P3-3 · Convert `logo.png` to WebP
Measured: 104.7 KB PNG → **8.9 KB** WebP q82 (91% smaller). Fetched by social scrapers, all of which
support WebP.
**Do NOT convert the favicon** — browser support for WebP favicons is inconsistent; PNG is universal.
A blanket "convert all images to WebP" recommendation would be wrong here.
**Dependencies:** only matters once P1-4 makes scrapers actually fetch it.

## P3-4 · Validate search demand before any content programme
**Not a code change — a gate.** No keyword, volume, ranking or competitor data was available at any
point in this audit, and none was invented. Before investing in articles or segment landing pages,
confirm real demand in Search Console (once P0-1 lets data accrue) for ΓΕΜΗ / ΚΑΔ / νέες επιχειρήσεις.

**A genuine possible outcome is that demand is too thin to justify the work** — in which case skip
content entirely and invest in direct sales. This is why the informational cluster and segment
landing pages were cut from P0–P2 rather than scheduled.

---

# Execution Order

```
P0-1 deploy 446a28f ─┬─> P1-1 canonical
                     ├─> P1-4 og:image absolute
                     ├─> P2-1 WhiteNoise caching
                     └─> P1-3 schema ──requires── P0-2 fix false claims
P0-2 false claims ───┴─> P2-2 answer blocks (same facts)
P0-3 link /pricing/     (independent)
P1-2 cache count        (independent)
P2-3 menu aria-label    (independent)
P2-4 ΚΑΔ figure         (independent)
```

**Day 1:** P0-1 → P0-2 → P0-3
**Week 1:** P1-1, P1-2, P1-3, P1-4
**Weeks 2–3:** P2-1 … P2-4
**Backlog:** P3-1 … P3-3 · **Gate before content:** P3-4

---

## Standing Limits

- Every retained finding was re-verified live on 2026-08-22 with exact file/line evidence.
- **No keyword volume, traffic, ranking, backlink, competitor or Core Web Vitals field data was
  available at any point, and none was invented.** No CrUX/GSC credentials are configured, so
  **LCP, INP and CLS are deliberately never quantified** anywhere in this plan. Performance claims
  rest on measured TTFB, transfer weight and render-blocking analysis only.
- Scores in the superseded reports are heuristics, not Google-internal signals. Search Console is the
  first-party source of truth.
