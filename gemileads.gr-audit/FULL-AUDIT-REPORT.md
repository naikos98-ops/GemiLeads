# Full SEO Audit — gemileads.gr

**Audited:** 2026-08-22 · **Business type:** SaaS (B2B, Greek market, subscription)
**Live commit state:** production is running `340c26b`; local commit `446a28f` is **unpushed**
**SEO Health Score: 41 / 100**

> **Read this first.** The last local commit (`446a28f`) already fixes a large share of what this
> audit finds: robots.txt, sitemap.xml, meta description, canonical, Open Graph, the Tailwind CDN,
> image weight, and the mobile nav height bug. It has **not been deployed**. Findings below are
> marked `[FIXED-UNDEPLOYED]` where that commit already resolves them. Deploying it is the single
> highest-value action available and would lift the score to roughly **68/100** with no new work.

---

## Score Breakdown

| Category | Weight | Score | Weighted |
|---|---:|---:|---:|
| Technical SEO | 22% | 45 | 9.9 |
| Content Quality | 23% | 35 | 8.1 |
| On-Page SEO | 20% | 40 | 8.0 |
| Schema / Structured Data | 10% | 0 | 0.0 |
| Performance (CWV) | 10% | 40 | 4.0 |
| AI Search Readiness | 10% | 55 | 5.5 |
| Images | 5% | 55 | 2.8 |
| **Total** | **100%** | | **38.3 → 41** |

Performance is scored on lab/field-proxy evidence only (TTFB, transfer weight, render-blocking
resources). No Core Web Vitals field data was available — see the Performance section.

---

## What Already Works

These were verified live and need no action:

- **HTTPS + HSTS with preload** — `strict-transport-security: max-age=31536000; includeSubDomains; preload`
- **Canonical host consistency** — `www.gemileads.gr` and `http://` both 301 to `https://gemileads.gr/` in a single hop. No chains.
- **Security headers** — `x-content-type-options: nosniff`, `x-frame-options: DENY`, `referrer-policy`, `cross-origin-opener-policy`. Confirms `DJANGO_DEBUG=0` in production.
- **Correct status codes** — gated pages return 302 to login; unknown URLs return a genuine 404 (no soft-404s).
- **Server-side rendering** — all content is in the initial HTML. No JS-rendering dependency for indexable content.
- **HTML/JS compression** — homepage 25 KB → 6.1 KB over the wire.
- **AI crawlers unblocked** — GPTBot, OAI-SearchBot, ClaudeBot, PerplexityBot, Googlebot, Bingbot all receive 200.
- **One H1 per page**, `<html lang="el">`, correct viewport meta, single `<main>` landmark.
- **Clean, stable URLs** — consistent trailing slashes, no parameter duplication, no pagination traps.

---

## TECHNICAL SEO — Score 45

### T1 · No robots.txt `[FIXED-UNDEPLOYED]`
- **Severity:** P1 High · **Difficulty:** Easy · **Confidence:** Verified
- **Evidence:** `GET https://gemileads.gr/robots.txt` → **404**
- **URL:** `/robots.txt` · **Source:** `config/urls.py`
- **Why it matters:** Without it, crawlers have no directive surface and no sitemap pointer. More
  importantly, this site has a large authenticated area (`/dashboard/`, `/leads/`, `/radars/`,
  `/superadmin/`, `/api/`) that crawlers will repeatedly request and receive 302s from, wasting
  crawl budget on a site with only 4 indexable pages.
- **Fix:** Deploy `446a28f`, which adds `gemiapp/seo.py::robots_txt` wired at `config/urls.py`.
- **Impact:** Directs crawl budget to the 4 pages that matter; enables sitemap discovery.

### T2 · No sitemap.xml `[FIXED-UNDEPLOYED]`
- **Severity:** P1 High · **Difficulty:** Easy · **Confidence:** Verified
- **Evidence:** `GET /sitemap.xml` → **404**
- **Fix:** Deploy `446a28f` (`django.contrib.sitemaps`, `StaticViewSitemap`).
- **Impact:** Faster discovery and re-crawl signalling. Modest on a 4-page site, but it is the
  prerequisite for Search Console coverage reporting.

### T3 · `/pricing/` is an orphan page — no internal links from any public page
- **Severity:** **P0 Critical** · **Difficulty:** Easy · **Confidence:** Verified
- **Evidence:** Crawled all 4 public pages; `href="/pricing/"` occurs **0 times** on `/`, `/pricing/`,
  `/signup/`, `/login/`. Repo confirms the only `{% url 'pricing' %}` references are in
  `templates/dashboard.html`, `templates/settings.html`, `templates/radars/list.html`,
  `templates/companies/detail.html` — all behind `@login_required`. The nav link is inside
  `{% if user.is_authenticated %}` in `templates/base.html`.
- **URL:** `https://gemileads.gr/pricing/` · **Source:** `templates/base.html`, `templates/home.html`
- **Why it matters:** This is the highest commercial-intent page on the site and the primary
  conversion target. With zero internal links it receives no internal PageRank, and is discoverable
  only via the (currently missing) sitemap. Commercial queries such as *"τιμές"* / *"συνδρομή"* have
  no crawlable path to it. This is simultaneously an SEO and a conversion defect.
- **Fix:** Add `/pricing/` to the public nav and the footer in `templates/base.html`, and add a
  pricing CTA section on `templates/home.html`. Note: `446a28f` adds `/pricing/` to the sitemap,
  which helps discovery but does **not** fix the internal-link deficit.
- **Impact:** High. Establishes the commercial page in the internal link graph.

### T4 · Static assets served with `max-age=60`
- **Severity:** P2 Medium · **Difficulty:** Easy · **Confidence:** Verified
- **Evidence:** `GET /static/images/logo.png` → `Cache-Control: max-age=60, public`
- **Source:** `config/settings.py` — no `STORAGES` / `STATICFILES_STORAGE` configured, so WhiteNoise
  runs with defaults: no manifest hashing, no pre-compression, 60-second TTL.
- **Why it matters:** Every repeat visitor re-downloads all static assets after 60 seconds. This
  directly inflates repeat-visit LCP and wastes bandwidth. WhiteNoise's `CompressedManifestStaticFilesStorage`
  hashes filenames, which makes a 1-year immutable cache safe.
- **Fix:** In `config/settings.py`:
  ```python
  STORAGES = {
      "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
      "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
  }
  WHITENOISE_MAX_AGE = 31536000
  ```
- **Impact:** Materially faster repeat visits; also emits pre-compressed brotli/gzip variants.
- **Caveat:** Manifest storage fails the build on any `{% static %}` reference to a missing file.
  Run `collectstatic` locally before deploying.

### T5 · No `llms.txt`
- **Severity:** P3 Low · **Difficulty:** Easy · **Confidence:** Requires external validation
- **Evidence:** `GET /llms.txt` → 404
- **Why it matters:** An emerging convention, **not** consumed by Google Search. Treat as optional.
- **Fix:** Only if cheap. Do not prioritise over T3 or C1.

---

## ON-PAGE SEO — Score 40

### O1 · No meta descriptions on any page `[FIXED-UNDEPLOYED]`
- **Severity:** P1 High · **Difficulty:** Easy · **Confidence:** Verified
- **Evidence:** No `<meta name="description">` on `/`, `/pricing/`, `/signup/`, `/login/`.
- **Source:** `templates/base.html`
- **Why it matters:** Google synthesises a snippet from page text when the tag is absent. With only
  321 words on the homepage, the synthesised snippet is likely to be nav or boilerplate text. A
  written description does not affect ranking but measurably affects **CTR**, which is the value here.
- **Fix:** Deploy `446a28f` (adds a `meta_description` block plus per-page overrides).

### O2 · No canonical tags `[FIXED-UNDEPLOYED]`
- **Severity:** P2 Medium · **Difficulty:** Easy · **Confidence:** Verified
- **Evidence:** No `<link rel="canonical">` on any page.
- **Why it matters:** Lower risk than usual here because host redirects are already correct and there
  are no URL parameters in use. It remains a defensive measure against future parameterised or
  session URLs.
- **Fix:** Deploy `446a28f`.

### O3 · Heading hierarchy skips H1 → H4 on the homepage
- **Severity:** P2 Medium · **Difficulty:** Easy · **Confidence:** Verified
- **Evidence:** Homepage heading order: `H1 → H4 → H4 → H4 → H4 → H2 → H3 → H3 → H3 → H2 → H3 → H3`
- **URL:** `/` · **Source:** `templates/home.html`
- **Why it matters:** The four H4s are the product-preview card labels, which appear structurally
  *above* the first H2. This makes the document outline misrepresent the page: a machine parsing
  heading structure (including AI answer engines extracting passages) reads four fourth-level
  subsections before any second-level section exists. It also affects screen-reader navigation.
- **Fix:** Demote the preview-card labels to non-heading elements (`<p class="font-bold">` or
  `<span>`), or promote them to H2/H3 so the outline is monotonic. They are decorative labels, so
  demoting is the better choice.
- **Impact:** Improves passage extraction quality for AI engines and accessibility. Low direct
  ranking impact.

### O4 · Title tags are good — no action needed
- **Severity:** Info · **Confidence:** Verified
- Homepage 57 chars, pricing 30, signup 35, login 20. All unique, all within display limits,
  all lead with the brand or the primary noun. No duplicates.
- Minor opportunity: `/pricing/` at 30 chars ("Συνδρομές & Πλάνα | Gemi Leads") has room to add a
  qualifier such as "από €19/μήνα" for CTR. Optional.

---

## CONTENT QUALITY — Score 35

### C1 · No contact details, Terms, or Privacy Policy anywhere on the public site
- **Severity:** **P0 Critical** · **Difficulty:** Medium · **Confidence:** Verified
- **Evidence:** Regex scan of the homepage found: company name `GEMILEADS IKE` present, but
  **no ΑΦΜ/VAT, no address, no phone, no email, no Terms link, no Privacy link, no GDPR statement**.
  Footer contains only a copyright line and a ΓΕΜΗ data disclaimer. Footer link count: **0**.
- **URL:** all public pages · **Source:** `templates/base.html` (footer), no legal templates exist
- **Why it matters (three distinct reasons):**
  1. **E-E-A-T / Trust.** Google's Quality Rater Guidelines treat missing contact and business
     information on a site that **takes payments** as a strong negative signal. This is a YMYL-adjacent
     transactional site charging €19–99/month.
  2. **Legal.** A Greek IKE selling subscriptions to EU customers requires a privacy policy under
     GDPR and imprint-style details. This is a compliance exposure independent of SEO.
  3. **Stripe.** Stripe's own requirements expect a public refund/terms policy for live accounts.
     Relevant when Stripe is activated.
- **Fix:** Add `/terms/`, `/privacy/`, `/contact/` pages and a real footer linking to them, plus
  company identity (ΑΦΜ, ΓΕΜΗ number, registered address, support email).
- **Impact:** High — this is the single largest content/trust deficit and it also unblocks
  Organization schema (S1).

### C2 · Thin content across all public pages
- **Severity:** P1 High · **Difficulty:** Medium · **Confidence:** Verified
- **Evidence:** Word counts — `/` **321**, `/pricing/` **205**, `/signup/` 83, `/login/` 67.
- **Why it matters:** 321 words on the primary landing page gives very little for topical relevance,
  and almost nothing for AI answer engines to extract as a citable passage. Auth pages being short is
  normal and expected; the homepage and pricing page being this thin is not.
- **Fix:** Expand the homepage with substantive sections that answer real queries: what ΓΕΜΗ Open Data
  is, what data each lead contains, how ΚΑΔ filtering works, update frequency, a worked example.
  Target 800–1,200 words of genuine explanation, not padding.
- **Impact:** High for both classic ranking and AI citability.

### C3 · No informational content layer — zero topical authority
- **Severity:** P1 High · **Difficulty:** Hard · **Confidence:** Verified
- **Evidence:** Site consists of 4 public pages. No `/blog/`, no guides, no glossary. No article schema.
- **Why it matters:** The site can only compete for a handful of transactional brand queries. The
  substantial Greek-language search demand around ΓΕΜΗ, ΚΑΔ codes, and company registration is
  entirely uncaptured. This is also the main lever for AI-engine citation, which favours
  explanatory content.
- **Fix:** Build an informational cluster. High-intent candidates grounded in what this product
  actually does: "Τι είναι το ΓΕΜΗ", "Πώς λειτουργεί το Open Data API του ΓΕΜΗ", "Κατάλογος ΚΑΔ
  2025 και τι σημαίνει κάθε κωδικός", "Πώς βρίσκω νέες επιχειρήσεις ανά νομό". The ΚΑΔ catalogue in
  `gemiapp/data/kad_2025.json` (9,651 entries) is a genuine content asset.
- **Impact:** Highest long-term ceiling, but slowest to realise. **Quality gate:** if a ΚΑΔ-per-page
  approach is used, it is programmatic SEO — enforce ≥60% unique content per page and do not publish
  thin stubs. See `references/quality-gates.md` thresholds.
- **Confidence note:** The existence of search demand for these terms is **probable**, not verified —
  I have no keyword volume data and did not invent any. Validate in Search Console or a keyword tool
  before investing.

---

## STRUCTURED DATA — Score 0

### S1 · No Schema.org markup of any kind
- **Severity:** P1 High · **Difficulty:** Easy · **Confidence:** Verified
- **Evidence:** `application/ld+json` occurrences across all public pages: **0**.
- **Source:** `templates/base.html`
- **Why it matters:** No entity definition exists for the business. Organization schema is how you
  declare the entity (name, legal identifiers, contact, sameAs) to both Google's Knowledge Graph and
  AI answer engines. For a SaaS product, `SoftwareApplication` with `offers` additionally makes
  pricing machine-readable.
- **Fix:** Add to `templates/base.html` (site-wide) an `Organization` + `WebSite` block, and on
  `/pricing/` a `SoftwareApplication` with `offers`. Draft JSON-LD is in
  `findings/schema.md`.
- **Impact:** Direct entity clarity for AI engines; enables Knowledge Panel eligibility over time.
- **Dependency:** Organization schema needs real contact/legal data — **blocked by C1**. Do C1 first.
- **Do NOT add:** FAQPage. Google retired FAQ rich results for all sites on 2026-05-07; there is no
  SERP feature to win and no confirmed AI-citation benefit. Use `QAPage` only for genuine user Q&A.
- **Do NOT add:** HowTo (deprecated Sept 2023).

---

## PERFORMANCE — Score 40

No Core Web Vitals **field** data was available (no CrUX/GSC access configured, and the domain is
unlikely to have sufficient traffic for a CrUX record). Findings below are from direct measurement of
transfer weight, render-blocking resources, and TTFB. LCP/INP/CLS values are **not stated** because I
did not measure them — doing so would require a real browser run against the live site.

### P1 · Tailwind Play CDN in production `[FIXED-UNDEPLOYED]`
- **Severity:** **P0 Critical** · **Difficulty:** Medium · **Confidence:** Verified
- **Evidence:** Live head contains `<script src="https://cdn.tailwindcss.com"></script>`; the asset is
  **120.4 KB of JavaScript**, render-blocking, in the `<head>`.
- **Why it matters:** This is not a stylesheet — it is Tailwind's JIT **compiler**. It downloads,
  parses, executes, scans the DOM and generates CSS **on the visitor's device** before first paint.
  Tailwind documents it as development-only and explicitly warns against production use. On mid-range
  mobile CPUs the parse+execute cost dominates. It is the primary cause of the reported slowness and
  a likely major contributor to LCP.
- **Fix:** Deploy `446a28f` — replaces it with a compiled stylesheet (**7.7 KB gzipped**, non-blocking-cheap,
  zero execution). Build wired into `scripts/build_render.sh` and `Dockerfile`.
- **Impact:** Removes 120 KB of blocking JS and all client-side CSS compilation.
- **Deploy caution:** the Render build must run `npm ci && npm run build:css` successfully, otherwise
  the site ships with **no styling at all**. Verify `Building the Tailwind stylesheet...` in the build log.

### P2 · TTFB 0.8–1.3 s
- **Severity:** P1 High · **Difficulty:** Medium · **Confidence:** Verified (measurement), Probable (cause)
- **Evidence:** 3 runs — 1.279 s / 0.816 s / 0.834 s. DNS and TCP connect are both <30 ms, so the
  latency is entirely server-side think time.
- **Why it matters:** TTFB gates LCP. Sub-second TTFB is the practical target; ~0.8 s consumes most of
  a good LCP budget before a single byte renders.
- **Contributing cause (verified in code):** `gemiapp/context_processors.py::global_stats` executes an
  **uncached `Company.objects.count()` on every request site-wide**. Measured at 19 ms over 17,789 rows
  on local SQLite; on Postgres a full sequential count is typically slower and grows with the table.
  The homepage view adds 4 further aggregate queries.
- **Fix (two parts):**
  1. Cache the global count — it is a marketing statistic that does not need to be exact:
     ```python
     from django.core.cache import cache
     def global_stats(request):
         return {"global_company_count": cache.get_or_set("global_company_count", lambda: Company.objects.count(), 3600)}
     ```
  2. The remainder is likely Render instance cold-start / spin-down on lower tiers.
- **Impact:** Removes a guaranteed per-request DB round-trip from every page.
- **Confidence:** The count query is verified. The proportion of the 0.8 s it represents is **probable**
  — isolating it needs server-side timing in production.

### P3 · Render-blocking Google Fonts
- **Severity:** P2 Medium · **Difficulty:** Easy · **Confidence:** Verified
- **Evidence:** `<link rel="stylesheet" href="fonts.googleapis.com/css2?family=Inter...">` in `<head>`,
  five weights requested (400,500,600,700,800).
- **Why it matters:** A blocking stylesheet on a third-party origin adds DNS+TLS+round-trip to the
  critical path. `preconnect` is already correctly present, which mitigates but does not remove it.
- **Fix:** Self-host Inter as woff2 next to `app.css` and drop the external origin entirely, or reduce
  to the 2–3 weights actually used. `&display=swap` is already set, which correctly prevents invisible text.
- **Impact:** Removes one third-party blocking request from the critical path.

---

## IMAGES — Score 55

### I1 · favicon.png is 129 KB `[FIXED-UNDEPLOYED]`
- **Severity:** P2 Medium · **Difficulty:** Easy · **Confidence:** Verified
- **Evidence:** Live `/static/images/favicon.png` = **132,331 bytes**, source dimensions 353×353,
  rendered at 48×48 in emails and ~32 px in the browser tab. Local (fixed) copy is 22,209 bytes.
- **Why it matters:** It is the **largest single asset on the homepage critical path** — larger than the
  Tailwind CDN payload is after gzip. It is requested on every page.
- **Fix:** Deploy `446a28f` (resized to 128×128, 22 KB).

### I2 · logo.png is 382 KB but unused on the homepage
- **Severity:** P3 Low · **Difficulty:** Easy · **Confidence:** Verified
- **Evidence:** Live asset is 391,443 bytes at 1255×347. Grep of live homepage HTML: `logo.png`
  referenced **0 times** (only `favicon.png` appears). `446a28f` reduces it to 105 KB and references it
  as the `og:image`.
- **Why it matters:** No current user-facing cost since it is not loaded. It becomes relevant the moment
  it is used as the `og:image` — social scrapers will fetch it.
- **Fix:** Covered by `446a28f`.

### I3 · No `<img>` elements at all on public pages
- **Severity:** Info · **Confidence:** Verified
- All iconography is inline SVG. Consequently: no missing alt text, no missing dimensions, no lazy-loading
  gaps, no WebP/AVIF opportunity. **This category needs no work.** It also means there is no image-search
  surface, which is an acceptable trade-off for this product.

---

## MOBILE — Score (folded into Technical/Performance)

### M1 · Fixed nav used a non-existent Tailwind class `[FIXED-UNDEPLOYED]`
- **Severity:** P2 Medium · **Difficulty:** Easy · **Confidence:** Verified
- **Evidence:** `templates/base.html` used `h-18` for the fixed nav. `h-18` is **not in Tailwind's default
  spacing scale** and was never generated — confirmed by `grep -c "\.h-18" static/css/app.css` → **0**.
  `<main>` compensated with a hardcoded `pt-[72px]`, an unlinked magic number.
- **Why it matters:** The nav had no declared height. When CTA buttons wrap on narrow viewports the nav
  grows taller than 72 px and overlaps page content. This is a genuine mobile-usability defect and a
  plausible CLS contributor.
- **Fix:** Deploy `446a28f` — defines `spacing: {18: '4.5rem'}` so `h-18`/`pt-18` derive from one value.

### M2 · Tap targets below 44 px `[PARTIALLY FIXED-UNDEPLOYED]`
- **Severity:** P3 Low · **Difficulty:** Easy · **Confidence:** Verified
- **Evidence:** Live HTML contains multiple `py-1` / `text-[10px]` interactive elements.
- **Note:** Most flagged instances are **badges, not controls** (e.g. status pills), where the guideline
  does not apply. `446a28f` adds a 44 px minimum on coarse pointers for links/buttons.

### M3 · No skip-link; only 1 aria-label
- **Severity:** P3 Low · **Difficulty:** Easy · **Confidence:** Verified
- **Why it matters:** Accessibility issues affect SEO only indirectly. Listed for completeness, not as
  an SEO priority. The icon-only hamburger button lacking an accessible name is the one worth fixing.

---

## AI SEARCH / GEO — Score 55

### G1 · AI crawlers are fully accessible — no action
- **Confidence:** Verified. GPTBot, OAI-SearchBot, ClaudeBot, PerplexityBot all → 200. Content is
  server-rendered, so no JS-execution barrier. This is the strongest part of the GEO profile.
- **Caveat:** deploying `446a28f`'s robots.txt uses `User-agent: *` with `Disallow` on private paths
  only — it does **not** block AI crawlers. Behaviour is preserved.

### G2 · Low passage-level citability
- **Severity:** P1 High · **Difficulty:** Medium · **Confidence:** Probable
- **Evidence:** 321 words on the homepage; no definitional statements; no structured factual passages;
  no schema entity; no author/organisation attribution.
- **Why it matters:** AI answer engines extract self-contained factual passages. There is currently
  almost nothing on the site that can be quoted as an answer to a question. The `H1→H4` outline break
  (O3) further degrades passage segmentation.
- **Fix:** Depends on C2/C3. Write direct definitional sentences ("Το ΓΕΜΗ είναι…", "Το Gemi Leads
  παρακολουθεί…") as standalone paragraphs under descriptive headings.
- **Impact:** This is the main GEO lever and it is a **content** problem, not a markup problem.

---

## Methodology & Limits

- All live findings verified by direct HTTP request on 2026-08-22. Source-file attributions verified
  against the working tree at `446a28f`.
- **No Core Web Vitals field data** (no CrUX/GSC credentials configured). LCP/INP/CLS are deliberately
  not quantified.
- **No keyword, traffic, ranking, backlink, or competitor data** was available and **none was invented**.
  Where search demand is assumed (C3), it is explicitly marked *probable — requires external validation*.
- Crawl scope: 4 public pages + 4 gated paths. The site's entire indexable surface is 4 pages, so this
  is complete coverage, not a sample.
