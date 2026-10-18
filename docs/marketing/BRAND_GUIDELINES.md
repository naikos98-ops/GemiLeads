# Gemi Leads — Brand Guidelines (campaign edition)

The authoritative design system is **`static/css/product-ui.css`** plus the rendered **Signal Ledger** screens
(see `AGENTS.md` → «Το ΕΓΚΥΡΟ design system»). This file translates it for marketing work: landing pages, ads,
video, social. When this file and the CSS disagree, the CSS wins.

## 1. Positioning

- **One line:** «Βρες τους επόμενους πελάτες σου τη στιγμή που μπαίνουν στην αγορά.»
- **What we are:** a precise, factual register of newly published GEMI registrations, filtered by criteria the
  customer sets (ΚΑΔ, νομός, νομική μορφή).
- **What we are not:** a lead-scoring AI, a data-enrichment broker, an outreach tool, an official GEMI service.
- **Tone:** calm, exact, ledger-like. Second person singular (εσύ), short sentences, no exclamation marks, no
  hype words (μοναδικό, επαναστατικό, εγγυημένα, έκρηξη πωλήσεων).

## 2. Proof rules

1. Only capabilities marked VERIFIED in `PRODUCT_AUDIT.md`.
2. Numbers only if they trace to a source: **9.651** ΚΑΔ codes, **09:00** daily digest, **3 hours / 08:00–23:00**
   Priority Alerts (Enterprise/Custom), Radar limits **0/5/10/15/15**, prices from /pricing/. No customer counts,
   no "X leads per day", no conversion claims, no testimonials until real, consented ones exist.
3. Every sample company is fictional and labelled («ΕΙΚΟΝΙΚΟ ΔΕΙΓΜΑ» / «Δείγμα παρουσίασης»).
4. Always state the limit next to the promise: a new registration is a public event, not a need or intent.

## 3. Colour tokens

| Token | Hex | Use |
|---|---|---|
| `--product-ink` | `#12201e` | text, 1px rules, primary button |
| `--product-paper` | `#f3f2ec` | page canvas |
| `--product-white` | `#fbfbf7` | raised sheets, featured rows |
| `--product-amber` | `#e6a029` | rules/strokes only: match stroke, selected state |
| amber text | `#775215` | amber **text** (the token gives 2.15:1 as text) |
| `--product-green` | `#295c4c` | "on" states, ✓, contact links |
| `--product-muted` | `#66716e` | secondary copy, labels |
| `--product-line` | `#cbd0cb` | inner dividers |
| `--product-red` | `#99443b` | disabled / compliance-off only |

**Selected / featured** is always `box-shadow: inset 3px 0 var(--product-amber)` — never a tint, pill or shadow.

## 4. Typography

Inter (400–800) for copy; `ui-monospace` for labels, codes, dates, eyebrows (uppercase, letter-spacing .08–.12em).
Scale tokens `--type-hero` 44px … `--type-meta` 12px (see top of `product-ui.css`). Headlines: tight tracking
(−.02 to −.035em), max ~20ch.

## 5. Components to reuse (public pages)

| Need | Primitive |
|---|---|
| Hero | `.public-hero`, `.public-hero-copy`, `.public-hero-cta` |
| Factual strip | `.public-meta` (3 cells, no live metrics) |
| Product proof | `.public-intake` + `.product-signal` rows (the real Signals row) |
| Process | `.public-chain` (amber dash, mono label, title, sentence) |
| Facts / FAQ / mapping | `.public-list` |
| Radar example | `.public-radar-sample` + `.product-radar-row` + `.product-criteria` |
| Does / does-not | `.public-fit` |
| Truth notice | `.public-notice` (amber left rule) |
| Buttons | `.product-ink-button` (primary, one per view), `.product-text-button` (secondary) |

**Never:** `rounded-card`, `shadow-soft`/`shadow-glow`, blue CTAs (`bg-signal`), rounded-full buttons, gradient
heroes, stock photos, generic stat cards, emoji in headings, fake testimonials.

## 6. Logo & assets

`static/images/gemi-leads-logo-horizontal.png` (25px high in topbars), `gemi-leads-icon.png`,
`gemi-leads-og.png` (1200×630), email logo `gemi-leads-logo-email.png`. Don't redraw the wordmark in text next to it.

## 7. Ads & video

- Show the real UI (Signals register, Radar row, Dossier) with **fictional** seed data.
- First 3 seconds: audience + outcome («Προμηθευτής HoReCa; Δες ποια νέα καταστήματα εστίασης συστήθηκαν
  στην περιοχή σου.»).
- End card: logo, URL `gemileads.gr/for/<audience>`, «Ξεκίνα δωρεάν».
- Legal line where relevant: «Ανεξάρτητο προϊόν της NORVA Ι.Κ.Ε. — δεν αποτελεί υπηρεσία του ΓΕΜΗ.»
