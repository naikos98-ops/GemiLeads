# Image SEO, Accessibility & Performance Audit — gemileads.gr

**Date:** 2026-08-22 · **Scope:** 5 public pages + 3 email templates
**Live commit:** `340c26b` · **Local HEAD:** `446a28f` (**unpushed**)

Nothing was modified.

---

## Headline: this site has no `<img>` elements

| Page | `<img>` | `<picture>` | inline `<svg>` | CSS bg | data: URI | preload |
|---|---:|---:|---:|---:|---:|---:|
| `/` | **0** | 0 | 9 | 1 | 1 | 0 |
| `/pricing/` | **0** | 0 | 21 | 1 | 1 | 0 |
| `/signup/` | **0** | 0 | 2 | 1 | 1 | 0 |
| `/login/` | **0** | 0 | 2 | 1 | 1 | 0 |
| `/password_reset/` | **0** | 0 | 2 | 1 | 1 | 0 |

All iconography is **inline SVG**. This makes the majority of classic image-SEO checks
**structurally non-applicable** — not passing by luck, but absent by architecture:

| Check | Status | Reason |
|---|---|---|
| Missing alt text | **N/A** | No `<img>` elements |
| Missing width/height | **N/A** | No `<img>` elements |
| Layout-shift (CLS) from images | **N/A** | SVGs sized via Tailwind (`h-5 w-5`, `h-7 w-7`) |
| `srcset` / `sizes` responsive | **N/A** | SVG is resolution-independent |
| Lazy loading | **N/A** | Nothing to lazy-load |
| `fetchpriority` / preload | **N/A** | See LCP below |
| Descriptive filenames | **N/A** | No content images |
| Duplicate assets | **Pass** | Distinct md5s, no duplication |
| Image sitemap | **N/A** | No indexable images |

**LCP consequence:** with no `<img>`, the LCP element is a **text block (the H1) or the hero SVG**.
There is therefore **no image preload or `fetchpriority="high"` opportunity** — a recommendation that
would normally appear here does not apply. LCP is gated by the 120 KB render-blocking Tailwind CDN
script instead (covered in the technical audit).

**Trade-off worth naming:** inline SVG means the site has **no Google Images surface at all**. For a
B2B data product that is an acceptable choice, but it is a choice, not an oversight.

---

# SEO ISSUES

## S1 · `og:image` renders a relative URL — social previews will not resolve · **P1 High**
**File:** `templates/base.html:27` (in unpushed `446a28f`) · **Confidence:** Verified

```django
<meta property="og:image" content="{% static 'images/logo.png' %}">
```

Rendered output, verified through Django:

```
/static/images/logo.png     <-- relative
```

The Open Graph spec requires an **absolute** URL. Facebook, LinkedIn, Slack, WhatsApp and X scrapers
fetch `og:image` out of page context and will not resolve a root-relative path. The result is a link
preview with **no image**, on every share of every page.

**Fix:**
```django
<meta property="og:image" content="{{ request.scheme }}://{{ request.get_host }}{% static 'images/logo.png' %}">
```

**Impact:** restores social link previews. This is the only genuine *SEO/discovery* image defect on
the site.

**How it fails:** paste a page URL into Slack or the LinkedIn Post Inspector after deploy — if no
image renders, the URL is still relative.

## S2 · `og:image` dimensions not declared · **P3 Low**
**File:** `templates/base.html` · **Confidence:** Verified

No `og:image:width` / `og:image:height`. Scrapers must download the image to determine layout, which
can cause a first-share preview to render without the image.

**Fix (after `446a28f` resizes the logo to 628×174):**
```html
<meta property="og:image:width" content="628">
<meta property="og:image:height" content="174">
<meta property="og:image:alt" content="Gemi Leads">
```

**Note:** 628×174 is below the 1200×630 recommended for large social cards. Since `twitter:card` is
set to `summary_large_image`, the preview will render small or letterboxed. Producing a proper
1200×630 share image is a separate, optional design task.

---

# ACCESSIBILITY ISSUES

## A1 · 36 decorative inline SVGs lack `aria-hidden="true"` · **P2 Medium**
**Files:** `templates/base.html`, `templates/home.html`, `templates/pricing.html`
**Confidence:** Verified

```
svg elements with aria-hidden: 0  (of 36 across public pages)
```

Every SVG is decorative (checkmarks, chevrons, icons accompanying visible text labels). Without
`aria-hidden="true"`, screen readers may announce them as unlabelled graphics, adding noise between
meaningful content.

**Fix:** add `aria-hidden="true"` and `focusable="false"` to purely decorative SVGs.

**Exception:** the hamburger menu button (`#menuButton`) is **icon-only** with no visible text — that
one needs a real accessible name (`aria-label="Άνοιγμα μενού"`), **not** `aria-hidden`. This is the
single highest-value accessibility fix here, since it is an interactive control that is currently
unnamed.

**SEO impact:** indirect only. Accessibility is not a ranking factor, but agentic browsers and AI
crawlers increasingly read the accessibility tree — an unnamed interactive control is genuinely
ambiguous to them.

## A2 · Email images use non-descriptive `alt="Logo"` · **P3 Low**
**Files:** `templates/emails/verification.html`, `templates/emails/daily_digest.html`,
`templates/registration/password_reset_email.html` · **Confidence:** Verified

```html
<img src="https://gemileads.gr/static/images/favicon.png" alt="Logo" width="48" height="48" …>
```

**What is already correct:** absolute URL ✅, `width`/`height` present ✅ (important in email clients,
which block images by default and would otherwise reflow).

**Issue:** `alt="Logo"` is not descriptive. With images blocked — the default in Outlook and Gmail —
the recipient sees the word "Logo".

**Fix:** `alt="Gemi Leads"`.

**Note:** email images are not crawled and have **no SEO impact**. This is purely recipient experience.

---

# PERFORMANCE ISSUES

## P1 · favicon.png is 129 KB — the entire image payload · **P1 High**
**File:** `static/images/favicon.png` · **Status:** fixed in unpushed `446a28f` · **Confidence:** Verified

| | Live | Local (post-`446a28f`) |
|---|---|---|
| Dimensions | **353 × 353** | 128 × 128 |
| Size | **129.2 KB** | 21.7 KB |
| Rendered at | 48×48 (email), ~32 px (tab) | — |

It is requested on **every page** and is **100% of the site's image bytes on first paint**. A 353×353
source for a 32 px render is ~7× the needed linear resolution.

**Fix:** deploy `446a28f` → **−108 KB**.

## P2 · logo.png is 382 KB and never actually loads · **P3 Low**
**File:** `static/images/logo.png` · **Confidence:** Verified

```
homepage references to logo.png: 0
```

Live it is **391,443 bytes at 1255×347**, but no page requests it — it is referenced only as
`og:image`, and that URL is currently relative (S1), so even scrapers do not fetch it.

**Current user-facing cost: zero.** It becomes real the moment S1 is fixed and scrapers begin
fetching it. `446a28f` reduces it to 105 KB, which resolves this before it matters.

## P3 · No WebP/AVIF · **P2 Medium**
**Confidence:** Verified by measurement

I encoded the actual assets rather than estimating:

| Asset (at post-`446a28f` size) | PNG | WebP q82 | Saving |
|---|---:|---:|---:|
| favicon 128×128 | 21.7 KB | **3.2 KB** | 85% |
| logo 628×174 | 104.7 KB | **8.9 KB** | 91% |

**Recommendation — split by asset, not blanket:**

- **logo.png → WebP: YES.** It is fetched by social scrapers, all of which support WebP. 105 KB → 8.9 KB.
- **favicon.png → WebP: NO.** Browser favicon support for WebP is inconsistent; PNG is universally
  accepted. Keep PNG, rely on the resize (P1). **A blanket "convert everything to WebP" recommendation
  would be wrong here.**

## P4 · `Cache-Control: max-age=60` — Cloudflare is not caching images · **P2 Medium**
**File:** `config/settings.py` (no `STORAGES` configured) · **Confidence:** Verified

```
/static/images/favicon.png   Cache-Control: max-age=60, public   cf-cache-status: DYNAMIC
/static/images/logo.png      Cache-Control: max-age=60, public   cf-cache-status: DYNAMIC
```

`cf-cache-status: DYNAMIC` means Cloudflare serves these **from origin every time** — a 60-second TTL
is not worth edge-caching. Every repeat visitor re-downloads the favicon from origin.

**Fix:** `CompressedManifestStaticFilesStorage` + `WHITENOISE_MAX_AGE = 31536000`. Hashed filenames
make a 1-year immutable TTL safe. (Same fix as the technical audit's P2-1 — one change resolves both.)

## P5 · No image compression beyond resize · **P3 Low**
Both PNGs are RGBA and unoptimised beyond Pillow's `optimize=True` in `446a28f`. Running `oxipng` or
`pngquant` would recover a further ~10–30%. Marginal once P1 and P3 land.

---

## Summary

| Metric | Count | Status |
|---|---:|---|
| Total `<img>` on public pages | **0** | N/A by design |
| Missing alt text | 0 | N/A |
| Missing dimensions | 0 | N/A |
| Oversized assets (>100 KB) | **2** | 129 KB + 382 KB — both fixed in `446a28f` |
| Wrong format | 1 | logo.png → WebP |
| Not lazy-loaded | 0 | N/A |
| Duplicate assets | 0 | Pass |
| Decorative SVG missing `aria-hidden` | **36** | A1 |
| Broken social-preview URL | **1** | S1 — highest-value fix |

### Prioritised list

| # | Action | Category | Effort | Saving / Impact |
|---|---|---|---|---|
| 1 | Deploy `446a28f` | Performance | Easy | **−386 KB** total assets |
| 2 | Make `og:image` absolute | **SEO** | Easy | Restores all social previews |
| 3 | `aria-label` on the icon-only menu button | Accessibility | Easy | Names an unlabelled control |
| 4 | WhiteNoise immutable caching | Performance | Easy | Enables Cloudflare edge caching |
| 5 | logo.png → WebP (**not** the favicon) | Performance | Easy | −96 KB for scrapers |
| 6 | `aria-hidden="true"` on 36 decorative SVGs | Accessibility | Easy | Cleaner a11y tree |
| 7 | `og:image:width/height/alt` | SEO | Easy | Reliable first-share preview |
| 8 | `alt="Gemi Leads"` in email templates | Accessibility | Easy | Recipient experience only |

**Item 2 is the only true image-SEO defect** and it is the one most likely to be missed, because the
image itself is fine — the URL that points at it is not.

---

## Methodology & Limits

- All measurements taken live 2026-08-22. WebP savings were produced by **actually encoding** the
  assets, not estimated from typical ratios.
- Live assets compared byte-for-byte against the local post-`446a28f` versions.
- No Google Images ranking, impression or SERP data was available (no DataForSEO extension) and none
  was invented.
- Email templates were included because they contain the site's only `<img>` elements, though they
  carry no SEO weight.
