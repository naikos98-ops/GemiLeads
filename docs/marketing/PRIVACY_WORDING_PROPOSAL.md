# Privacy policy — proposed wording (REQUIRES OWNER / LEGAL APPROVAL)

**Status: proposal only. `templates/legal/privacy.html` has NOT been changed.** Apply only after NORVA Ι.Κ.Ε.
(the controller) or its legal advisor approves the text. Confirm the factual points marked ⚠ first.

## 1. Database host — section 5, providers table

**Current text (inaccurate):**

| Πάροχος | Ρόλος | Τι λαμβάνει |
|---|---|---|
| Render | Φιλοξενία εφαρμογής και βάσης | Όλα τα δεδομένα λογαριασμού |

**Facts:**
- Production data has lived in Supabase PostgreSQL since 2026-08-17 (commit `34ea38b`; see the AGENTS.md history of 2026-08-23).
- Render runs the application and background jobs. During processing they handle the same data.
- `render.yaml` declares both in Frankfurt / eu-central-1.
  - ⚠ The comment there says the Render service had to be re-created to change region. Confirm in the Render dashboard that the live service really is in Frankfurt.
  - ⚠ Confirm the Supabase project region (eu-central-1) and that a DPA is in place with both providers.

**Proposed text:**

| Πάροχος | Ρόλος | Τι λαμβάνει |
|---|---|---|
| Render | Φιλοξενία της εφαρμογής και των εργασιών παρασκηνίου | Όλα τα δεδομένα λογαριασμού, κατά την επεξεργασία τους |
| Supabase | Φιλοξενία της βάσης δεδομένων (PostgreSQL) | Όλα τα δεδομένα λογαριασμού |

Optional sentence after the table, only if the regions are confirmed:
> «Η εφαρμογή και η βάση δεδομένων φιλοξενούνται σε κέντρα δεδομένων στη Φρανκφούρτη (ΕΕ).»

Keep the existing paragraph on transfers outside the EEA (Standard Contractual Clauses); it remains correct for
providers headquartered in the US.

## 2. Google Analytics — «Ανώνυμα στατιστικά»

**Why it's inaccurate:**
- GA4 sets first-party cookies (`_ga`, `_ga_<id>`) holding a random client identifier, and links a visitor's hits through that identifier.
- That is **pseudonymous** data, which is personal data under GDPR. It is not anonymous.
- What *is* true:
  - nothing loads without consent;
  - Gemi Leads sends no name, email, ΑΦΜ, search text or other directly identifying data (enforced in `gemiapp/campaigns.py`);
  - consent can be withdrawn at any time.

**Proposed text — providers table row:**

| Google Analytics (Google Ireland Ltd) | Στατιστικά επισκεψιμότητας | Ψευδωνυμοποιημένο αναγνωριστικό συσκευής (cookie), σελίδες που επισκέφθηκες, πηγή επίσκεψης (π.χ. καμπάνια) και ενέργειες όπως η εγγραφή — μόνο με τη συγκατάθεσή σου |

**Proposed text — section 4, «Στατιστικών» bullet:**
> «**Στατιστικών.** Cookies του Google Analytics που μετρούν την επισκεψιμότητα μέσω ενός ψευδωνυμοποιημένου
> αναγνωριστικού. Δεν στέλνουμε στο Google όνομα, email, ΑΦΜ ή ό,τι πληκτρολογείς στην αναζήτηση.
> **Δεν φορτώνονται καθόλου** αν δεν πατήσεις «Αποδοχή». Αν επιλέξεις «Απόρριψη», δεν φορτώνεται κανένας κώδικας
> τρίτου μέρους. Μπορείς να ανακαλέσεις τη συγκατάθεσή σου οποιαδήποτε στιγμή από τις «Ρυθμίσεις cookies».»

⚠ Before publishing, confirm these in the GA4 admin:
- Google signals is **off**, and ad personalisation is off.
- Data retention is set to 14 months (or the period the owner chooses), and that period is stated in the policy if required.
- The GA4 entity named in Google's terms for this account.

## 3. Also noticed (not in scope, for the owner's attention)

- Google sign-in (and Apple, if enabled) is not listed as a recipient in the providers table, although it receives
  the login request.
- The policy should name the campaign attribution parameters (`utm_*`) if campaigns run. They are read from the URL
  only and sent to GA4 only with consent.
