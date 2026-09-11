# Forensic audit and visual primitive mapping

Read-only audit of the Gemini visual drift, the recovery, and the mapping from the approved
authenticated Signal Ledger to the public and admin surfaces. No code is changed by this
document.

Branch audited: `recovery/visual-consolidation` @ `343bc24`
Gemini state preserved at: `refs/recovery/gemini-visual-state` (`a9a04a7`) and
`.recovery/gemini-uncommitted.patch` (216 089 bytes)

---

## A. Git history and diff

```
343bc24  Revert "fix(signup): render form-level validation errors"
efb10e0  fix(signup): render form-level validation errors        <- reverted, out of scope
22a2489  fix(ui): restore original copy on the migrated auth/public pages
1afa5cd  feat(ui): finish the Signal Ledger migration across every remaining surface
c3b6af1  docs: review captures and forensic handoff notes
3393e25  feat(ui): extend the Signal Ledger design system to Landing, Pricing and Superadmin
75b6a42  test: align stale assertions with approved Signal Ledger IA
e83301a  Merge branch 'fix/outreach-hard-bounces' into recovery/visual-consolidation
608671c  (main) fix(compliance): cold outreach shutdown, safeguards and migration 0031
e55f8a3  (fix/outreach-hard-bounces) feat: overhaul lead management system with new UI design
e927bbc  common ancestor of both branches
```

Working tree: clean (untracked `.recovery/` scratch and 53 obsolete Gemini intermediate
screenshots only).

### The root cause

The two branches diverged at `e927bbc` and were never reconciled:

| | `main` (Gemini's base) | `fix/outreach-hard-bounces` |
| --- | --- | --- |
| Cold-outreach compliance shutdown | present | present (equivalent) |
| `static/css/product-ui.css` | **0 lines — absent** | 1383 lines |
| Signal Ledger authenticated templates | **absent** | present |
| `seed_demo_marketing_data` | absent | present |

`git show main:static/css/product-ui.css` returns nothing. `grep -rn "product-ui\|product-body"`
over `main` returns zero hits. `main:templates/dashboard.html` is the pre-Signal-Ledger SaaS
screen (`rounded-card`, `shadow-soft`, stat-card grid, `blue-600`, "Καλημέρα, …").

**Gemini was therefore working on a branch where the approved design system did not exist.**
It read `main`'s actual primitives and propagated them. Every prior attempt that "reintroduced
`rounded-card` / `shadow-soft`" was reproducing what `main` genuinely contained. The drift was
structural, not carelessness — which is why instructing an agent to "use the approved system"
could never have worked from that branch.

---

## B. What Gemini changed

Ten files, all uncommitted, `608671c → a9a04a7` (+948 / −1145):

| File | Surface | Lines |
| --- | --- | --- |
| `templates/base.html` | shared chrome (public **and** authenticated) | 93 |
| `templates/home.html` | Landing | 583 |
| `templates/pricing.html` | Pricing | 502 |
| `templates/superadmin/base.html` | Superadmin shell | 207 |
| `templates/superadmin/overview.html` | Superadmin Overview | 242 |
| `templates/superadmin/client_finder/list.html` | Superadmin | 229 |
| `templates/superadmin/outreach_history/list.html` | Superadmin | 125 |
| `templates/superadmin/users/list.html` | Superadmin | 83 |
| `gemiapp/views.py` | `SAMPLE_LEADS` demo data | 18 |
| `AGENTS.md` | handoff notes | 11 |

No CSS file was touched — Gemini worked purely in Tailwind utilities, which is itself the
finding: there was no product CSS on that branch to work from.

### Gemini's visual vocabulary (class census in its own version)

| File | Dominant classes |
| --- | --- |
| `base.html` | `rounded-full` ×6, `font-mono` ×5, `bg-navy-950` ×5, `text-amber-400`, `shadow-soft`, `backdrop-blur` |
| `home.html` | `font-mono` ×27, `rounded-full` ×6, `bg-navy-950` ×3, `bg-emerald-500` ×2 |
| `pricing.html` | `font-mono` ×44, `bg-navy-950` ×11, `rounded-full` ×10 |
| `superadmin/base.html` | `font-mono` ×15, `bg-navy-950` ×6, `rounded-full` ×5, `text-amber-400` ×2, `bg-emerald-500`, `backdrop-blur` |
| `superadmin/overview.html` | `font-mono` ×21, `rounded-full` ×5, `bg-emerald-500` |

Mapped against the prohibited list: `font-mono` at 44 occurrences on a pricing page is
terminal cosplay; `rounded-full` is the pill-CTA language; `bg-navy-950` chrome plus
`text-amber-400` and `bg-emerald-500` is the separate yellow-heavy admin identity;
`backdrop-blur` is glassmorphism. All four are explicitly out of the approved system.

---

## C. Gemini changes worth preserving

1. **`SAMPLE_LEADS` replacement (`gemiapp/views.py`) — preserve.** This is a genuine privacy
   fix, not a visual change. The prior list contained **real natural-person names**
   (ΑΝΔΡΕΟΥ ΜΑΡΙΑ, ΓΑΛΑΝΟΥ ΕΛΕΝΗ, ΚΩΝΣΤΑΝΤΙΝΙΔΗΣ ΓΕΩΡΓΙΟΣ) and real-looking domains
   (`cloudworks.io`, `greenenergy.gr`, `neaestiasi.gr`) on the public landing page. Gemini
   replaced them with six synthetic entities on the reserved `.demo` TLD and removed NORVA.
   Verified in the current tree: all four approved fictional companies present, `NORVA` count 0,
   real-person-name count 0.

2. **Pricing claim corrections — preserve.** Gemini removed "API Webhooks" and "dedicated
   database sync" from the Custom plan after checking the backend. Both are unimplemented.
   Confirmed still absent.

3. **Compliance-archive framing intent — preserve the intent, rebuild the execution.** Gemini
   correctly recognised that outreach history had to be presented as archive rather than a
   Growth feature. Its visual execution used the wrong system.

## D. Gemini changes to remove or rebuild

| Change | Why |
| --- | --- |
| `base.html` nav rebuilt as a "GL" mini-brand tile with `bg-sand-100/95 backdrop-blur-sm` | Separate public brand; glassmorphism; `base.html` is shared with the approved authenticated screens, so this leaked into the product |
| `main` padding `pt-18 → pt-16` | Same shared-chrome leak |
| Landing hero as a dark terminal panel, `font-mono` ×27 | Terminal cosplay; hero proof must derive from the real Signals register |
| Landing invented metrics (`9.651` framed as telemetry, `DAILY SYNC`, `09:00` as a live number) | Metrics must be real and sourced. *(Note: `9.651`, `09:00` and `23:00` **are** real — pinned by tests to `kad_2025.json` length and `apps.SCHEDULES` cron. Gemini's error was removing them as decoration in a later pass, not showing them.)* |
| Pricing as `rounded-full` / card language, `font-mono` ×44 | Pricing must have no identity of its own; it derives from the Radars register |
| Superadmin dark navy sidebar, `SA` tile, `text-amber-400` group headers, `bg-emerald-500` back button | Separate admin identity; the rail must be the product rail |
| Outreach grouped under "Growth" | Must be `COMPLIANCE / ARCHIVE` |
| Send / test-send / queue forms rendered `disabled` | A disabled button still ships a working POST target |
| Deleted code comments explaining the phone dark-theme CTA pinning | Lost rationale for a real bug fix |

---

## E. Approved authenticated screens — intact

Verified `git diff` against the Signal Ledger merge (`e83301a`) and against the working tree:

```
UNCHANGED  templates/dashboard.html          (Signals)
UNCHANGED  templates/radars/list.html        (Radars)
UNCHANGED  templates/radars/detail.html
UNCHANGED  templates/leads/list.html         (Leads)
UNCHANGED  templates/companies/detail.html   (Business Dossier)
UNCHANGED  templates/settings.html           (Settings)
UNCHANGED  templates/includes/kad_picker.html
UNCHANGED  gemiapp/forms.py
```

The authenticated section of `product-ui.css` (everything before the
`PUBLIC & ADMIN SURFACES` banner) is **byte-identical** to `e83301a`. All additions are in
scoped layers below it.

`kad_picker.html` deliberately retains legacy classes (`rounded-control` ×2, `rounded-full` ×3,
`shadow-soft` ×1) because it renders **inside** the approved Signals and Radar-form screens.
Changing it would alter approved UI.

---

## F. Last approved Signal Ledger implementation

`fix/outreach-hard-bounces` @ **`e55f8a3`** — "feat: overhaul lead management system with new
UI design". It introduced `static/css/product-ui.css` (1383 lines) and the `product-*`
authenticated templates. This is the only commit in the repository where the approved system
exists in full, and it is the version now merged into the recovery branch.

Corroborating capture: `docs/marketing-screenshots/marketing_signals_desktop_1788909944920.png`
(02:25) predates Gemini's `base.html` edit (04:00) and shows the approved rail, topbar and
register.

## G. Sources inspected

- `AGENTS.md` — now carries the design-system authority statement (line 46) and the
  non-authoritative legacy list (line 63)
- `docs/gemi-leads-ui-study/`: `README.md`, `current-ui-audit.md`, `design-directions.md`,
  `implementation-notes.md`, `refinement-review.md`, `anti-ai-audit.md`, 123 screenshots
- `docs/compliance/cold-outreach-shutdown.md`, `docs/marketing-demo-data.md`
- `static/css/product-ui.css` (3075 lines) — the governing system
- `refinement-review.md` supplies the canonical **state grammar** table (Signal / Matched /
  New / Viewed / Contacted / Interested / Lead), which the public intake rows reuse verbatim

---

## Tokens — the single source

```css
--product-ink:        #12201e   /* dark green-black structural ink */
--product-paper:      #f3f2ec   /* warm off-white canvas */
--product-white:      #fbfbf7   /* raised/selected surface */
--product-line:       #cbd0cb   /* thin internal rule */
--product-amber:      #e6a029   /* restrained semantic accent */
--product-amber-soft: #f6e5bd
--product-green:      #295c4c   /* eyebrow / positive state */
--product-red:        #99443b   /* negative / compliance-off state */
--product-muted:      #66716e
--product-state-rule: #8e9793
--product-rail-width: 184px
--admin-rail-width:   218px     /* the one derived value: one extra grouping level */
```

Type: Inter for identity and prose; `ui-monospace` **only** for metadata, IDs, dates, state
tags and control labels. Radius `0` everywhere except the 2px BETA chip. Shadow is used for
exactly one purpose — `inset 3px 0 var(--product-amber)` as the selected/active marker.

---

## Visual primitive mapping

Each row gives the approved source primitive with its real declarations, and the counterpart.
No parallel primitives were created: the public and admin layers reuse `.product-*` classes
directly where the grammar is identical, and derive a scoped class only where the layout
differs.

### 1. Signals chronological row → Landing public intake row

| | Value |
| --- | --- |
| Source | `.product-signal` |
| Grid | `58px 4px minmax(220px,1fr) 90px`, gap `14px`, min-height `80px`, padding `10px 18px` |
| Border | `border-bottom: 1px solid var(--product-line)`; no other border |
| Selected | `background: var(--product-white)` + `box-shadow: inset 3px 0 var(--product-amber)` |
| Match marker | `.product-match-stroke` — `height: 48px; background: var(--product-amber)` |
| Type | name 13px/700 ink; meta 9px mono muted; reason 11px `#775215` |
| Radius / shadow | 0 / none (except the inset selection marker) |
| Responsive | ≤460px grid collapses to `38px 3px minmax(0,1fr) 70px`, name ellipsises |
| **Counterpart** | **`.product-signal` reused verbatim**, wrapped in `.public-intake` (`1px solid ink`, paper ground) with a 44px mono header |

The landing hero renders real `.product-signal` rows. The first is `is-new selected`, giving
the amber inset and match stroke — the same treatment a matched signal gets in the product.

### 2. Signals selected inspector → Landing relevance / product proof

| | Value |
| --- | --- |
| Source | `.product-inspect` — `background: var(--product-white)`, padding `22px 24px 26px`, `position: sticky; top: 58px` |
| Reading order | identity → public metadata → EVENT→INFO→MATCH→LEAD chain → match evidence → primary action |
| **Counterpart** | `.public-chain` — the same chain, scaled to five full-width cells divided by `1px solid var(--product-line)`, each opening with a `26×3px` amber rule |

The chain labels are the product's own: `EVENT / INFORMATION / CRITERIA / RELEVANCE / LEAD`.

### 3. Radars definition row → Pricing plan row

| | Value |
| --- | --- |
| Source | `.product-radar-row` — grid `1.1fr 1.5fr .7fr 220px`, gap `24px`, padding `20px 13px`, `border-bottom: 1px solid var(--product-ink)` |
| Header | `.product-radar-register > header` — padding `14px 13px`, mono 8px/600 muted uppercase, ink bottom rule |
| Figure | `26px/700` ink with a 10px muted caption beneath |
| State | `.product-radar-state` mono 8px/700; `.on` → `var(--product-green)` |
| Paused | `opacity: .6` |
| **Counterpart** | `.public-plan-row` — grid `1.05fr 1.5fr .62fr .62fr 205px`, gap `22px`; same header treatment, same `26px/700` figure for price and radar count |
| Featured plan | `.public-plan-row.featured` → `background: var(--product-white)` + `box-shadow: inset 3px 0 var(--product-amber)` — **identical** to the row-selected grammar |

One extra column versus Radars (price *and* radar count both need a figure). Everything else
is the Radars register.

### 4. Leads filter / register → Superadmin Users and operational registers

| | Value |
| --- | --- |
| Source | `.product-ledger-tools` — height `44px`, padding `0 20px`, flex space-between, `border-bottom: 1px solid var(--product-line)`, paper ground; row grammar from `.product-signal` |
| **Counterpart (filters)** | `.admin-filters` — flex wrap, `align-items: flex-end`, gap `10px`, padding `14px 0`, `border-bottom: 1px solid var(--product-line)`; labels mono 8px/600 uppercase muted; controls `1px solid ink`, radius 0, min-height 32px |
| **Counterpart (register)** | `.admin-register` — `border-collapse: collapse`, `border-top: 1px solid var(--product-ink)`; `th` mono 8px/600 uppercase muted with ink bottom rule; `td` 12.5px, `border-bottom: 1px solid var(--product-line)`; row hover → `var(--product-white)` |
| State tags | `.admin-tag` — `1px solid var(--product-line)`, mono 8px/700 uppercase; `.on` green, `.warn` `#775215`/amber, `.off` `#99443b` |

A table is used rather than a CSS grid because admin registers carry 6–8 columns and must
scroll inside `.admin-scroll`; the visual grammar (thin rules, mono headers, no zebra, no card)
is the Leads register.

### 5. Business Dossier sections → Superadmin compliance / detail sections

| | Value |
| --- | --- |
| Source | `.product-record-section` (`margin-top: 24px`) inside `.product-dossier-grid`; public record left, work right |
| **Counterpart (detail)** | `.admin-panel` — `1px solid var(--product-ink)`, white ground, padding `18px 20px`; heading mono 9px/700 uppercase with a `1px solid var(--product-line)` underline |
| **Counterpart (compliance)** | `.admin-compliance` — `1px solid ink` with `border-left: 3px solid #99443b`, carrying `COMPLIANCE STATE`, `COLD OUTREACH: DISABLED`, `ARCHIVE: READ ONLY` as a mono definition list |

The red left rule is the only place `--product-red` is used structurally, and it means exactly
one thing: a disabled/archived subsystem.

### 6. Settings notice / form grammar → Pricing beta and access notices

| | Value |
| --- | --- |
| Source | Settings notice — flat panel, ink border, amber left rule, mono label above prose |
| **Counterpart** | `.public-notice` — padding `14px 18px`, `1px solid var(--product-ink)`, `border-left: 3px solid var(--product-amber)`, white ground; `b` mono 9px/700 uppercase; body 12.5px/1.6 muted |
| Used for | the beta / "πληρωμές δεν είναι ακόμη ενεργές" notice, the complimentary-access notice, and the synthetic-sample disclosure on the landing page |

### 7. Authenticated product rail → Superadmin expanded rail

| | `.product-rail` (source) | `.admin-rail` (counterpart) |
| --- | --- | --- |
| Position | fixed, `top: 58px`, bottom 0 | identical |
| Width | `--product-rail-width: 184px` | `--admin-rail-width: 218px` |
| Border | `border-right: 1px solid var(--product-ink)` | identical |
| Ground | `var(--product-white)` | identical |
| Item | min-height 64px, grid `44px 1fr`, eyebrow + name + descriptor | min-height 34px, single line (12px/600) |
| Grouping | eyebrow per item (`MARKET`/`CRITERIA`/`OUTCOME`/`SYSTEM`) | `.admin-rail-group > p` mono 7px/600 `.13em` uppercase muted |
| **Active** | `background: ink; color: white; box-shadow: inset 3px 0 var(--product-amber)` | **identical declaration** |
| Foot | `.product-source` — `PUBLIC DATA SOURCE / ΓΕΜΗ OPEN DATA` | `.admin-rail-foot` — back to product |

The admin rail trades the per-item descriptor for a group header because it carries 13
destinations against the product's 4. Width grows by 34px for the group labels. The active
state, border, ground and type scale are unchanged.

---

## Shared-primitive consolidation

`product-ui.css` is one file with three scoped layers, so nothing added for a public or admin
surface can reach the approved screens:

| Line | Scope | Surfaces |
| --- | --- | --- |
| 17 | `body.product-body` | Signals, Radars, Leads, Dossier, Settings |
| 1395 | `body.public-body` | Landing, Pricing, auth, legal |
| 1396 | `body.admin-body` | Superadmin |

`base.html` applies `product-body` when authenticated and `public-body` otherwise;
`superadmin/base.html` applies `admin-body`.

Legacy theme keys `rounded-card`, `rounded-chip` and `shadow-glow` were removed from
`tailwind.config.js` after confirming zero references, so a future `rounded-card` now fails to
generate rather than silently restoring the old look. `app.css` was byte-identical after the
removal, confirming they were already unused. `rounded-control`, `rounded-panel` and
`shadow-soft` survive solely for `kad_picker.html` and `app.js`.

---

## Compliance state (unchanged by any visual work)

| Check | Result |
| --- | --- |
| `608671c` ancestor of HEAD | yes |
| `0031_cancel_pending_outreach.py` modified | no (0 files) |
| `OUTREACH_ENABLED` default | `"0"` — fail closed |
| `OUTREACH_DAILY_SEND_CAP` default | `0` |
| `outreach_enabled()` guards | 3 call sites |
| send / test / queue POST targets in templates | **0** (was 2, rendered `disabled`) |
| `COMPLIANCE / ARCHIVE` + `COLD OUTREACH: DISABLED` + `ARCHIVE: READ ONLY` | present on overview, outreach history, client finder |

Verification, password-reset and user-configured Radar/Digest email paths are untouched: no
Python outside `tests.py` and the `SAMPLE_LEADS` constant differs from the merge.

---

## Verification at time of audit

```
manage.py check              System check identified no issues (0 silenced)
manage.py makemigrations     No changes detected
manage.py test               Ran 646 tests — OK
route audit                  ALL ROUTES CLEAN (33 routes: status, template-source
                             leakage, legacy classes, product-ui.css linked)
```

## Open items for human decision

1. `.recovery/` — scratch scripts, the Gemini patch and 7 preserved screenshots. Commit,
   gitignore, or delete.
2. 53 obsolete Gemini intermediate screenshots are untracked in the screenshots directory.
3. `fix/outreach-hard-bounces` is now fully absorbed and could be deleted.
4. `.h-18` / `.pt-18` survive only because Tailwind scans `gemiapp/**/*.py` and `tests.py`
   itself mentions them (`test_the_nav_height_class_is_generated`). No template uses them.
5. `test_login_is_rate_limited` is intermittently flaky through cross-test cache state; it
   passes on the pre-change baseline too, so it is pre-existing.
