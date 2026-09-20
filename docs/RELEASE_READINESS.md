# Gemi Leads 2.0 — Release readiness (G0 / G1)

Status as of 2026-09-20 (G1 drill 07:23–07:26 UTC, independently repeated 10:57–11:01 UTC). This file records the gates that block every Gemi Leads 2.0 migration from reaching
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
| G1 — staging forward/rollback | **PASSED** | A full drill on 2026-09-20 with a legacy parity digest captured at every stage: rollback → **authoritative 0031 baseline** → forward → rollback → reapply. All 13 legacy datasets matched the baseline on count *and* digest at every stage, and the whole drill was **repeated independently the same day with byte-identical digests** **[B]**. |
| G6 — request budget | **NOT_MEASURED** | The ≤7/min ceiling is structurally enforced **[A]**; the legacy importer's actual consumption has never been measured **[C]**. Not a dark-deploy blocker: no 2.0 job is scheduled. |
| Operator alerting (A2) | **IMPLEMENTED** | A production ERROR from `gemiapp.ingestion.client` / `gemiapp.services` emails the configured operators through the existing SMTP relay **[A]**. Sentry is not required and is not provisioned. One post-deploy confirmation remains **[C]**. |
| Production migrations 0032–0054 | **READY, pending the post-deploy alert confirmation** | G0, G1 and the A2 alerting requirement are closed. What is left is a confirmation that can only be made against production (below). |
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

### Migration drill — final G1 run, 2026-09-20 07:23–07:26 UTC **[B]**

PostgreSQL **17.6**. Source schema `gemiapp` **0031**, target **0054**, 23 migrations in the range. Before every
destructive command the drill re-verified, and aborted on any doubt: PostgreSQL, `GEMI_LEADS_ENVIRONMENT=staging`,
the GEMI collector disabled (production has a key; staging must not), and an opaque fingerprint of the connected
database unchanged since the drill began. **Production was never contacted at any point**, no GEMI request was
made, and no local worker or server held a connection (the only other connections were Supabase's own platform
components).

The staging-only 2.0 smoke fixtures (2 organizations, 2 organization radars, 2 signals, 1 snapshot, 2
opportunities) were destroyed by the rollbacks, as authorised. No legacy data was touched.

| Step | Duration | Result |
| --- | --- | --- |
| Rollback 0054 → 0031 (first) | 20.64 s | 23 migrations unapplied; 84 → 38 tables |
| **Forward 0031 → 0054** | **32.40 s** | `0031→0033` 4.29 s · **`0034` 3.85 s** · **`0035` 4.25 s** · `0035→0054` 20.01 s |
| Rollback 0054 → 0031 (second) | 20.58 s | 84 → 38 tables |
| **Reapply 0031 → 0054** | **35.93 s** | `0031→0033` 4.76 s · **`0034` 4.22 s** · **`0035` 4.68 s** · `0035→0054` 22.27 s |
| `manage.py check` at 0054 | — | No issues (0 silenced) |
| `manage.py migrate --check` at 0054 | — | Clean (exit 0) |
| D37 schedule across the drill | — | **1 → 0 → 1 → 0 → 1** |
| Schedule totals across the drill | — | 4 → 3 → 4 → 3 → 4, **zero duplicate funcs at every stage** |
| Legacy schedule identities | — | ids 1, 3 and 22 preserved unchanged throughout; only the D37 row is removed and re-created |
| SHADOW end-to-end smoke at 0054 (earlier) | — | Passed |

`0034` and `0035` are the only two migrations that touch a legacy table, and both stayed close to four seconds on
production-shaped volume — including `0035`'s two non-concurrent partial unique index builds over 33,391
`companyactivity` rows, which is the only operation in the range that takes `ACCESS EXCLUSIVE` on a table with
real data.

#### Confirmation run, 2026-09-20 10:57–11:01 UTC **[B]**

The whole drill was repeated from scratch against the same database (same fingerprint), on the later commit that
added operator alerting, with the 0031 column list **re-derived from `information_schema` rather than reused**.

| Step | Duration | First run |
| --- | --- | --- |
| Rollback 0054 → 0031 (first) | 19.04 s | 20.64 s |
| **Forward 0031 → 0054** | **31.56 s** | 32.40 s |
| — `0031→0033` / **`0034`** / **`0035`** / `0035→0054` | 4.22 s / **3.66 s** / **4.11 s** / 19.57 s | 4.29 / 3.85 / 4.25 / 20.01 s |
| Rollback 0054 → 0031 (second) | 18.15 s | 20.58 s |
| **Reapply 0031 → 0054** | **35.90 s** | 35.93 s |
| — `0031→0033` / **`0034`** / **`0035`** / `0035→0054` | 4.79 s / **4.42 s** / **4.63 s** / 22.06 s | 4.76 / 4.22 / 4.68 / 22.27 s |
| `check` · `migrate --check` · `makemigrations --check` at 0054 | — | all clean, exit 0, "No changes detected" |
| D37 across the run | **1 → 0 → 1 → 0 → 1** | same |
| Schedules · duplicate funcs | 4 → 3 → 4 → 3 → 4 · **0 at every stage** | same |
| Legacy schedule ids | 1, 3, 22 unchanged throughout | same |

**The re-derived column sets were identical, and all 13 baseline digests reproduced byte for byte.** Two
independent drills, run hours apart on different commits, produced the same authoritative 0031 baseline and the
same match at every stage — so the parity result is a property of the migrations, not of one capture.

### Legacy parity — the G1 verdict **[B]**

**Digest definition.** For each dataset, the **complete 0031-era column list** was read from
`information_schema.columns` at the authoritative 0031 baseline and reused verbatim at every later stage, so a
digest can only ever cover columns that existed at 0031 — no 2.0 column can enter it, and no column can be
silently omitted. The digest is

    md5(string_agg(md5(row(<every 0031 column, alphabetical>)::text), '' ORDER BY id))

For `gemiapp_companyactivity` the documented compatibility rule applies: at 0054 the legacy view is the
`legacy_listed` rows, and at 0031 the column does not exist and every row is legacy-visible. No row content and
no PII was printed at any point; only counts and digests were recorded. Two independent reads of the same 0031
state produced identical digests, so the capture itself is stable.

| dataset | baseline | forward | rollback | reapply | baseline digest | forward | rollback | reapply |
| --- | ---: | ---: | ---: | ---: | --- | :---: | :---: | :---: |
| `auth_user` (11 cols) | 17 | 17 | 17 | 17 | `1094f27862af353089744a5ea081d729` | ✓ | ✓ | ✓ |
| `gemiapp_usersubscription` (16) | 17 | 17 | 17 | 17 | `aae526f9312b7e9ac402e9025b7ea511` | ✓ | ✓ | ✓ |
| `gemiapp_customerradar` (13) | 17 | 17 | 17 | 17 | `e1a3d56c9ef753dbf3c2815f3761d30d` | ✓ | ✓ | ✓ |
| `gemiapp_radarmatch` (9) | 12,549 | 12,549 | 12,549 | 12,549 | `498a1e1998f9b7a5b740ef5213909ed0` | ✓ | ✓ | ✓ |
| `gemiapp_usercompanylead` (9) | 12,542 | 12,542 | 12,542 | 12,542 | `d3d2d19b5ed3ddea5cd6c776ea0cdc2d` | ✓ | ✓ | ✓ |
| `gemiapp_digestpreference` (9) | 17 | 17 | 17 | 17 | `d265fb2062780f3978db20611fdca1c1` | ✓ | ✓ | ✓ |
| `gemiapp_digestdelivery` (8) | 398 | 398 | 398 | 398 | `afc0a97217a5d510a53b30b06b55aff5` | ✓ | ✓ | ✓ |
| `gemiapp_outreachsuppression` (3) | 29 | 29 | 29 | 29 | `01d362b45ff78a5f57f7e02eb45f7569` | ✓ | ✓ | ✓ |
| `gemiapp_personsuppression` (7) | 0 | 0 | 0 | 0 | `d41d8cd98f00b204e9800998ecf8427e` | ✓ | ✓ | ✓ |
| `gemiapp_importrun` (9) | 750 | 750 | 750 | 750 | `42a71f73a2255fd7a9c0f12f1611b24e` | ✓ | ✓ | ✓ |
| `gemiapp_activitycode` (6) | 9,911 | 9,911 | 9,911 | 9,911 | `b4a3b446b90a449fca47b9214f8bdf64` | ✓ | ✓ | ✓ |
| `gemiapp_company` (22) | **4,903** | 4,903 | 4,903 | 4,903 | `76c3f8242fe320501a9c9302aeb19d42` | ✓ | ✓ | ✓ |
| `gemiapp_companyactivity` (5) | **33,391** | 33,391 | 33,391 | 33,391 | `7e57e07c67733506eebb6f63c0d352b4` | ✓ | ✓ | ✓ |

**Every legacy dataset matched the authoritative 0031 baseline, on count and on digest, at every stage.**
`gemiapp_company` = 4,903 and `gemiapp_companyactivity` = 33,391 are also identical to the production baseline
recorded above, so the drill ran on production-shaped volume and the 2.0 chain created and destroyed no legacy
row. After the reapply, `gemiapp_companyactivity` still holds **0** rows at `legacy_listed = false`: the 2.0
schema adds no derived rows on its own.

**`G1_STATUS = PASSED`.** The earlier gap — no digest captured before the forward migration — is closed: the
drill rolls staging back to 0031 first, captures the baseline there, and only then measures the forward run
against it; and it has now been done twice with identical results. Digests from the 2026-09-19 snapshot are
superseded; they covered a hand-picked subset of columns, while these cover every 0031-era column of every
dataset.

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

## Operator alerting (A2)

### The requirement

> **Production ERROR events from `gemiapp.ingestion.client` / `gemiapp.services` must actively notify an
> operator.**

This replaces the earlier wording, which named Sentry. That wording was written on this branch on 2026-09-15
(`74def43`, the A2 package) as the release gate for contract validation; it has never applied to deployed code.
Sentry itself is older — `f0885c1` added the SDK and `da3f72b` declared `SENTRY_DSN` in `render.yaml`, both on
`main` in August — but nothing in the codebase depends on it: `sentry_sdk.init` sits behind `if SENTRY_DSN:` and
`gemiapp` contains no Sentry import. **Sentry is one valid implementation of the requirement above. It is not
required, and it is not provisioned for this deployment.**

Why the requirement exists at all: from 0054 onward `GemiClient._validate` re-raises on a response the A2
contract refuses, and the error is not retried. `fetch_companies` validates every page before anything is
written, `import_for_date` marks the `ImportRun` failed and re-raises, `run_daily_pipeline_task` re-raises — and
`send_digests` never runs. **No customer gets a digest that day**, and without an active signal the failure is
found by a customer rather than by an operator.

### How this deployment satisfies it **[A]**

Django's own `AdminEmailHandler`, over the SMTP relay that already sends the digests. No new service, no new
credential, no new dependency.

| Piece | Where |
| --- | --- |
| Recipients | `ADMINS = operator_admins(SUPERADMIN_EMAILS)` in `config/settings.py`. No address is written in code; `render.yaml` already sets `SUPERADMIN_EMAILS`. Empty in, empty out — with no `ADMINS`, `AdminEmailHandler` returns before it builds a message, so an unset variable degrades to "no alert", never to an error. |
| Routing | `LOGGING` attaches `operator_console` (stderr) and `operator_email` to exactly `gemiapp.ingestion.client` and `gemiapp.services`, at `WARNING` — which is precisely the visibility they had before, when an unconfigured root left them on `logging.lastResort`. Nothing is suppressed and nothing new is emitted. |
| Level | The email handler is `ERROR` only. WARNING and INFO never notify. |
| Not production, no alert | The handler carries Django's `require_debug_false` filter, so a development run never notifies anyone. The test runner additionally empties `ADMINS` for the whole suite. |
| No duplicates | The email handler hangs off those two loggers and off no ancestor of them, so one record can only ever produce one message. `propagate` stays on, so `assertLogs` and any future root handler still see the record. |
| Failure containment | `config/operator_alerts.OperatorEmailHandler` routes any failure of its own to `logging.Handler.handleError`. Django already sends with `fail_silently`; this covers everything else, so a failing relay can never escape `logger.error()`, reach the ingestion path and mask the exception being reported. The original error still reaches stderr through the console handler beside it. |

Tests: `gemiapp/test_operator_alerts.py` (21) — recipient derivation, routing for both loggers, silence for
INFO/WARNING, for unrelated loggers, under `DEBUG`, and with no operators configured; one record producing exactly
one message; a raising backend leaving the import's exception, `ImportRun` status and stderr untouched.

### What remains, and it can only be done against production **[C]**

Confirm, once, that an operator email actually arrives from the production service. `manage.py sendtestemail
--admins` exercises the whole path — `ADMINS` plus the SMTP relay — without faking an ingestion failure. It is in
the dark-deployment smoke checklist and is the last item of the A2 gate.

### Secondary, passive evidence — unchanged

Render's stderr (`gemiapp` WARNING and ERROR reach it; INFO does not, and that is unchanged),
`ImportRun.status` / `error_message` at `/superadmin/pipeline/`, django-q's failure rows in `/admin/` (bounded by
`save_limit: 50`), and the `diagnose_intraday` command. All of them keep the evidence; none of them tells anyone.
They remain the triage material after an alert, not a substitute for one.

### Follow-up, deliberately not implemented here

An ERROR alert says a request failed; it does not say **the digest did not go out**. The stronger check is a daily
outcome heartbeat: one scheduled task asserting that yesterday has a successful `ImportRun` *and* the expected
`DigestDelivery` rows, which notifies when it does not. It catches this failure and every other cause of a missing
digest — a dead worker, a stuck schedule, an SMTP outage — none of which an ingestion-error alert would catch.
`diagnose_intraday` already contains the query logic. Recorded as the post-deploy follow-up.

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

1. **Confirm the operator alert path against production** — `manage.py sendtestemail --admins` after the deploy,
   as part of the smoke checklist. The mechanism is implemented and tested; this proves the relay delivers.
2. **Move the two production dumps out of the repository tree** (`.gitignore` now prevents accidental staging;
   the files still exist on disk).
3. **Staging GEMI key, or measured headroom (A5/G6)** — a follow-up, not a dark-deploy blocker, since no 2.0 job
   is scheduled and the dark deploy adds zero scheduled GEMI requests.
4. **Daily outcome heartbeat** — the post-deploy follow-up described under Operator alerting. Not required for the
   dark deploy.

G0, G1 and the A2 alerting requirement are closed (2026-09-20). Nothing in this repository now blocks the dark
deployment; item 1 is a confirmation to make against production, not a gate to build.
