# Structured Data Audit — gemileads.gr

**Date:** 2026-08-22 · **Scope:** 5 public pages (complete indexable surface)
**Schema Score: 0/100** — no structured data exists in any format.

Nothing was modified. Generated JSON-LD is in `generated-schema.json` (not applied).

---

## Detection Results

| Page | JSON-LD | Microdata | RDFa |
|---|---:|---:|---:|
| `/` | 0 | 0 | 0 |
| `/pricing/` | 0 | 0 | 0 |
| `/signup/` | 0 | 0 | 0 |
| `/login/` | 0 | 0 | 0 |
| `/password_reset/` | 0 | 0 | 0 |

Also verified **absent from source**, not merely unshipped:

```
grep -rn "ld+json|schema.org|itemscope|itemprop" templates/ gemiapp/ config/  ->  no matches
```

The unpushed commit `446a28f` adds Open Graph and canonical tags but **no structured data**.

### Consequences for the requested checks

Because zero markup exists, four of the requested categories are **vacuously clean** — this is an
absence of data, not a clean bill of health:

| Requested check | Result |
|---|---|
| Invalid schema | None — nothing to validate |
| Duplicate schema | None — nothing to duplicate |
| Conflicting schema | None — nothing to conflict |
| Inaccurate properties | None — no properties exist |
| Deprecated types in use | None |
| **Missing schema** | **This is the entire finding** |

---

## Applicability Assessment

Each requested type was tested against the site rather than assumed.

| Type | Applicable? | Evidence |
|---|---|---|
| **Organization** | ✅ **Yes** | Real trading entity with a public site |
| **WebSite** | ✅ **Yes** | Standard site-level entity |
| **SoftwareApplication** | ✅ **Yes** | Web-based B2B SaaS with real, code-verified pricing |
| **WebPage** | ⚠️ Optional | Only adds value on `/pricing/` to bind page → software entity |
| **Service** | ❌ No | Would duplicate `SoftwareApplication`. Choosing both for the same offering creates a **conflicting** entity — pick one. `SoftwareApplication` is the better fit for a web app. |
| **Product** | ❌ No | This is subscription software, not a product with SKU/inventory. `Offer` inside `SoftwareApplication` covers pricing correctly. |
| **BreadcrumbList** | ❌ No | Flat site: all public pages at depth 0–1. Markup would describe a hierarchy that does not exist. |
| **FAQPage** | ❌ No | **Two independent reasons:** (1) no genuine Q&A content exists on any public page (0 FAQ blocks found); (2) Google **retired FAQ rich results for all sites on 2026-05-07** — no SERP feature remains. |
| **Article / BlogPosting** | ❌ No | No blog, no articles, no dated content, no author. No `blog/article/author/post` templates exist. |
| **Person** | ❌ No | No named author, founder, or team member is published anywhere |
| **LocalBusiness** | ❌ **No** | 0 premises signals (no opening hours, no address, no map, no "visit us"). SaaS sold nationally — `Organization` is the correct type. Using `LocalBusiness` would be **inaccurate markup**. |

---

## What can be truthfully encoded

I verified each fact against source before including it. **Nothing was fabricated.**

### Verified — safe to encode

| Property | Value | Source of truth |
|---|---|---|
| `name` | Gemi Leads | Site-wide branding |
| `url` | `https://gemileads.gr/` | Live |
| `logo` | `/static/images/logo.png`, 628×174 | `static/images/logo.png` (post-`446a28f` dimensions) |
| `email` | `info@gemileads.gr` | `config/settings.py:120` (`EMAIL_REPLY_TO`) |
| `inLanguage` | `el` | `<html lang="el">` |
| Offer prices | 19 / 49 / 99 EUR | `gemiapp/superadmin/services.py::PLAN_PRICES` — cross-checked against `templates/pricing.html` |

### Deliberately OMITTED — cannot be verified

| Property | Why omitted |
|---|---|
| `legalName` | **Important:** the only "GEMILEADS IKE" string on the site is inside a **fake demo record** (`templates/home.html:132`, labelled *Ενδεικτική απεικόνιση*, shown as "ΙΚΕ · ΠΑΤΡΑ"). It is **not** a legal declaration. Encoding it as `legalName` — or inferring a Patras address from it — would be fabrication. |
| `address` / `telephone` | No address or phone exists anywhere on the site or in the repo |
| `vatID` / ΓΕΜΗ identifier | Not published |
| `sameAs` | Zero social profile links found. An empty or guessed `sameAs` is worse than none |
| `aggregateRating` / `review` | No genuine reviews exist. Fabricated review markup is a **manual-action risk**, not merely ineffective |
| `foundingDate`, `numberOfEmployees`, awards | Not published |

Automated check on the generated file confirms: **no placeholder tokens, absolute URLs only, none of the nine fabrication-prone fields present.**

---

## Recommendations

### R1 · Organization + WebSite (site-wide) — **P1 High**
**File:** `templates/base.html` — insert immediately before `{% block head %}{% endblock %}` at **line 30**
**Confidence:** Verified · **Difficulty:** Easy

Uses a `@graph` so the two entities link by `@id` rather than duplicating the publisher object.

```html
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@graph": [
    {
      "@type": "Organization",
      "@id": "https://gemileads.gr/#organization",
      "name": "Gemi Leads",
      "url": "https://gemileads.gr/",
      "logo": {
        "@type": "ImageObject",
        "url": "https://gemileads.gr/static/images/logo.png",
        "width": 628,
        "height": 174
      },
      "email": "info@gemileads.gr",
      "description": "Καθημερινή παρακολούθηση νέων επιχειρήσεων από το επίσημο Open Data API του ΓΕΜΗ, με φίλτρα ΚΑΔ και περιοχής."
    },
    {
      "@type": "WebSite",
      "@id": "https://gemileads.gr/#website",
      "url": "https://gemileads.gr/",
      "name": "Gemi Leads",
      "inLanguage": "el",
      "publisher": { "@id": "https://gemileads.gr/#organization" }
    }
  ]
}
</script>
```

**Why:** currently no entity definition exists for Google's Knowledge Graph or AI answer engines.
This is the single highest-value schema addition.

**`SearchAction` deliberately omitted:** company search sits behind `@login_required`, so declaring a
sitelinks searchbox would advertise an endpoint crawlers cannot use — inaccurate markup.

**Enrichment dependency:** `legalName`, `address`, `vatID` and ΓΕΜΗ identifier should be added **once
a real `/contact/` or imprint page exists**. Until then they stay out.

---

### R2 · SoftwareApplication with Offers (pricing page) — **P1 High**
**File:** `templates/pricing.html` — add a `{% block head %}` (the template extends `base.html` at
line 1 but currently defines no head block)
**Confidence:** Verified · **Difficulty:** Easy

```html
{% block head %}
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "SoftwareApplication",
  "@id": "https://gemileads.gr/#software",
  "name": "Gemi Leads",
  "applicationCategory": "BusinessApplication",
  "operatingSystem": "Web",
  "url": "https://gemileads.gr/",
  "inLanguage": "el",
  "provider": { "@id": "https://gemileads.gr/#organization" },
  "offers": [
    { "@type": "Offer", "name": "Pro", "price": "19", "priceCurrency": "EUR",
      "url": "https://gemileads.gr/pricing/", "availability": "https://schema.org/InStock" },
    { "@type": "Offer", "name": "Business", "price": "49", "priceCurrency": "EUR",
      "url": "https://gemileads.gr/pricing/", "availability": "https://schema.org/InStock" },
    { "@type": "Offer", "name": "Enterprise / Real-Time", "price": "99", "priceCurrency": "EUR",
      "url": "https://gemileads.gr/pricing/", "availability": "https://schema.org/InStock" }
  ]
}
</script>
{% endblock %}
```

**Custom tier deliberately excluded:** priced "Κατόπιν Επικοινωνίας" (`PLAN_PRICES["custom"] = 0`).
An `Offer` with `price: 0` would state the product is free — a factual error.

**⚠ Accuracy blocker — fix content first.** The live pricing page currently contains claims the
product does not honour (verified against source):

| Live claim | Reality |
|---|---|
| Business: "Daily & **Weekly** Digest" | `gemiapp/services.py:363` raises `ValueError("Το εβδομαδιαίο digest έχει καταργηθεί.")` |
| Enterprise: "08:00 - **00:00**" | `gemiapp/tasks.py:51` enforces `8 <= hour <= 23` |

Schema is a machine-readable restatement of on-page content. Marking up a page whose feature claims
are already inaccurate propagates the error into structured data. **Correct the copy before shipping R2.**

---

### R3 · WebPage on `/pricing/` — **P3 Optional**
**File:** same `{% block head %}` as R2 (append to the `@graph`)

```json
{
  "@type": "WebPage",
  "@id": "https://gemileads.gr/pricing/#webpage",
  "url": "https://gemileads.gr/pricing/",
  "name": "Συνδρομές & Πλάνα | Gemi Leads",
  "inLanguage": "el",
  "isPartOf": { "@id": "https://gemileads.gr/#website" },
  "about": { "@id": "https://gemileads.gr/#software" }
}
```

Marginal benefit — it binds page to software entity. Skip if you want minimal markup.

---

### R4 · Article + Person — **deferred, not yet applicable**
Becomes applicable **only if** the informational content cluster ships. Do not add speculatively.
Requires a real named author with a bio; inventing one would be fabrication.

---

## Implementation Notes

**Server-render it.** Per Google's December 2025 JS SEO guidance, JSON-LD injected via JavaScript can
face delayed processing. Both blocks belong in Django templates, which is already SSR — verified:
`<h1>` present in raw HTML under `User-Agent: Googlebot`.

**Entity graph integrity.** R2 references `https://gemileads.gr/#organization`, defined in R1.
**Ship R1 first** — otherwise the `provider` reference dangles. Each block validates in isolation, so
this failure is silent.

**Validation checks performed on the generated file:**
- All blocks parse as valid JSON ✅
- Required properties present for every type ✅
- Absolute URLs only, no relative paths ✅
- No placeholder text ✅
- None of the nine fabrication-prone fields present ✅

**After deploying, verify with:** Rich Results Test and Schema Markup Validator. Confirm the `@id`
cross-references resolve between blocks, not just that each block validates alone.

**How this fails:** if `@id` references break, the entity graph silently fragments into unrelated
nodes while every individual block still passes validation.

**Leading indicator:** Search Console → Enhancements begins reporting the Organization entity.

---

## Priority Summary

| # | Action | Priority | Effort | Blocked by |
|---|---|---|---|---|
| R1 | Organization + WebSite in `base.html:30` | **P1** | Easy | — |
| R2 | SoftwareApplication + Offers in `pricing.html` | **P1** | Easy | Fix the two false pricing claims first |
| R3 | WebPage on `/pricing/` | P3 | Easy | R1 |
| R4 | Article + Person | Deferred | — | Content cluster must exist |
| — | Enrich R1 with `legalName`/`address`/`vatID` | P2 | Easy | A real `/contact/` page must exist |

**Do NOT implement:** Service, Product, BreadcrumbList, FAQPage, LocalBusiness — each verified
non-applicable above. **Never recommend:** HowTo (deprecated Sept 2023), SpecialAnnouncement
(deprecated July 2025).

---

## Methodology & Limits

- Detection performed live 2026-08-22 across all three formats on all 5 public pages.
- Every encodable fact traced to a source file or live response; unverifiable properties omitted rather
  than guessed.
- **No reviews, ratings, addresses, authors, awards, customer counts or organisation details were
  fabricated.** Where data was unavailable the property was left out entirely.
- Scores are this skill's heuristics, not Google-internal signals.
