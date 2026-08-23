# GEO / AEO / AI Search Visibility Audit — gemileads.gr

**Date:** 2026-08-22 · **Scope:** 5 public pages (complete indexable surface)
**GEO Readiness Score: 31/100**

Nothing was modified.

> **Framing (per Google's primary source).** Google's AI optimization guide states that optimizing
> for generative AI search *is still SEO*; "AEO" and "GEO" are rebranded labels for the same work.
> No AI-specific files, markup, chunking or rewrites are required. Findings below are therefore
> framed as SEO fundamentals applied to AI-search surfaces, not a separate discipline.

---

## Executive Summary

This site has an **unusually clean technical foundation for AI search and almost nothing worth
citing**. That combination is favourable: the hard, expensive part is already done.

| Layer | State |
|---|---|
| **Technical accessibility** | **Excellent (95/100)** — all AI crawlers allowed, full SSR, semantic HTML |
| **Content citability** | **Critical (8/100)** — zero extractable passages, zero definitions |
| **Entity clarity** | **Critical (5/100)** — no schema, no legal identity, no off-site presence |
| **Authority signals** | **Critical (5/100)** — no author, no dates, no sources, no brand mentions |

**The bottleneck is content, not infrastructure.** No technical work will improve AI citation here.

---

## Score Breakdown

| Criterion | Weight | Score | Weighted |
|---|---:|---:|---:|
| Citability | 25% | 8 | 2.0 |
| Structural Readability | 20% | 45 | 9.0 |
| Multi-Modal Content | 15% | 10 | 1.5 |
| Authority & Brand Signals | 20% | 5 | 1.0 |
| Technical Accessibility | 20% | 88 | 17.6 |
| **Total** | | | **31.1 → 31** |

---

## 1 · Platform Breakdown

| Platform | Score | Why |
|---|---:|---|
| **Google AI Overviews** | 25 | Strongly ranking-correlated. Site has 4 indexable pages, no sitemap live, one orphaned commercial page — little to rank, so little to cite. |
| **Google AI Mode** | 20 | Draws from a broader pool where **freshness and entity authority** outweigh position. Site has zero dates and zero entity markup — the two things this surface weights most. |
| **ChatGPT** | 15 | Cites Wikipedia (~48%) and Reddit (~11%) heavily. **Zero presence on either.** Crawler access is fine; the entity simply isn't known. |
| **Perplexity** | 15 | Reddit-dominant (~47%). No community footprint. |
| **Claude** | 30 | ClaudeBot has access and content is fully server-rendered, so it *can* read the site — but there is no substantive passage to quote. |
| **Bing Copilot** | 25 | Bingbot 200. No IndexNow. |

> Treat AI Overviews and AI Mode as **two distinct citation engines**. They agree on answers ~86% of
> the time but cite the same URLs only ~13.7% of the time. Ranking well feeds AI Overviews; AI Mode
> rewards freshness and entity authority independently of position.

---

## 2 · AI Crawler Access — **PASS, no action needed**

All verified live, 2026-08-22:

| Crawler | Status | | Crawler | Status |
|---|---|---|---|---|
| GPTBot | **200** | | Google-Extended | **200** |
| OAI-SearchBot | **200** | | CCBot | **200** |
| ChatGPT-User | **200** | | Bytespider | **200** |
| ClaudeBot | **200** | | Googlebot | **200** |
| anthropic-ai | **200** | | Bingbot | **200** |
| PerplexityBot | **200** | | | |

No robots.txt exists, so everything is allowed by default.

**Verified: the pending `robots.txt` in commit `446a28f` does not restrict AI crawlers.** I rendered
it directly — it uses `User-agent: *` and disallows only authenticated paths (`/dashboard/`,
`/superadmin/`, `/api/`, `/leads/`, …). All public pages remain crawlable by every AI bot. Deploying
it is safe from a GEO standpoint.

*(The `Sitemap: http://` seen when rendering locally is a DEBUG-only artefact — with
`SECURE_PROXY_SSL_HEADER` set under `if not DEBUG`, production correctly emits `https://`. Verified.)*

---

## 3 · Server-Side Rendering — **PASS, the single strongest asset**

AI crawlers do **not** execute JavaScript. Fetched as `User-Agent: GPTBot`:

```
words visible without JS : 321   (100% of page content)
<h1> present             : yes
semantic landmarks       : main=1 nav=1 footer=1 article=3 section=5
```

Full parity — nothing is hidden behind JS. Combined with §2, the entire technical prerequisite for AI
citation is already satisfied.

---

## 4 · Passage-Level Citability — **CRITICAL, 0 citable passages**

Optimal AI-citation passage length is **134–167 words**, and ~44% of citations come from the first
30% of a page. I segmented the homepage by heading:

```
passages under headings : 14
in optimal 134-167w band: 0        <-- none
longest passage         : 39 words
```

Full distribution:

| Heading | Words | |
|---|---:|---|
| (pre-heading intro) | 25 | too short |
| Οι νέες επιχειρήσεις που μπορούν… | 39 | too short — **longest on the page** |
| ΠΑΠΑΔΟΠΟΥΛΟΣ ΚΩΝΣΤΑΝΤΙΝΟΣ | 4 | demo record as H4 |
| ΒΛΑΧΟΣ ΙΩΑΝΝΗΣ ΚΑΙ ΣΙΑ ΟΕ | 4 | demo record as H4 |
| ΓΕΩΡΓΙΟΥ ΝΙΚΟΛΑΟΣ | 4 | demo record as H4 |
| GEMILEADS IKE | 20 | demo record as H4 |
| Πώς λειτουργεί | 22 | too short |
| Επίλεξε ΚΑΔ και περιοχή | 19 | too short |
| Λάβε καθημερινό email | 23 | too short |
| Κατέβασε ή στείλε στο CRM | 9 | too short |
| Καθημερινό digest | 16 | too short |
| Φίλτρα ανά ΚΑΔ & περιοχή | 12 | too short |
| CSV exports | 23 | too short |
| Δημιούργησε λογαριασμό… | 28 | too short |

**There is no block of text on this site that an answer engine could lift as an answer.**

**Compounding issue:** four of the fourteen "headings" are **demo record names** marked up as `<h4>`
(`templates/home.html`). They appear *above* the first `<h2>`, so the outline reads
`H1 → H4 → H4 → H4 → H4 → H2`. To a machine parsing document structure, four fourth-level subsections
exist before any second-level section — actively degrading passage segmentation.

---

## 5 · Definitions, Facts & Attribution — **CRITICAL**

| Signal | Count | Impact |
|---|---:|---|
| Definition-pattern sentences (`X είναι…`, `αναφέρεται σε…`) | **0** | LLMs extract these preferentially |
| Question-form headings (`Τι είναι…`, `Πώς…;`) | **0** | Matches query patterns |
| Numeric claims | 2 | `100%`, `9.744 ΚΑΔ` |
| Link to primary source (ΓΕΜΗ/businessportal) | **0** | Site depends on ΓΕΜΗ but never links it |
| Publication / updated dates | **0** | Content <3 months old is ~3× more likely to be cited |
| Author / byline | **0** | |
| Comparison content | **0** | |
| Use-case language (λογιστές, ασφαλιστές, B2B) | **0** | |

### ⚠ The one citable statistic is wrong

Homepage states **"9.744 ΚΑΔ 2025"**. Verified against source:

```
homepage claim        : 9.744
kad_2025.json entries : 9,651
ActivityCode rows     : 10,463
```

The figure matches **neither** the catalogue file nor the live database. This is the site's most
quotable fact, and an AI engine citing it would **propagate an incorrect figure attributed to your
brand**. Fixing accuracy matters more than adding volume.

**Missed opportunity:** the site never links to `publicity.businessportal.gr`, the official source it
is built on. Citing your own primary source is a standard authority signal — and here it is free.

---

## 6 · Entity Clarity — **CRITICAL**

| Signal | State |
|---|---|
| Structured data (JSON-LD / Microdata / RDFa) | **0 across all 5 pages** |
| Organization entity | Undefined |
| `sameAs` profiles | **0** social links found |
| Wikipedia / Wikidata | Absent |
| Reddit / YouTube / LinkedIn | Absent |
| Legal identity on site | Only inside a **fake demo record** (see below) |

**Brand mentions correlate ~3× more strongly with AI citation than backlinks** (Ahrefs, 75k brands;
YouTube mentions strongest at ~0.737 vs Domain Rating ~0.266). This site has no off-site footprint at
all, which is the primary reason ChatGPT and Perplexity score lowest.

**Accuracy caution:** the only "GEMILEADS IKE" string on the site sits inside the *Ενδεικτική
απεικόνιση* demo feed (`templates/home.html:132`, shown as "ΙΚΕ · ΠΑΤΡΑ"). It is **not** a legal
declaration, and must not be encoded as `legalName` or used to infer an address.

---

## 7 · Pages Hardest for an LLM to Understand

Ranked by interpretive difficulty:

| Page | Difficulty | Why |
|---|---|---|
| **`/pricing/`** | **Highest** | 205 words. H1 is the slogan *"Η δύναμη των δεδομένων."* — carries no entity or intent signal. Tier features are icon-prefixed fragments (`⚡ 15 Ενεργά Ραντάρ`) not sentences. **Contains two factually false claims** (below). Orphaned — zero internal links, so entity relationships cannot be inferred from context. |
| **`/`** | High | Longest passage 39 words. Four demo-record H4s break the outline. Product described in feature fragments, never defined. Never states what a lead contains. |
| **`/signup/` `/login/`** | N/A | Utility pages, correctly thin. **No action** — should not be optimised for citation. |

### `/pricing/` states two things the product does not do

Verified against source. Schema and AI citation both restate on-page content, so these errors would
propagate:

| Live claim | Reality | Source |
|---|---|---|
| Business: "Daily & **Weekly** Digest" | Weekly digest **removed** — raises `ValueError("Το εβδομαδιαίο digest έχει καταργηθεί.")` | `gemiapp/services.py:363` |
| Enterprise: "08:00 - **00:00**" | Window is **08:00–23:00** | `gemiapp/tasks.py:51`, `gemiapp/apps.py:22` |

---

## 8 · Top 5 Highest-Impact Changes

Ordered by expected impact ÷ effort.

### 1 · Fix the three factual errors — **P0, Easy**
`templates/pricing.html`, `templates/home.html`. Remove "Weekly Digest", correct 00:00 → 23:00,
correct the ΚΑΔ count.
**First principle:** an inaccurate citation is worse than no citation — it attaches a falsehood to your brand.
**Fails if:** a customer buys Business expecting a weekly digest the code refuses to send.

### 2 · Add three self-contained 134–167 word answer blocks — **P0, Medium**
`templates/home.html`. Each opens with a definition and answers one question:
- *"Τι είναι το ΓΕΜΗ;"* — Το ΓΕΜΗ (Γενικό Εμπορικό Μητρώο) είναι…
- *"Τι περιλαμβάνει κάθε lead;"* — name the real 9 CSV fields, **including ΑΦΜ, email και website**
- *"Πόσο συχνά ενημερώνονται τα δεδομένα;"* — daily 09:00; Enterprise every 3h, 08:00–23:00

**Why this is the single highest-leverage content change:** it simultaneously creates the first
citable passages, the first definitions, the first question-form headings, and states the product's
core value — which is currently invisible (`αφμ`, `επωνυμ`, `διεύθυνση` all score **0** occurrences).

**Fails if:** word count rises but no passage stands alone without surrounding context.
**Leading indicator:** the page begins appearing in AI Overviews for "τι είναι το ΓΕΜΗ"-type queries.

### 3 · Add Organization + WebSite + SoftwareApplication JSON-LD — **P1, Easy**
`templates/base.html` line 30 (before `{% block head %}`); `templates/pricing.html` needs a new
`{% block head %}`. Draft markup ready in `generated-schema.json`.
**Depends on:** #1 (do not mark up false claims). Enrich with `legalName`/`address`/`vatID` only once
a real contact page exists.

### 4 · Demote the four demo-record `<h4>` elements — **P1, Easy**
`templates/home.html`. Convert to `<p>`/`<span>`, restoring a monotonic outline.
**Bonus:** also removes named natural persons (ΑΤΟΜΙΚΗ sole traders) from marketing decoration —
a GDPR consideration. Replace with obviously-fictional names.

### 5 · Add author, dates and a link to the primary source — **P1, Easy**
An `/about/` page plus published/updated dates. Link `publicity.businessportal.gr` from the homepage
data claim.
**Why:** freshness is one of the strongest AI Mode signals, and content stale 6+ months loses citation
eligibility. Zero dates currently exist.

---

## 9 · Schema Recommendations for AI Discoverability

Full detail and validated JSON-LD in `SCHEMA-REPORT.md` / `generated-schema.json`.

| Type | Verdict |
|---|---|
| Organization + WebSite | **Add** — site-wide, `templates/base.html:30` |
| SoftwareApplication + Offer | **Add** — `/pricing/`, after fixing the false claims |
| WebPage | Optional |
| **FAQPage** | **Do NOT add.** Google retired FAQ rich results for **all** sites on 2026-05-07. There is no confirmed AI-citation benefit. Well-structured on-page Q&A (#2) delivers the value without the markup. |
| LocalBusiness / Product / Service / BreadcrumbList | Not applicable — verified in `SCHEMA-REPORT.md` |
| Article / Person | Only once real articles with named authors exist |

---

## 10 · llms.txt & RSL — deliberately NOT prioritised

`/llms.txt`, `/llms-full.txt`, `/.well-known/rsl.xml` → all **404**.

**Google explicitly states you do not need `llms.txt`**, that Google Search ignores it, and that it
"won't harm (nor help) your visibility or rankings." Mueller called the discovery use case "a dead
end." Independent studies (SE Ranking 300k domains; OtterlyAI server logs) found no citation lift.

**Recommendation: skip it.** It is cheap, but recommending it would imply a benefit the evidence does
not support. Every item in §8 outranks it. Revisit only if a non-Google engine documents real use.

---

## Priority Roadmap

| Priority | Action | Effort | Blocks |
|---|---|---|---|
| **P0** | Fix 3 factual errors (#1) | Easy | #3 |
| **P0** | Three 134–167w answer blocks (#2) | Medium | — |
| **P1** | Organization + SoftwareApplication schema (#3) | Easy | needs #1 |
| **P1** | Demote demo-record H4s (#4) | Easy | — |
| **P1** | Author, dates, primary-source link (#5) | Easy | — |
| **P2** | Link `/pricing/` internally (currently orphaned) | Easy | — |
| **P2** | Off-site entity presence: LinkedIn, then Reddit/YouTube | Hard | — |
| **P3** | IndexNow (Bing Copilot only) | Easy | — |
| **Skip** | llms.txt, RSL | — | no evidence of benefit |

---

## Methodology & Limits

- All crawler access, SSR and passage measurements taken live 2026-08-22; content claims cross-checked
  against the working tree at `446a28f`.
- **No AI-citation, ranking, traffic or brand-mention volume data was measured.** No tool here queries
  ChatGPT/Perplexity citation share. Platform scores are **heuristic estimates** from observable
  on-page and crawler signals — not measured visibility.
- Third-party statistics (Ahrefs, SE Ranking, SparkToro) are cited as published industry findings, not
  as facts about this site.
- Scores are this skill's own model, not Google-internal signals. Search Console is the first-party source.
