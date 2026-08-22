# Content SEO Analysis — gemileads.gr

**Date:** 2026-08-22 · **Scope:** 4 public pages (complete indexable surface, not a sample)
**Content Quality Score: 34/100** · **AI Citation Readiness: 22/100**

Nothing was modified. All findings verified against the live site and the repository.

---

## E-E-A-T Breakdown

> Weights are **this skill's own scoring model**, ordered to reflect Google's stated hierarchy
> (Trust is most important). Google publishes no numeric E-E-A-T weights. These are heuristics,
> not Google-internal signals.

| Factor | Score | Key signals |
|---|---|---|
| **Experience** | 4/20 | No case studies, no original research, no worked example, no screenshots of real output. The one "Live Feed" is explicitly labelled *Ενδεικτική απεικόνιση*. |
| **Expertise** | 6/25 | No author, no team, no "about" page. Domain expertise (ΓΕΜΗ/ΚΑΔ data engineering) is real but entirely undocumented. |
| **Authoritativeness** | 5/25 | No external citations, no press, no customer logos, no testimonials. Not even a link to the official ΓΕΜΗ/Business Portal source it depends on. |
| **Trustworthiness** | 8/30 | HTTPS ✓. Honest disclaimer ✓ ("δεν αποτελεί υπηρεσία του ΓΕΜΗ"). But **no contact, no ΑΦΜ, no address, no Terms, no Privacy, no refund policy** on a site charging €19–99/month. |

### Google's Who / How / Why test

| Question | Answer on this site | Verdict |
|---|---|---|
| **Who** created it? | Nothing beyond "GEMILEADS IKE" in a demo row | **Fail** |
| **How** was it made? | Data provenance stated ("επίσημο Open Data API ΓΕΜΗ") — the one strong answer | **Partial pass** |
| **Why** does it exist? | Clearly to sell a genuine service, not to farm clicks | **Pass** |

---

# 1 · CURRENT CONTENT WEAKNESSES

## W1 · Four factual contradictions between marketing copy and shipped product `[P0]`
**Confidence:** Verified against source code. This is the most serious finding in this report:
the site is making claims to paying customers that the product does not honour.

| # | Live claim | Reality | Source of truth |
|---|---|---|---|
| 1 | Business tier: **"Daily & Weekly Digest"** | Weekly digest was **removed**. `send_digests` raises `ValueError("Το εβδομαδιαίο digest έχει καταργηθεί.")` | `gemiapp/services.py:363` |
| 2 | Enterprise: **"Ενημέρωση ανά 3 ώρες (08:00 - 00:00)"** | Window is **08:00–23:00**. Cron fires at 8,11,14,17,20,23; guard is `8 <= hour <= 23` | `gemiapp/tasks.py:51`, `gemiapp/apps.py:22` |
| 3 | Homepage: **"9.744 ΚΑΔ 2025"** and **"08:00 Πρωινό Digest"** | Catalogue holds **10,463** codes; daily cron runs at **09:00** | `ActivityCode` count; `gemiapp/apps.py:17` |
| 4 | Homepage: **"στο inbox σου κάθε πρωί"** at 08:00 | Daily digest cron is `0 9 * * *` | `gemiapp/apps.py:17` |

**Why it matters:** #1 is a **paid feature advertised on the pricing page that no longer exists** — a
consumer-protection and chargeback exposure, not merely an SEO issue. #2–#4 are accuracy defects
that raters and users can verify, directly damaging Trustworthiness (the heaviest E-E-A-T factor).

**Files:** `templates/pricing.html`, `templates/home.html`

## W2 · The core value proposition is never stated `[P0]`
**Confidence:** Verified by term-frequency analysis of the live page text.

The product exports **9 fields per lead** — Ημερομηνία, Αρ. ΓΕΜΗ, **ΑΦΜ**, Επωνυμία, Νομική μορφή,
Νομός, Πόλη, **Email**, **Website** (`gemiapp/views.py::export_csv`).

Occurrences in live homepage copy:

```
αφμ           0        επωνυμ        0
διεύθυνση     0        νομική μορφή  0
τηλέφων       0        email         1
```

A prospect cannot learn **what they actually receive**. "Email και website της επιχείρησης" is the
single strongest B2B selling point in this product and it is invisible. This is simultaneously the
biggest conversion gap and the biggest semantic-relevance gap.

## W3 · Thin content on both commercial pages `[P1]`
| Page | Words | Floor for type | Gap |
|---|---:|---:|---|
| `/` | 321 | 500 (homepage) | −179 |
| `/pricing/` | 205 | 800 (service page) | −595 |
| `/signup/` | 83 | — | Normal for auth |
| `/login/` | 67 | — | Normal for auth |

Word count is **not** a ranking factor. The issue is **topical coverage**: neither page answers the
questions a buyer actually has (what data, how fresh, what legal basis, can I cancel, is there a trial).

## W4 · Weak H1 on the highest-intent page `[P1]`
`/pricing/` H1 is **"Η δύναμη των δεδομένων."** — a slogan with zero keyword or intent signal on the
site's primary commercial page. Compare the homepage H1, which is strong and specific.

**Fix:** e.g. *"Πλάνα και τιμές Gemi Leads — από €19/μήνα"*.

## W5 · No meta descriptions live `[P1]`
Absent on all 4 pages. Fixed in unpushed `446a28f`, but note that generic descriptions there should be
made page-specific for `/pricing/`. Affects CTR, not ranking.

## W6 · Zero informational content — no topical authority `[P1]`
4 public pages, no blog, no glossary, no guides. Nothing explains ΓΕΜΗ, ΚΑΔ, or company-registration
data. The site can only compete on brand and direct commercial queries.

## W7 · Named individuals in demo data `[P2]`
Homepage "Live Feed" shows *ΠΑΠΑΔΟΠΟΥΛΟΣ ΚΩΝΣΤΑΝΤΙΝΟΣ*, *ΒΛΑΧΟΣ ΙΩΑΝΝΗΣ*, *ΓΕΩΡΓΙΟΥ ΝΙΚΟΛΑΟΣ* —
sole traders (ΑΤΟΜΙΚΗ), i.e. **natural persons**, with locations and dates.

*Ενδεικτική απεικόνιση* is present and honest. But if these are real ΓΕΜΗ records, publishing named
individuals as marketing decoration is a GDPR consideration independent of SEO. If they are invented,
they are fabricated records presented as product output.

**Recommendation:** replace with obviously-fictional company names (e.g. "ΠΑΡΑΔΕΙΓΜΑ ΙΚΕ"). Low
effort, removes the ambiguity entirely.

## W8 · No page should be de-indexed — but 4 currently lack noindex `[P2]`
`/signup/`, `/login/`, `/password_reset/`, `/resend-verification/` are thin utility pages.
`446a28f` correctly adds `noindex` to the reset/resend pages and keeps signup/login indexable
(correct — they serve navigational brand queries). **No change needed beyond deploying.**

## W9 · No cannibalisation `[PASS]`
Verified — each page targets a distinct intent with no title/H1 overlap:

```
/          Gemi Leads — Οι νέες επιχειρήσεις στο inbox σου κάθε πρωί
/pricing/  Συνδρομές & Πλάνα | Gemi Leads
/signup/   Δημιουργία λογαριασμού · Gemi Leads
/login/    Σύνδεση · Gemi Leads
```

---

# 2 · COMMERCIAL OPPORTUNITIES

| # | Opportunity | Intent | Effort | Why |
|---|---|---|---|---|
| C1 | **Fix the 4 factual contradictions** | — | Easy | Trust floor; a live paid-feature claim is false |
| C2 | **Data-fields section on `/`** — "Τι περιλαμβάνει κάθε lead" with the real 9 columns | Commercial investigation | Easy | Converts W2 into the strongest selling point |
| C3 | **Expand `/pricing/`** — comparison table, cancellation terms, what "Ραντάρ" means, FAQ | Transactional | Medium | 205 words is far too thin for the buying decision |
| C4 | **Use-case landing pages** (see §5) | Commercial | Medium | Captures segment-specific intent |
| C5 | **Rewrite pricing H1** | Transactional | Easy | Slogan → intent-matched heading |

---

# 3 · INFORMATIONAL OPPORTUNITIES

> **Gate before investing.** I have **no keyword volume, ranking, or competition data** and invented
> none. Every topic below is a *hypothesis grounded in the product's actual domain*, not a validated
> demand signal. Confirm in Search Console (once the sitemap ships) or a keyword tool before writing.
> A genuine possible outcome is that Greek search demand here is too thin to justify the work — in
> which case skip §6 entirely and invest in direct sales.

Ranked by *proximity to purchase*, not assumed volume:

| # | Topic | Intent | Rationale |
|---|---|---|---|
| I1 | Τι είναι το ΓΕΜΗ και τι δεδομένα δημοσιεύει | Informational | Defines the core entity; highest citability |
| I2 | Πώς λειτουργεί το Open Data API του ΓΕΜΗ | Informational/technical | Product's own foundation; demonstrates expertise |
| I3 | ΚΑΔ 2025: τι είναι και πώς διαβάζεται ο κωδικός | Informational | Directly gates product use |
| I4 | Πώς βρίσκω νέες επιχειρήσεις για B2B πωλήσεις | Commercial investigation | Closest to purchase |
| I5 | Είναι νόμιμο να επικοινωνώ με νέες επιχειρήσεις; (GDPR + ΓΕΜΗ) | Informational | **Highest-trust topic**; addresses the unspoken objection |

I5 deserves emphasis: it is the objection most likely to block a sale, it is legally substantive, and
it simultaneously fixes an E-E-A-T gap.

---

# 4 · CONTENT CLUSTERS

```
HUB A — ΓΕΜΗ & ανοικτά δεδομένα        HUB B — B2B lead generation στην Ελλάδα
├── I1 Τι είναι το ΓΕΜΗ                ├── I4 Πώς βρίσκω νέες επιχειρήσεις
├── I2 Open Data API                   ├── I5 GDPR & ψυχρή επικοινωνία
└── I3 ΚΑΔ 2025                        └── Use-case pages (§5)
        │                                       │
        └────────► /pricing/ ◄─────────────────┘
```

Two hubs, both converging on `/pricing/`. Hub A builds entity authority; Hub B captures buying intent.

**Quality gate — binding.** The ΚΑΔ catalogue holds **9,651 entries** (`gemiapp/data/kad_2025.json`).
Generating one page per code is **programmatic SEO** and would produce ~9,651 near-identical stubs —
a reliable trigger for quality suppression. If pursued: enforce ≥60% unique content per page, start
with **20 curated sector pages**, and prove they earn impressions before scaling. Do not auto-generate
the long tail.

---

# 5 · RECOMMENDED LANDING PAGES

| # | URL | Targets | Priority |
|---|---|---|---|
| L1 | `/terms/`, `/privacy/`, `/contact/` | Trust floor + GDPR + Stripe prerequisite | **P0** |
| L2 | `/pricing/` (expand, don't create) | Transactional | **P0** |
| L3 | `/gia-logistes/` (λογιστές & φοροτεχνικοί) | Segment intent | P1 |
| L4 | `/gia-asfalistes/` (ασφαλιστικοί σύμβουλοι) | Segment intent | P2 |
| L5 | `/gia-b2b-poliseis/` (B2B πωλήσεις) | Segment intent | P2 |
| L6 | `/about/` (ποιοι είμαστε) | Expertise/Authority | P1 |

L3 is listed first among segments because accountants are the clearest fit: a newly-registered company
needs an accountant immediately, making Gemi Leads' "day-one registration" data uniquely valuable.
**This remains a hypothesis pending validation.**

**Do NOT create:** comparison/alternatives pages yet. There is no verified competitor set, and
inventing one would fabricate market data.

---

# 6 · RECOMMENDED ARTICLES

Only after the §3 validation gate passes. Sequence deliberately front-loads trust:

1. **Είναι νόμιμο να επικοινωνώ με νέες επιχειρήσεις;** (I5) — removes the purchase objection
2. **Τι είναι το ΓΕΜΗ** (I1) — entity definition, most citable by AI engines
3. **ΚΑΔ 2025: πλήρης οδηγός** (I3) — leverages the existing catalogue asset
4. **Πώς λειτουργεί το Open Data API του ΓΕΜΗ** (I2) — demonstrable first-hand expertise
5. **Πώς βρίσκω νέες επιχειρήσεις για B2B πωλήσεις** (I4) — commercial bridge to `/pricing/`

Each needs: visible author + credentials, publish/updated date, a standalone definitional opening
paragraph (for AI extraction), and ≥2 internal links.

---

# 7 · INTERNAL LINKING STRATEGY

## Current state — verified by crawling all public pages

```
200  /                      linked 5x
200  /login/                linked 11x
200  /password_reset/       linked 1x
200  /resend-verification/  linked 1x
200  /signup/               linked 13x
```

**`/pricing/` receives ZERO internal links.** Every `{% url 'pricing' %}` in the repo sits inside a
login-gated template (`dashboard.html`, `settings.html`, `radars/list.html`, `companies/detail.html`),
and the nav link is inside `{% if user.is_authenticated %}` in `templates/base.html`.

The site's highest-value commercial page is an orphan — invisible to crawlers and to logged-out visitors.

## Target structure

| From | To | Anchor | Priority |
|---|---|---|---|
| Public nav (`base.html`) | `/pricing/` | Τιμές | **P0** |
| Footer (`base.html`) | `/pricing/`, `/terms/`, `/privacy/`, `/contact/` | descriptive | **P0** |
| `/` pricing CTA section | `/pricing/` | Δες τα πλάνα | **P0** |
| Every article | `/pricing/` + 1 sibling article | contextual | P2 |
| Hub pages | their spokes, bidirectional | descriptive | P2 |

**Rules:** descriptive anchors (never "κάντε κλικ εδώ"); 3–5 internal links per 1,000 words;
no page more than 2 clicks from `/`.

---

# 8 · PRIORITY ORDER

### P0 — Trust & accuracy floor (Week 1)
1. **Fix the 4 factual contradictions** (W1) — `templates/pricing.html`, `templates/home.html`.
   Remove "Weekly Digest", correct 08:00→09:00 and 00:00→23:00, correct the ΚΑΔ count.
   *Fails if:* a customer buys Business expecting a weekly digest that cannot be sent.
2. **Link `/pricing/` publicly** (§7) — nav + footer + homepage CTA.
   *Verify:* `curl -s https://gemileads.gr | grep -c 'href="/pricing/"'` → ≥1
3. **Add `/terms/`, `/privacy/`, `/contact/`** (L1) with ΑΦΜ, ΓΕΜΗ number, address, support email.
   *Three independent drivers:* QRG trust, GDPR obligation, Stripe prerequisite.

### P1 — Convert existing traffic (Weeks 2–3)
4. **"Τι περιλαμβάνει κάθε lead"** section on `/` (W2) — the real 9 fields, ΑΦΜ/email/website named.
5. **Expand `/pricing/`** to ~800 words (W3) — comparison table, cancellation, FAQ.
6. **Rewrite the pricing H1** (W4).
7. **Add `/about/`** (L6) — fixes the Expertise gap; enables `Organization` schema.
8. **Replace named individuals in demo data** (W7).

### P2 — Authority (Month 2+, gated)
9. **Validate demand first** (§3 gate). Skip 10–11 if it fails.
10. Publish articles 1–3 in the §6 order.
11. Segment landing pages L3–L5.

### P3 — Scale (Month 3+)
12. Complete both hubs; 20 curated ΚΑΔ sector pages **only** if 9–11 earn impressions.

---

## AI Citation Readiness: 22/100

| Signal | State |
|---|---|
| Crawler access | **Pass** — GPTBot, ClaudeBot, PerplexityBot, OAI-SearchBot all 200; full SSR |
| Quotable passages | **Fail** — no definitional statements anywhere |
| Entity definition | **Fail** — zero structured data |
| Heading hierarchy | **Fail** — homepage jumps H1→H4 |
| Original data | **Fail** — no research, statistics or case studies |
| Attribution | **Fail** — no author, no dates |

**The single highest-leverage observation in this report:** technical AI-crawler access is already
*perfect*, and content is the only thing missing. Note per Google's own guidance that AEO/GEO is
rebranded SEO — no AI-specific files, markup or rewrites are needed. Writing genuine explanatory
content (§6) is the entire fix.

---

## Methodology & Limits

- All content extracted from the live site 2026-08-22; claims cross-checked against the working tree.
- **No keyword volumes, rankings, traffic, backlink or competitor data** was available and **none was
  invented**. Every topic recommendation is explicitly gated on validation.
- Scores are **this skill's heuristics**, not Google-internal signals. Search Console is the
  first-party source of truth.
- Scope: 4 public pages = the complete indexable surface.
