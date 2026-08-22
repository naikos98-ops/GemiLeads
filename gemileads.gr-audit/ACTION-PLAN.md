# Action Plan — gemileads.gr

Ordered by dependency, not just severity. Each item states how you would know it **failed**, and a
leading indicator you can watch without re-running the audit.

---

## Phase 0 — Deploy what is already built (Day 1)

> One action closes 8 findings. Nothing else should happen before this, because it changes the
> baseline every later measurement is compared against.

### 0.1 · Push and deploy commit `446a28f`
**Closes:** T1, T2, O1, O2, P1, I1, I2, M1, M2 (partial)
**Effort:** Easy · **Impact:** Score 41 → ~68

```bash
git push origin main
```

**Verify after deploy — all four must pass:**
```bash
curl -s -o /dev/null -w "%{http_code}\n" https://gemileads.gr/robots.txt      # expect 200
curl -s -o /dev/null -w "%{http_code}\n" https://gemileads.gr/sitemap.xml     # expect 200
curl -s https://gemileads.gr | grep -c "cdn.tailwindcss.com"                  # expect 0
curl -s https://gemileads.gr | grep -c 'name="description"'                   # expect 1
```

**How it fails:** the Render build must run `npm ci && npm run build:css`. If that step fails, the
site deploys **with no CSS at all**. Check the build log for `Building the Tailwind stylesheet...`
before trusting the deploy. Roll back immediately if the homepage renders unstyled.

**Leading indicator:** Search Console → Pages report should begin showing sitemap-discovered URLs
within ~1 week.

---

## Phase 1 — Critical fixes not yet built (Week 1)

### 1.1 · Link `/pricing/` from public pages `[T3 · P0]`
**Effort:** Easy · **Blocks:** nothing · **Unblocks:** commercial-query visibility

Add to `templates/base.html`: a `Τιμές` link in the public (unauthenticated) nav **and** in the footer.
Add a pricing CTA block to `templates/home.html`.

**How it fails:** `curl -s https://gemileads.gr | grep -c 'href="/pricing/"'` still returns `0`.
**Leading indicator:** `/pricing/` starts accruing impressions in Search Console.

> Do this even though `446a28f` adds pricing to the sitemap. A sitemap entry with zero internal links
> is a weak signal; internal linking is the actual ranking mechanism.

### 1.2 · Add Terms, Privacy, Contact + a real footer `[C1 · P0]`
**Effort:** Medium · **Unblocks:** 1.3 (Organization schema needs this data)

Create `/terms/`, `/privacy/`, `/contact/`. Footer must carry: company legal name, ΑΦΜ, ΓΕΜΗ number,
registered address, support email, and links to the two policy pages.

**Why this is P0 and not cosmetic:** it is simultaneously (a) a QRG trust signal on a site that takes
payments, (b) a GDPR obligation for an EU IKE, and (c) a Stripe live-account prerequisite. The SEO
value is real but it is the *third* reason, not the first.

**How it fails:** a rater (or a customer) cannot find who they are paying or how to contact them.
**Leading indicator:** none directly observable — this is a floor-raising fix, not a traffic fix.

---

## Phase 2 — High-impact improvements (Weeks 2–3)

### 2.1 · Cache the site-wide company count `[P2 · P1]`
**Effort:** Easy

`gemiapp/context_processors.py` runs an uncached `COUNT(*)` on **every request site-wide**. Wrap in
`cache.get_or_set(..., 3600)`.

**How it fails:** TTFB stays ≥0.8 s after the change, which would mean the latency is Render
cold-start rather than the query — a different fix (paid instance tier / keep-warm).
**Leading indicator:** `curl -w "%{time_starttransfer}"` on a warm instance.

### 2.2 · WhiteNoise immutable caching `[T4 · P2]`
**Effort:** Easy

Add `STORAGES` with `CompressedManifestStaticFilesStorage` + `WHITENOISE_MAX_AGE = 31536000`.

**How it fails:** `collectstatic` errors on a missing `{% static %}` target — run it locally first.
**Verify:** `curl -sD- -o/dev/null https://gemileads.gr/static/css/app.<hash>.css | grep -i cache-control`
should show `max-age=31536000, immutable`.

### 2.3 · Organization + WebSite + SoftwareApplication schema `[S1 · P1]`
**Effort:** Easy · **Depends on 1.2**

JSON-LD drafts in `findings/schema.md`. Do **not** add FAQPage (Google retired FAQ rich results for all
sites on 2026-05-07) or HowTo (deprecated 2023).

**How it fails:** Rich Results Test reports missing required properties, or the declared contact data
contradicts the footer.
**Leading indicator:** Search Console → Enhancements picks up the Organization entity.

### 2.4 · Fix the H1→H4 outline break `[O3 · P2]`
**Effort:** Easy — demote the four preview-card labels in `templates/home.html` to `<p>`/`<span>`.

### 2.5 · Expand homepage and pricing copy `[C2 · P1]`
**Effort:** Medium — target 800–1,200 words of genuine explanation on `/`.

**How it fails:** word count rises but the text is padding — no new question is answered. Judge by
whether a stranger could explain what ΓΕΜΗ data you provide after reading it.

---

## Phase 3 — Content & authority (Month 2+)

### 3.1 · Validate demand before building `[C3 prerequisite]`
**Effort:** Easy · **Do this first**

I had no keyword data and invented none. Before writing anything, confirm real demand in Search Console
(once sitemap data accrues) or a keyword tool for: *ΓΕΜΗ*, *ΚΑΔ 2025*, *νέες επιχειρήσεις*, *αναζήτηση ΑΦΜ*.

**How it fails:** the terms show negligible volume, in which case skip 3.2 entirely and invest in
direct sales instead. **This is a genuine possible outcome — do not skip this gate.**

### 3.2 · Build an informational cluster `[C3 · P1]`
**Effort:** Hard · **Only after 3.1 confirms demand**

Candidates grounded in the actual product: "Τι είναι το ΓΕΜΗ", "Πώς λειτουργεί το Open Data API",
"Κατάλογος ΚΑΔ 2025", "Πώς βρίσκω νέες επιχειρήσεις ανά νομό".

**Quality gate — binding:** if you generate per-ΚΑΔ pages from `gemiapp/data/kad_2025.json` (9,651
entries) that is **programmatic SEO**. Enforce ≥60% unique content per page. A hard stop applies well
before publishing thousands of near-identical stubs — that pattern reliably triggers quality
suppression. Publish 20 genuinely useful pages before considering scale.

### 3.3 · Improve passage citability `[G2 · P1]`
Falls out of 3.2. Write standalone definitional paragraphs under descriptive headings.

---

## Phase 4 — Monitoring (ongoing)

1. **Verify Search Console** for `gemileads.gr` and submit the sitemap. Nothing in Phase 3 can be
   validated without it.
2. **Watch:** Pages/indexed count, `/pricing/` impressions, and CWV once field data accumulates.
3. **Re-audit** after Phase 2 completes.
4. **Self-host Inter** `[P3]` and reduce to the 2–3 weights actually used — deferred, low impact.

---

## Quick Wins (highest value ÷ effort)

| # | Action | Effort | Closes |
|---|---|---|---|
| 1 | Deploy `446a28f` | Easy | 8 findings |
| 2 | Link `/pricing/` publicly | Easy | P0 orphan page |
| 3 | Cache the global count | Easy | Per-request DB hit |
| 4 | WhiteNoise immutable caching | Easy | 60s cache TTL |
| 5 | Demote H4s on homepage | Easy | Outline break |

## Long-Term Opportunities

- **The ΚΑΔ catalogue is a real content asset** — 9,651 official classification codes already normalised
  in the repo. Handled well (curated, grouped, genuinely explained) it is a defensible topical moat.
  Handled badly (one thin page per code) it is a quality liability. The difference is entirely in
  execution discipline.
- **AI answer engines are the realistic near-term channel.** Crawler access is already perfect and the
  competition for Greek ΓΕΜΗ explanatory content is likely thin. This is a content investment, not a
  technical one.
- **Self-hosted fonts + immutable assets** would make the site genuinely fast rather than merely
  not-slow, once Phase 0–2 land.
