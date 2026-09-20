# Gemi Leads 2.0 — Release readiness (G0 / G1)

Status as of 2026-09-20. This file records the gates that block every Gemi Leads 2.0 migration from reaching
production, the staging contract that G0 requires, and the rollback procedures that G1 must prove on staging.
It never contains secret values, hostnames, database names or keys.

## How evidence is classified

Every claim below carries its class. Nothing is presented as proven by the repository when it is not.

| Class | Meaning |
| --- | --- |
| **[A] repository-recorded** | Reproducible from this repository: code, migrations, tests, or a command anyone can re-run |
| **[B] externally observed** | Run against a real environment (production read-only, or the staging database) and recorded here by the operator who ran it. Not reproducible from the repository alone |
| **[C] open** | Not done, or not verifiable from here |

## Current verdict

| Gate | Status | Why |
| --- | --- | --- |
| G0 — staging environment | **PASSED_WITH_DOCUMENTED_SCOPE** | A separate staging PostgreSQL exists, loaded with production-shaped data, driven by local application processes. See the scope note below. |
| G1 — staging forward/rollback | **PARTIAL** | Forward, rollback and reapply all passed on staging **[B]**, and the post-drill volumes match production exactly **[B]**. No *pre-drill* digest was captured, so "the legacy digests were identical before and after" is **not** evidenced. |
| G6 — request budget | **NOT_MEASURED** | The ≤7/min ceiling is structurally enforced **[A]**; the legacy importer's actual consumption has never been measured **[C]**. Not a dark-deploy blocker: no 2.0 job is scheduled. |
| Sentry alert rule (A2) | **UNVERIFIABLE_FROM_REPO** | The code supports `SENTRY_DSN` **[A]**; whether it is set in production, and whether an alert rule exists, can only be confirmed in the Sentry and Render dashboards **[C]**. |
| Production migrations 0032–0054 | **BLOCKED** | Blocked only by the Sentry check and the G1 gap above, not by the environment. |
| D37 schedule in production | **Not enabled** | Registered only when 0054 code is deployed. Currently 0 rows in production **[B]**. |

## Authoritative criteria

The blueprint defines no G0/G1 checklist; the authoritative text is in `AGENTS.md`:

* **A2 gate:** confirm `SENTRY_DSN` is set in production and that a Sentry alert rule exists for ERROR events of
  `gemiapp.ingestion.client` / `gemiapp.services`; and, before any 2.0 migration reaches production — a staging
  **database**, a separate GEMI API key for staging, and a known production data volume.
* **A4 gate (0032):** a staging database (G0) and a staging forward/rollback test (G1).
* **A5 gate:** a separate staging key, **or** measured headroom under the upstream limit of 8 requests/minute/key
  (production budget: 7 requests/minute shared across all workers).

### Scope note: what "staging" means here

A previous revision of this file derived a stricter criterion — "a separate staging **web + worker service**" —
which `AGENTS.md` does not require. It was removed deliberately, not overlooked. What `AGENTS.md` asks for is a
staging *database*, and that exists.

**Practical staging, as accepted:**

    local application processes  ->  separate staging PostgreSQL (Supabase)

The database is a distinct instance, not the production database and not a credential-sharing branch of it. The
application runs from a local checkout against it, with `GEMI_LEADS_ENVIRONMENT=staging`, so every staging guard
in `config/environment.py` applies: the GEMI collector is disabled without `GEMI_STAGING_API_KEY`, outreach is
forced off, email goes to the console backend, and a live Stripe key refuses to start.

**What this scope does not rehearse**, stated so nobody assumes otherwise:

| Not rehearsed | Substitute performed | Result |
| --- | --- | --- |
| Render's `preDeployCommand: python manage.py migrate` | The same migrations run against staging by hand | Passed **[B]** |
| `scripts/build_render.sh` | `npm ci`, `npm run build:css`, `collectstatic` run locally | Passed **[A]**, below |
| gunicorn serving the WSGI app | `config.wsgi.application` imported, then the app served over HTTP against staging | Passed **[B]**, below. gunicorn itself needs `fcntl` and cannot run on the Windows workstation; Render is Linux |
| `qcluster` booting | **Not possible on the workstation**: django-q2 requires `multiprocessing.get_context("fork")`, which Windows does not provide. Every scheduled `func` was resolved and its schema guard evaluated against staging instead | Partial **[B]**, below |
| Render's own build, boot and env-var wiring | — | **[C]** — the deploy is its own first rehearsal; §Rollback covers it |

## Production volume (authorized read-only inspection) **[B]**

| Fact | Value |
| --- | --- |
| Engine | PostgreSQL 17.6 |
| `gemiapp` migration state | 0031 |
| 0032–0054 applied | No |
| 2.0 tables present | None |
| `gemiapp_company` rows | **4,903** |
| `gemiapp_companyactivity` rows | **33,391** |
| `django_q_schedule` rows | 3 |
| D37 schedule rows | 0 |

This is the "known production data volume" the A2 gate requires. It also sizes the only migration with real lock
risk: `0035` builds two partial unique indexes over 33,391 `companyactivity` rows — small, and expected to be
brief, but it is a non-concurrent index build and takes `ACCESS EXCLUSIVE` while it runs.

## Staging evidence **[B]**

| Fact | Value |
| --- | --- |
| Engine | PostgreSQL 17.6 (Supabase), separate instance |
| `GEMI_LEADS_ENVIRONMENT` | `staging` |
| `gemiapp` migration state | 0054 |
| Tables | 84 |
| GEMI collector | Disabled (no `GEMI_STAGING_API_KEY`): every request is refused before anything is sent |

### Migration drill

| Step | Result |
| --- | --- |
| Forward 0031 → 0054 | Passed |
| Rollback 0054 → 0031 | Passed |
| Reapply 0031 → 0054 | Passed |
| D37 schedule lifecycle across the drill | 0 → 1 → 0 → 1, as designed |
| SHADOW end-to-end smoke at 0054 | Passed |

### Legacy parity snapshot (read-only, 2026-09-20)

Counts and md5 digests over the 0031-era columns, taken after the drill. Nothing was written to staging.

| Table | Rows | md5 of row digest |
| --- | --- | --- |
| `auth_user` | 17 | `9b407a5bba32b10b78c83028d31f6122` |
| `gemiapp_usersubscription` | 17 | `e560d3e58dd3591631ec322d0b33ac2b` |
| `gemiapp_customerradar` | 17 | `5e84806badc234dff504c26cec809aa5` |
| `gemiapp_radarmatch` | 12,549 | `9d07dc54049b08a04c8b92f34a264362` |
| `gemiapp_usercompanylead` | 12,542 | `31d3e70ee9dd22929340659d577d02ad` |
| `gemiapp_digestpreference` | 17 | `fa7eb4b07f943fe79ab6da5a8613c1c6` |
| `gemiapp_digestdelivery` | 398 | `b346d0524718c700b544e62cf80ac2c7` |
| `gemiapp_outreachsuppression` | 29 | `825bae6e2d4b7b6f8b5dc8b72b7f3245` |
| `gemiapp_personsuppression` | 0 | `d41d8cd98f00b204e9800998ecf8427e` |
| `gemiapp_importrun` | 750 | `81bffb112e7e74f4110b4e94c7e93200` |
| `gemiapp_activitycode` | 9,911 | `528507162b75759ecc71a2b5d971257d` |
| `gemiapp_company` | **4,903** | `495df351747c50a7a344ac1ab30cebcc` |
| `gemiapp_companyactivity` where `legacy_listed` | **33,391** | `dc6acdb82ea6e6c794450fae4aed33eb` |

Digest definition (reproducible): `md5(string_agg(md5(row(<0031-era columns>)::text), '' ORDER BY id))`, with
`gemiapp_company` including `raw_data::text`.

**What this proves.** `gemiapp_company` = 4,903 and legacy-visible `gemiapp_companyactivity` = 33,391 are
**identical to the production baseline** above, after the full forward → rollback → reapply cycle. No legacy row
was created or destroyed by the 2.0 chain on the two tables where an independent production baseline exists.
`gemiapp_companyactivity` total is also 33,391 with **0** rows at `legacy_listed = false`, so the 2.0 schema has
added no derived rows: the legacy view is the whole table.

**What this does not prove, and why G1 is not marked fully passed.** No digest was captured *before* the forward
migration on this database, so the criterion "legacy aggregates are identical before and after the forward run"
has no before-value to compare against. The table above is a **forward baseline** for any future drill, not
evidence of equality across the one already performed. Closing this honestly means one more drill on staging with
a digest captured at 0031 first — it does **not** mean re-running anything in production. No parity evidence has
been invented to fill the gap.

**Note.** Staging also carries the SHADOW smoke's own rows (2 organizations, 2 organization radars, 2 signals of
which 1 is LIVE, 1 snapshot, 2 opportunities). The LIVE signal is a smoke fixture; it will make
`run_g4_shadow_cycle`'s precheck refuse on staging, which is the precheck working as designed.

## Local rehearsal against staging (2026-09-20) **[B]**

| Step | Result |
| --- | --- |
| `manage.py check` | No issues (0 silenced) |
| `manage.py check --deploy`, `.env` as-is | 6 warnings — all six are consequences of the local `DJANGO_DEBUG=1`, not of the 0054 code |
| `manage.py check --deploy` with the switches `render.yaml` sets (`DJANGO_DEBUG=0`, a long secret, real allowed hosts) | **No issues (0 silenced)** |
| `from config.wsgi import application` (what gunicorn loads) | Imported |
| App served over HTTP against staging | `GET /` → 200, `GET /accounts/login/` → 200, `GET /dashboard/` → 302 to login. Stopped cleanly |
| `qcluster` boot | **Not possible on Windows** — django-q2 needs the `fork` start method |
| Every `apps.SCHEDULES` func resolved against staging | All 4 import and are callable; D37's `requires_schema` guard returns True at 0054 |
| Staging state after the rehearsal | 4 schedules, **D37 exactly 1**, queue untouched (1 pre-existing item, not consumed), `importrun` unchanged at 750 |

No GEMI job was run and no GEMI request was made: the qcluster rehearsal disabled the scheduler before starting,
and all four staging schedules were past due, so a plain boot would have fired both pipeline tasks.

## Build rehearsal (worktree, 2026-09-20) **[A]**

`scripts/build_render.sh` is `pip install -r requirements.txt`, `npm ci`, `npm run build:css`, `collectstatic`.

| Step | Result |
| --- | --- |
| `pip install -r requirements.txt` | **Skipped deliberately** — the pinned dependencies are already installed and reinstalling would mutate the working environment |
| `npm ci --no-audit --no-fund` | 73 packages, clean |
| `npm run build:css` | `static/css/app.css` regenerated (27,296 bytes) |
| `manage.py collectstatic --noinput` | 1 copied, 134 unmodified, 372 post-processed — the hashed manifest builds |
| Working tree after the build | Clean: `app.css`, `node_modules/` and `staticfiles/` are all ignored |

## Sentry (A2) **[A]** + **[C]**

| Fact | Class |
| --- | --- |
| `sentry-sdk==2.0.0` is pinned | **[A]** |
| `config/settings.py` initialises Sentry when `SENTRY_DSN` is set, with `DjangoIntegration()` and a 5% trace sample | **[A]** |
| sentry-sdk 2.x enables the logging integration by default, so a `logger.error` becomes an event | **[A]** |
| `render.yaml` declares `SENTRY_DSN` with `sync: false` — set in the Render dashboard, never in the repository | **[A]** |
| Whether `SENTRY_DSN` is actually set on the production service | **[C]** — dashboard only |
| Whether an alert rule exists for ERROR events of `gemiapp.ingestion.client` / `gemiapp.services` | **[C]** — Sentry only |

No code can verify an external alert rule. **This is the one manual operator blocker**, and it matters: from 0054
onward `GemiClient._validate` re-raises on a contract violation, so an upstream GEMI schema change stops the daily
import and therefore the daily digest. `config/settings.py` defines no `LOGGING`, so `gemiapp` INFO lines never
reach the Render log — ERROR and WARNING do, and Sentry is the alerting path.

## Staging environment contract (`config/environment.py`)

| Variable | Staging meaning |
| --- | --- |
| `GEMI_LEADS_ENVIRONMENT` | `production` (default when unset — production behaviour is unchanged), `staging`, or `development`. Any other value refuses to start. |
| `GEMI_STAGING_API_KEY` | The only GEMI key staging may use. `GEMI_API_KEY` is **ignored** in staging. Without it the collector is disabled: every GEMI request raises `GemiConfigurationError` before anything is sent. |
| `STAGING_EMAIL_BACKEND` | The email backend in staging; default is the console backend. The SMTP relay is never used unless this is set explicitly. |
| `STAGING_BREVO_API_KEY` | The only Brevo API key staging may use. `BREVO_API_KEY` is ignored. |
| `OUTREACH_ENABLED` | Forced off in staging regardless of its value. |
| `STRIPE_SECRET_KEY` | A live key (`sk_live_…`, `rk_live_…`) refuses to start. Only test-mode keys, test prices and a test webhook secret may be configured. |

Staging must not share the production worker's GEMI rate budget. The budget lives in the `shared` database cache,
so a separate staging database also means a separate budget.

## Migration range

| Range | State |
| --- | --- |
| 0001–0031 | In `main` and `origin/main`. Production is confirmed at 0031 **[B]**. |
| 0032–0054 | Only on `feature/gemi-2-a1-gemi-client`. Not in any deployed code. |

**Auto-migrate hazard:** `render.yaml` runs `python manage.py migrate` on every deploy. Merging this branch into
the deployed branch applies 0032–0054 to production on the next deploy. Do not merge before the blockers below
are cleared.

All 23 migrations are reversible **[A]**. No forward migration rewrites existing data; the only `RunPython` is
reverse-only. Reversal notes:

* 0034 adds nine nullable GEMI metadata columns to `Company`; reversing drops them (and their data).
* 0035 extends `CompanyActivity`; reversing deletes rows with `legacy_listed = False` (2.0-only rows) and restores
  the old unique constraint. Legacy-visible rows survive.
* Every other migration creates new 2.0 tables; reversing drops them and their data.

## D37 schedule lifecycle

The `Task Due Notifications` schedule is schema-aware (`requires_schema` in `gemiapp/apps.py`): the `post_migrate`
registration keeps **exactly one** row while the notification table exists and **none** while it does not. A
rollback below 0054 therefore removes the row automatically; reapplying 0054 registers exactly one again. The
three legacy schedules are registered exactly as before. No manual row deletion is needed.

Defence in depth: the task itself still checks for its table and returns
`{"skipped": "notification_schema_missing"}` with a warning, which covers a row that survives some other way (for
example code older than this fix deployed on a 0054 schema that is then rolled back without running `migrate`).

Operationally, still stop the worker (`qcluster`) around any rollback so no task runs against a half-migrated schema.

Verified on a throwaway PostgreSQL 17 cluster (empty database, 2026-09-19) **[A]**, and again across the staging
drill (0 → 1 → 0 → 1) **[B]**:

| State | Notification table | D37 rows | Total rows |
| --- | --- | --- | --- |
| gemiapp 0053 | no | 0 | 3 |
| forward to 0054 | yes | 1 | 4 |
| registration run twice more | yes | 1 | 4 |
| reverse to 0053 | no | 0 | 3 |
| reverse to 0031, then `migrate` again at 0031 | no | 0 | 3 |
| reapply 0054 | yes | 1 | 4 |

Legacy rows kept their id, cron and next_run in every state; no function had duplicate rows.

## Local PostgreSQL rehearsal (historical, not G0/G1)

Run 2026-09-19 on a throwaway PostgreSQL 17 cluster on `127.0.0.1`, loaded from the local dev copy
(153,210 objects; 17,799 companies; 119,215 legacy company activities). No production system was contacted. The
cluster, the dump and the backup file were destroyed afterwards. Timings are for that dataset, which is **larger**
than production, so they are a loose upper bound rather than a prediction.

| Step | Result |
| --- | --- |
| `pg_dump -Fc` backup | exit 0, 5 s, 18.4 MB |
| Restore into an isolated temporary DB | `pg_restore` exit 0 in 12 s; `check` clean; `migrate --check` clean; legacy aggregates identical; temporary DB dropped |
| Reverse `gemiapp` 0054 → 0031 | exit 0, 8 s, 23 migrations unapplied; 2.0 tables gone; legacy aggregates identical |
| Forward 0031 → 0054 | exit 0, 6 s, 23 migrations applied; `migrate --check` clean; legacy aggregates identical |
| Schedules after reverse to 0031 | 4 rows — **the D37 row was re-registered by `post_migrate` even at 0031** (fixed since: see D37 schedule lifecycle) |
| Schema guard at 0053 | task returns `{"skipped": "notification_schema_missing"}` with a warning; reapply → exactly 1 D37 row, 4 total |

Legacy aggregates compared: row count and an md5 digest over the 0031-era columns of `auth_user`,
`usersubscription`, `customerradar`, `radarmatch`, `usercompanylead`, `digestpreference`, `digestdelivery`,
`outreachsuppression`, `personsuppression`, `importrun`, `activitycode`, `company`, and the legacy-visible
`companyactivity` rows. This rehearsal **did** capture before and after digests and found them identical — on the
dev-copy database, not on staging, which is why it does not close the G1 gap above.

## Data safety

Production dumps are real customer data and never enter the repository. `.gitignore` carries `*.dump`, `*.sql` and
`*.sql.gz` so a stray dump cannot be staged by `git add -A`; `.env` and `backups/` were already ignored. As of
2026-09-20 no dump or SQL file is tracked in any branch, and the two local production dumps are untracked. They
must be moved out of the repository tree, and no file is deleted automatically.

The ignore rules only protect a checkout once a branch carrying them is checked out there. Until this commit
reaches the branch checked out in the main working copy, the dumps in that copy remain unignored.

## Remaining blockers before a dark deployment

1. **Sentry (A2)** — confirm `SENTRY_DSN` is set on the production service and that an alert rule exists for ERROR
   events of `gemiapp.ingestion.client` / `gemiapp.services`. Manual, dashboard-only, and the one hard blocker.
2. **G1 pre-drill digests** — either accept G1 as PARTIAL with this file's reasoning, or run one more staging
   drill capturing the digest at 0031 before the forward run.
3. **Move the two production dumps out of the repository tree** (`.gitignore` now prevents accidental staging;
   the files still exist on disk).
4. **Staging GEMI key, or measured headroom (A5/G6)** — a follow-up, not a dark-deploy blocker, since no 2.0 job
   is scheduled and the dark deploy adds zero scheduled GEMI requests.
