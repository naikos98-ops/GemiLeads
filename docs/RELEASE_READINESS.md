# Gemi Leads 2.0 — Release readiness (G0 / G1)

Status as of 2026-09-19. This file records the gates that block every Gemi Leads 2.0 migration from reaching
production, the staging contract that G0 requires, and the rollback procedures that G1 must prove on staging.
It never contains secret values.

## Current verdict

| Gate | Status | Why |
| --- | --- | --- |
| G0 — staging environment | **BLOCKED_NO_STAGING** | No staging service, no staging database and no staging GEMI key exist. A local copy of the dev database is not staging. |
| G1 — staging forward/rollback | **BLOCKED** (depends on G0) | Only a local, non-production PostgreSQL rehearsal exists (below). It is evidence, not the gate. |
| Production migrations 0032–0054 | **BLOCKED_BY_G0_G1** | — |
| D37 schedule in production | **Not enabled** | Registered only when 0054 code is deployed; must not be deployed before G0/G1. |

## Authoritative criteria

The blueprint defines no G0/G1 checklist; the authoritative text is in `AGENTS.md`:

* **A2 gate:** before any 2.0 migration reaches production — a staging database, a separate GEMI API key for
  staging, and a known production data volume.
* **A4 gate (0032):** a staging database (G0) and a staging forward/rollback test (G1).
* **A5 gate:** a separate staging key, or measured headroom under the upstream limit of 8 requests/minute/key
  (production budget: 7 requests/minute shared across all workers).

Derived acceptance criteria:

**G0 passes only when all of these hold on a real remote staging deployment:**

1. A separate staging web + worker service and its own PostgreSQL database (not the production database, not a
   branch of it that shares credentials).
2. `GEMI_LEADS_ENVIRONMENT=staging` is set, and the app starts with it (see the contract below).
3. No production secret is present: no `GEMI_API_KEY` reliance, no live Stripe key, no production SMTP relay
   credentials, no production Brevo API key, no production webhook tokens.
4. A known production data volume (row counts of the tables the 2.0 chain alters: `gemiapp_company`,
   `gemiapp_companyactivity`), obtained by an authorized read — never guessed.
5. A staging backup can be taken and restored into an isolated database.

**G1 passes only when, on that staging database loaded with production-shaped data:**

1. Forward 0031 → 0054 succeeds with measured timings, `migrate --check` is clean, `manage.py check` passes.
2. Legacy aggregates (users, subscriptions, radars, matches, leads, digests, suppressions, companies, legacy
   company activities) are identical before and after the forward run.
3. Reverse 0054 → 0031 succeeds, and legacy aggregates are again identical.
4. The D37 schedule rollback procedure below is executed and leaves no schedule that errors.
5. Re-forward to 0054 leaves exactly one row per `SCHEDULES` entry.

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
| 0001–0031 | In `main` and `origin/main`. Production is **inferred** to be at 0031 (Render runs `python manage.py migrate` as `preDeployCommand`), but this has **not** been verified against production. |
| 0032–0054 | Only on `feature/gemi-2-a1-gemi-client`. Definitely not in any deployed code. |

**Auto-migrate hazard:** `render.yaml` runs `python manage.py migrate` on every deploy. Merging this branch into the
deployed branch applies 0032–0054 to production on the next deploy. Do not merge before G0/G1.

All 23 migrations are reversible. Reversal notes:

* 0034 adds nine GEMI metadata columns to `Company`; reversing drops them (and their data).
* 0035 extends `CompanyActivity`; reversing deletes rows with `legacy_listed = False` (2.0-only rows) and restores
  the old unique constraint. Legacy-visible rows survive.
* Every other migration creates new 2.0 tables; reversing drops them and their data.

## D37 schedule rollback procedure

`post_migrate` registers every `SCHEDULES` entry and never removes rows. Rolling back below 0054 (or deploying code
older than 0054) therefore leaves the `Task Due Notifications` schedule in `django_q_schedule`.

Since this release the task checks for its table first and returns `{"skipped": "notification_schema_missing"}`
with a warning, so a leftover row produces a daily logged skip, not a failing task. Still, remove the row on any
rollback below 0054:

1. Stop the worker (`qcluster`).
2. `python manage.py migrate gemiapp 0053` (or lower).
3. `python manage.py shell -c "from django_q.models import Schedule; Schedule.objects.filter(func='gemiapp.tasks.generate_task_due_notifications_task').delete()"`
4. If code older than 0054 is deployed, `post_migrate` will not re-create the row. If 0054 code stays deployed, the
   next `migrate` re-registers it; that is harmless because of the schema guard.
5. Start the worker.

Re-applying 0054 re-registers exactly one row (`cron 0 8 * * *`, Europe/Athens).

## Local PostgreSQL rehearsal (not G0/G1)

Run 2026-09-19 on a throwaway PostgreSQL 17 cluster on `127.0.0.1`, loaded from the local dev copy
(153,210 objects; 17,799 companies; 119,215 legacy company activities). No production system was contacted. The
cluster, the dump and the backup file were destroyed afterwards. Timings are for this small dataset only; they say
nothing about production volume, which is still unknown.

| Step | Result |
| --- | --- |
| `pg_dump -Fc` backup | exit 0, 5 s, 18.4 MB |
| Restore into an isolated temporary DB | `pg_restore` exit 0 in 12 s; `check` clean; `migrate --check` clean; legacy aggregates identical; temporary DB dropped |
| Reverse `gemiapp` 0054 → 0031 | exit 0, 8 s, 23 migrations unapplied; 2.0 tables gone; legacy aggregates identical |
| Forward 0031 → 0054 | exit 0, 6 s, 23 migrations applied; `migrate --check` clean; legacy aggregates identical |
| Schedules after reverse to 0031 | 4 rows — **the D37 row is re-registered by `post_migrate` even at 0031** (the hazard the procedure above removes) |
| Schema guard at 0053 | task returns `{"skipped": "notification_schema_missing"}` with a warning; procedure step 3 removes the row; reapply → exactly 1 D37 row, 4 total |

Legacy aggregates compared: row count and an md5 digest over the 0031-era columns of `auth_user`,
`usersubscription`, `customerradar`, `radarmatch`, `usercompanylead`, `digestpreference`, `digestdelivery`,
`outreachsuppression`, `personsuppression`, `importrun`, `activitycode`, `company`, and the legacy-visible
`companyactivity` rows.
