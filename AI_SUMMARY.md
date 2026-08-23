# Gemi Leads - Comprehensive AI Project Summary

> **STATUS: BETA — BILLING IS NOT ACTIVE.** The application, the GEMI pipeline and the emails all
> work, but no payment can be taken: `LEGAL_BILLING_ACTIVE` defaults to `0`, which makes
> `create_checkout_session` refuse server-side and removes the checkout buttons from the pricing
> page. Access is granted by hand through the Superadmin panel (complimentary access). Prices shown
> anywhere in the product are indicative until payments open. `BETA_MODE` (default `1`) controls the
> visible "Beta" label independently.

## 1. Overview & Business Value
**Gemi Leads** is a B2B SaaS application built to help businesses monitor and capture new companies registering in the official Greek General Commercial Registry (ΓΕΜΗ). 
**Problem Solved:** Instead of manually searching through the ΓΕΜΗ portal every day to find new potential clients, users can set up automated "Customer Radars" based on specific NACE (ΚΑΔ) codes. The application automatically fetches new registrations daily via the official GEMI Open Data API, matches them against user preferences, and sends personalized email digests to users with their new leads.

## 2. Technical Stack
* **Framework:** Django 5.2 (Python)
* **Database:** PostgreSQL (Production via `dj-database-url`), SQLite (Development)
* **Frontend:** Django Templates with Tailwind CSS (via local classes or CDN), Vanilla JS (`app.js`)
* **Email Delivery:** Brevo (SMTP)
* **Payments:** Stripe (Checkout, Customer Portal, Webhooks) — fully implemented but **switched off in beta**; price ids exist for Pro, Business and Enterprise
* **Background Jobs:** `django-q2` (Database-backed queue and scheduler)
* **Error Tracking:** Sentry
* **Deployment:** Render (Docker container, Gunicorn, Whitenoise for static files)

## 3. Core Architecture & Workflow
The system operates on a daily pipeline (`python manage.py run_daily_pipeline`):
1. **Fetch:** Hits the `opendata-api.businessportal.gr` to get yesterday's registrations.
2. **Normalize:** Parses and normalizes NACE (ΚΑΔ) codes and company data.
3. **Match:** Checks every new company against every active `CustomerRadar` created by users.
4. **Lead Generation:** For every match, a `UserCompanyLead` is generated for the specific user, and a `RadarMatch` links the lead to the radar that found it.
5. **Digest:** The pipeline sends a daily email digest to every entitled user, plus a 3-hourly real-time digest to Enterprise/Custom tiers. The weekly digest was removed on 2026-08-22.

## 4. Key Database Models (`gemiapp/models.py`)
* `Company`: The core model storing official GEMI data (AFM, Name, GEMI number, Registration Date, Legal Form, Address).
* `CompanyActivity`: Maps a `Company` to specific NACE codes (ΚΑΔ).
* `CustomerRadar`: A user's saved search criteria (NACE codes, location, etc.).
* `UserCompanyLead`: The actual "lead" object assigned to a user. Tracks lifecycle statuses (`new`, `viewed`, `contacted`, `converted`, `rejected`), user notes, and favorites.
* `RadarMatch`: The junction tracking exactly *why* a company matched a specific radar.
* `UserSubscription`: Tracks a user's Stripe subscription status and limits their active radars. During beta every entitlement comes from `complimentary_tier`, not from Stripe. Limits live in `RADAR_LIMITS` in `gemiapp/models.py` (Free: 0, Pro: 5, Business: 10, Enterprise/Custom: 15) and may be overridden per account via `custom_radar_limit`.

## 5. Key Integrations & Configurations
* **Authentication:** Built-in Django Auth with enforced email verification (`is_active=False` on signup). Users confirm via tokenized email links. Rate limiting (`django-ratelimit`) protects login/signup endpoints.
* **Stripe:** Implemented in `gemiapp/billing.py`. Handles Checkout Sessions and a webhook (`/stripe_webhook/`) to update `UserSubscription.tier` dynamically. **Gated behind `LEGAL_BILLING_ACTIVE`; while that is `0` no checkout session is ever created.**
* **Email:** Configured to use SMTP via Brevo (`smtp-relay.brevo.com`). The sender domain is authenticated (`notifications@send.gemileads.gr`).

## 6. Features Implemented (Completed Phases)
* **Phase 1 (Core Radars):** Idempotent matching logic, NACE code catalog with autocomplete, Radar CRUD, soft deletion.
* **Phase 2 (Lead Inbox):** Personal inbox per user, advanced filtering, lead lifecycles, private notes, favorites, CSV exports per radar.
* **Phase 3 (Digest Integration):** Radar-based daily email digests, deduplication across multiple radars, HTML/TXT email templates.
* **Phase 4 & 6 (Production Hardening):** PostgreSQL setup, Sentry integration, dynamic limits per subscription tier, Docker and `django-q2` deployment setup.
* **Phase 5 (Auth Flow):** Email verification on signup, password reset flow, secure unsubscribe links without login.
* **Phase 7 (Stripe):** Full payment integration with test-mode readiness.

## 7. Guidelines for AI Agents Working on This Codebase
1. **Read `AGENTS.md`:** Always start by reading `AGENTS.md` which acts as the dynamic changelog and task tracker for the project.
2. **No Fake Data:** The database contains real GEMI data and a massive NACE catalog. Do not seed generic fake data that corrupts the real catalog.
3. **Email Configuration:** Always ensure `DEFAULT_FROM_EMAIL` uses the verified domain (`notifications@send.gemileads.gr`).
4. **Environment Variables:** Never commit secrets. Rely on `.env` locally and Render Environment Variables in production.
5. **UI Consistency:** Stick to the established Tailwind aesthetics (vibrant navy/blue palettes, rounded corners, soft shadows).
6. **Testing:** Before marking any task as complete, ensure `python manage.py test` and `manage.py check` run without errors. On a clean clone the suite needs `pip install -r requirements.txt`, `npm ci && npm run build:css` and `manage.py collectstatic` first — otherwise ~126 tests fail on a missing staticfiles manifest, which looks like broken code but is an unbuilt environment.
7. **Do not re-enable billing casually.** The product is in beta and must not charge anyone. `LEGAL_BILLING_ACTIVE` is the only switch, and turning it on is a business decision, not a cleanup task.
8. **Do not hardcode record counts into documentation.** Company/lead/user totals live in the production database and go stale immediately; a wrong figure has already reached public copy once.
