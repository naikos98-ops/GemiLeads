# Incident & Audit Record: Cold Outreach Subsystem Permanent Shutdown

**Document Reference**: `docs/compliance/cold-outreach-shutdown.md`  
**Date of Audit**: September 9, 2026  
**Status**: **PERMANENTLY SHUT DOWN (FAIL-CLOSED)**  
**Target Subsystem**: Cold Prospecting / Unsolicited Client Outreach (`CompanyOutreach`)  
**Scope**: Verification of emergency hotfix, entry point audit, queue transition, management command hardening, data retention audit, copy audit, and operational verification checklists.

---

## 1. Executive Summary & Incident Overview

On September 9, 2026, an emergency hotfix was deployed following a recipient complaint regarding unsolicited marketing email sent to a newly registered GEMI company. 

Immediate operational directives were established:
1. **Immediate Cessation**: All unsolicited marketing/cold outreach sends to newly registered GEMI businesses were stopped globally.
2. **Fail-Closed Default**: Master switch `OUTREACH_ENABLED` was introduced with hardcoded default `False` / `"0"`, and daily send cap `OUTREACH_DAILY_SEND_CAP` was set to `0`.
3. **Queue Freeze & Cancellation**: Historical `pending` and `sending` queue records were transitioned via database migration (`0031_cancel_pending_outreach.py`) to a non-sendable `cancelled` state to prevent any future accidental resurrection.
4. **Transactional Preservation**: Non-marketing, user-requested transactional emails (account verification, password resets, and user-configured radar digests) remain fully operational and isolated from cold outreach logic.

---

## 2. Exhaustive Outreach Entry Points & Safeguards Map

Every mechanism in the codebase capable of queuing, sending, or retrying unsolicited cold outreach was audited and verified to fail closed when `OUTREACH_ENABLED=0`:

| # | Entry Point / Mechanism | File & Symbol Location | Action When `OUTREACH_ENABLED=0` | Status |
|---|---|---|---|---|
| 1 | **Batch Queueing Service** | [superadmin/services.py](file:///d:/Projects/Gemi-Signal/gemiapp/superadmin/services.py#L686) `queue_company_outreach()` | Returns `0`, logs warning, creates 0 rows, enqueues 0 tasks. | **FAIL-CLOSED** |
| 2 | **Test Outreach Service** | [superadmin/services.py](file:///d:/Projects/Gemi-Signal/gemiapp/superadmin/services.py#L608) `send_outreach_test_email()` | Raises `RuntimeError("Οι αποστολές outreach είναι απενεργοποιημένες.")`. | **FAIL-CLOSED** |
| 3 | **Pending Queue Worker** | [superadmin/services.py](file:///d:/Projects/Gemi-Signal/gemiapp/superadmin/services.py#L740) `process_pending_outreach()` | Returns `(0, 0, pending)`, logs warning, sends 0 emails. | **FAIL-CLOSED** |
| 4 | **Background Send Task** | [tasks.py](file:///d:/Projects/Gemi-Signal/gemiapp/tasks.py#L81) `send_company_outreach_task()` | Returns `{"sent": 0, "failed": 0, "skipped": N}`, logs warning, aborts send. | **FAIL-CLOSED** |
| 5 | **Daily Drain Cron Task** | [tasks.py](file:///d:/Projects/Gemi-Signal/gemiapp/tasks.py#L114) `drain_pending_outreach_task()` | Returns `{"sent": 0, "failed": 0, "skipped": 0}`, logs info, skips execution. | **FAIL-CLOSED** |
| 6 | **Superadmin Send View** | [superadmin/views.py](file:///d:/Projects/Gemi-Signal/gemiapp/superadmin/views.py#L335) `client_finder_send()` | Sets UI alert message, redirects back to Client Finder, blocks send. | **FAIL-CLOSED** |
| 7 | **Superadmin Test View** | [superadmin/views.py](file:///d:/Projects/Gemi-Signal/gemiapp/superadmin/views.py#L366) `client_finder_test()` | Sets UI alert message, redirects back to Client Finder, blocks send. | **FAIL-CLOSED** |
| 8 | **Requeue CLI Command** | [requeue_dropped_outreach.py](file:///d:/Projects/Gemi-Signal/gemiapp/management/commands/requeue_dropped_outreach.py) `handle()` | Raises `CommandError`, refuses DB modifications and task enqueuing. Excludes `cancelled` rows. | **FAIL-CLOSED** |
| 9 | **Bot Prune CLI Command** | [prune_bot_suppressions.py](file:///d:/Projects/Gemi-Signal/gemiapp/management/commands/prune_bot_suppressions.py) `handle()` | Skips `CompanyOutreach` requeue step, logs warning message, creates no pending rows. | **FAIL-CLOSED** |

---

## 3. Technical Safeguards & Environment Configuration

- **Default Switch Setting**: `config/settings.py` evaluates `OUTREACH_ENABLED = os.getenv("OUTREACH_ENABLED", "0") == "1"`. If the environment variable is missing, unconfigured, or set to `"0"`, it evaluates to `False`.
- **Environment Overrides**:
  - Local `.env`: `OUTREACH_ENABLED=0`, `OUTREACH_DAILY_SEND_CAP=0`
  - `.env.example`: `OUTREACH_ENABLED=0`
  - `render.yaml`: `OUTREACH_ENABLED="0"`, `OUTREACH_DAILY_SEND_CAP="0"`
- **Superadmin UI Enforcement**: `templates/superadmin/client_finder/list.html` displays a prominent red warning banner and disables all action buttons (`disabled` attribute + opacity style) when `outreach_enabled` is `False`.

---

## 4. Pending Queue Cancellation (`0031_cancel_pending_outreach.py`)

To ensure historical pending outreach entries cannot be accidentally processed if `OUTREACH_ENABLED` were ever toggled in the future:
1. **Schema Update**: Added `("cancelled", "Ακυρώθηκε")` to `CompanyOutreach.STATUSES`.
2. **Data Migration**: Migration `0031_cancel_pending_outreach.py` updated all pre-existing records with `status__in=["pending", "sending"]` to `status="cancelled"`.
3. **Audit Trail**: Updated records carry the explanatory `error_message`: `"Αποστολή ακυρώθηκε λόγω οριστικής διακοπής cold outreach (compliance shutdown)."`.
4. **Resurrection Prevention**: Because workers (`process_pending_outreach`, `drain_pending_outreach_task`) explicitly query `status="pending"`, historical `cancelled` records will **never** be picked up by worker runs.

---

## 5. Suppression & Do-Not-Contact System Audit

- **Recipient Opt-Out Handling**: The `OutreachSuppression` table records minimum required identifiers (normalized email addresses) strictly necessary to prevent re-contact.
- **Suppression Verification**: All outreach functions (`uncontacted_companies_qs()`, `process_pending_outreach()`) consult `OutreachSuppression.is_suppressed(email)`.
- **Brevo Webhook Integration**: Hard bounces (`hardBounce`, `hard_bounce`) and block events (`blocked`, `spam`) received via `gemiapp/email_tracking.py:brevo_webhook` automatically populate `OutreachSuppression`.
- **Privacy & UI Protection**: Suppression details are kept minimal (address + timestamp only). Complainant details are never exposed in product UI or administrative displays.
- **Isolation from Transactional Email**: Account verification (`send_verification_email`), password resets (`PasswordResetView`), and user-configured radar digests (`send_digests`) bypass `OutreachSuppression` and function normally for registered platform users.

---

## 6. External Email Provider Safety (Brevo Manual Verification Checklist)

Application-side cold outreach was built using inline Django templates sent via SMTP (`X-Mailin-Tag: outreach:<company_id>`). No automated campaigns were created in Brevo via API.

However, because platform access to the Brevo dashboard is managed separately, the operator must complete the following manual checklist:

- [ ] **1. Active Email Campaigns**: Log into Brevo Dashboard -> **Campaigns** -> **Email**. Confirm that zero scheduled, draft, or running marketing campaigns exist.
- [ ] **2. Automations & Workflows**: Check **Automations**. Confirm no active workflows exist that trigger marketing emails upon contact creation or tag assignment.
- [ ] **3. Contact Lists**: Check **Contacts** -> **Lists**. Ensure no GEMI lead lists are configured with auto-responders or drip sequences.
- [ ] **4. Webhooks**: Check **Transactional** -> **Settings** -> **Webhooks**. Verify that the webhook URL points to `/api/brevo/webhook/<token>/` and that webhook secret matches `BREVO_WEBHOOK_TOKEN`.
- [ ] **5. Transactional Sending Status**: Send a test account verification or password reset email from the application to confirm transactional delivery remains healthy.

---

## 7. Render Production Deployment Manual Verification Checklist

Because Render deployment credentials are held by the infrastructure owner, the operator must manually verify the live deployment:

- [ ] **1. Deployed Commit Verification**: Confirm in Render Dashboard that the active deployment of the web service and worker service is at or past commit `76f0998` (or includes `0031_cancel_pending_outreach.py`).
- [ ] **2. Environment Variables**: Confirm environment variables on Render:
  - `OUTREACH_ENABLED` = `0`
  - `OUTREACH_DAILY_SEND_CAP` = `0`
- [ ] **3. Worker Service Verification**: Check Render background worker logs. Verify log line `Cold outreach is disabled; daily drain skipped.` appears during scheduled run time (`07:37 UTC`).
- [ ] **4. Queue State Verification**: Run `python manage.py shell` on production or inspect Superadmin Client Finder UI: confirm `pending_total` is `0` and `cancelled_total` reflects cancelled historical items.
- [ ] **5. Log Inspection**: Search production logs (Datadog/Render logs) for `Client outreach:` or `send_company_outreach_task`. Confirm zero outgoing outreach SMTP attempts have occurred since September 9, 2026 01:40:03 +0300.

---

## 8. Data-Retention Classification & Cleanup Plan

> [!IMPORTANT]
> Specific retention periods for historical outreach and engagement logs require formal legal/privacy policy approval. The system retains only the minimum identifier required for do-not-contact suppression.

| Data Category | Stored Location / Model | Retention Classification | Retention Plan & Policy Status |
|---|---|---|---|
| **Suppression Records** | `OutreachSuppression` (`email`, `created_at`) | **Required for Suppression** | Retains minimum normalized email address technically necessary to prevent re-contact. |
| **Outreach Audit Trail** | `CompanyOutreach` (`sent_to`, `sent_at`, `status`, `error_message`) | **Requires Legal/Privacy Approval** | Retained in inert `cancelled`/`sent` state; final retention window subject to legal policy review. |
| **Engagement Logs** | `EmailEngagementEvent` (`payload`, `event_type`, `tag`) | **Requires Legal/Privacy Approval** | Aggregated webhook logs retained for security/bounce auditing; retention window subject to legal policy review. |
| **Email Templates** | `templates/emails/client_outreach.html`, `.txt` | **Requires Legal/Privacy Approval** | Inert template code retained in repository history; unhooked from execution. |
| **Public GEMI Emails** | `Company.email` | **Requires Legal/Privacy Approval** | Public directory data imported from official GEMI open data API; final retention policy subject to legal review. |

---

## 9. Product & UI Copy Audit Findings

A complete audit of public-facing and administrative interface copy was performed:

1. **`templates/superadmin/client_finder/list.html`**: Contains administrative controls for historical outreach. **Updated**: Displays explicit red warning banner ("Οι αποστολές cold outreach έχουν διακοπεί..."), disables send buttons, and displays cancelled count stat card.
2. **`templates/home.html`**: Describes platform features ("CSV export για άμεση επικοινωνία", "στην επικοινωνία σου"). Clarified: All references describe *subscribers* performing their own authorized outreach to GEMI businesses, not automated emailing by Gemi Leads.
3. **`templates/legal/terms.html`**: Section 6 ("Δική σου ευθύνη κατά την επικοινωνία") explicitly reinforces subscriber compliance responsibility under GDPR Article 21 and e-Privacy regulations.

---

## 10. Prerequisites Required Before Any Future Marketing-Email Capability Could Ever Be Re-evaluated

If the organization ever considers introducing opt-in marketing capabilities in the distant future, **ALL** of the following conditions must be satisfied prior to any code changes:

1. **Explicit Legal Opinion**: Written legal and GDPR compliance approval confirming compliance with e-Privacy Directive and Greek Data Protection Law (Law 4624/2019).
2. **Double Opt-In Mechanism**: Implementation of explicit, affirmative double opt-in consent prior to queueing any marketing communication.
3. **Dedicated Infrastructure**: Separation of marketing email IP pools and sender domains from transactional email infrastructure (`info@gemileads.gr`).
4. **Board & Management Authorization**: Formal executive sign-off and risk review.
