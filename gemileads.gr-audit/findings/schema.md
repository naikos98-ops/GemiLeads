# Structured Data — Findings & Draft Markup

**Current state (verified 2026-08-22):** `application/ld+json` blocks on public pages = **0**.
No Organization, WebSite, SoftwareApplication, or Breadcrumb markup exists.

---

## What to add

### 1 · Organization + WebSite (site-wide, in `templates/base.html`)

**Blocked by C1** — the placeholders below must be filled with real registered data. Do not ship
this with invented values; contradicting your own footer is worse than having no markup.

```html
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@graph": [
    {
      "@type": "Organization",
      "@id": "https://gemileads.gr/#organization",
      "name": "Gemi Leads",
      "legalName": "GEMILEADS IKE",
      "url": "https://gemileads.gr/",
      "logo": {
        "@type": "ImageObject",
        "url": "https://gemileads.gr/static/images/logo.png",
        "width": 628,
        "height": 174
      },
      "email": "info@gemileads.gr",
      "vatID": "EL__________",
      "identifier": {
        "@type": "PropertyValue",
        "propertyID": "ΓΕΜΗ",
        "value": "__________"
      },
      "address": {
        "@type": "PostalAddress",
        "streetAddress": "__________",
        "addressLocality": "__________",
        "postalCode": "__________",
        "addressCountry": "GR"
      },
      "contactPoint": {
        "@type": "ContactPoint",
        "contactType": "customer support",
        "email": "info@gemileads.gr",
        "availableLanguage": ["el"]
      }
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

**Note on `SearchAction` / sitelinks searchbox:** deliberately omitted. The site has no public search
endpoint — company search is behind `@login_required`. Declaring one that crawlers cannot use would be
inaccurate markup.

---

### 2 · SoftwareApplication with offers (on `/pricing/`)

Prices verified against `gemiapp/superadmin/services.py::PLAN_PRICES` and `templates/pricing.html`:
Pro €19, Business €49, Enterprise €99 per month.

```html
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "SoftwareApplication",
  "name": "Gemi Leads",
  "applicationCategory": "BusinessApplication",
  "operatingSystem": "Web",
  "url": "https://gemileads.gr/",
  "provider": { "@id": "https://gemileads.gr/#organization" },
  "description": "Παρακολούθηση νέων επιχειρήσεων από το επίσημο Open Data API του ΓΕΜΗ, με φίλτρα ΚΑΔ και περιοχής, Ραντάρ Πελατών και email ειδοποιήσεις.",
  "inLanguage": "el",
  "offers": [
    { "@type": "Offer", "name": "Pro",        "price": "19", "priceCurrency": "EUR",
      "category": "subscription", "url": "https://gemileads.gr/pricing/" },
    { "@type": "Offer", "name": "Business",   "price": "49", "priceCurrency": "EUR",
      "category": "subscription", "url": "https://gemileads.gr/pricing/" },
    { "@type": "Offer", "name": "Enterprise", "price": "99", "priceCurrency": "EUR",
      "category": "subscription", "url": "https://gemileads.gr/pricing/" }
  ]
}
</script>
```

**Do not add `aggregateRating` or `review`** unless you have genuine, verifiable customer reviews.
Fabricated review markup is a manual-action risk, not merely an ineffective tactic.

---

## What NOT to add

| Type | Reason |
|---|---|
| **FAQPage** | Google retired FAQ rich results for **all** sites on 2026-05-07. There is no SERP feature to win, and no confirmed AI-citation benefit. If genuine user Q&A exists later, use `QAPage`. |
| **HowTo** | Deprecated by Google in September 2023. |
| **BreadcrumbList** | The site is flat — 4 public pages, no hierarchy. Breadcrumbs would describe a structure that does not exist. Revisit only if Phase 3 content creates real nesting (e.g. `/blog/kad/...`). |
| **AggregateRating** | No real reviews exist. |
| **LocalBusiness** | This is a SaaS product, not a premises-based local business. `Organization` is the correct type. |

---

## Validation

After adding, verify each page with the Rich Results Test and the Schema Markup Validator.

**How this fails:** required properties missing, or the `@id` references between `Organization`,
`WebSite`, and `SoftwareApplication` not resolving — which silently breaks the entity graph even
though each block validates in isolation.

**Leading indicator:** Search Console → Enhancements begins reporting the Organization entity.
