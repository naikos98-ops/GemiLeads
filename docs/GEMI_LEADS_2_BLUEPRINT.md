# GEMI LEADS 2.0
## Business Signals & Sales Opportunities Platform
### Master Blueprint — Από το Α έως το Ω

---

# 0. ΤΟ ΤΕΛΙΚΟ ΠΡΟΪΟΝ

Το Gemi Leads δεν πρέπει να είναι:

**«Μία βάση με καινούργιες εταιρείες από το ΓΕΜΗ.»**

Πρέπει να γίνει:

**«Ένα σύστημα που παρακολουθεί την ελληνική επιχειρηματική δραστηριότητα, εντοπίζει γεγονότα που μπορούν να δημιουργήσουν εμπορικές ανάγκες και τα μετατρέπει σε στοχευμένες sales opportunities για κάθε πελάτη.»**

Η βασική αλυσίδα:

**ΓΕΜΗ → Companies → History → Changes → Signals → Matching → Scoring → Opportunities → Notifications → Sales Action → Outcome**

---

# 1. ΚΛΕΙΔΩΝΟΥΜΕ ΤΙ ΑΚΡΙΒΩΣ ΕΙΝΑΙ ΕΝΑ SIGNAL

Signal = ένα πραγματικό εταιρικό γεγονός που μπορεί να έχει εμπορική σημασία.

Πρώτη taxonomy:

### Tier 1 — Structured / High Confidence

Αυτά πρέπει να προκύπτουν deterministic από δεδομένα:

- `NEW_COMPANY`
- `STATUS_CHANGED`
- `KAD_ADDED`
- `KAD_REMOVED`
- `LEGAL_FORM_CHANGED`
- `LOCATION_CHANGED`, εφόσον το αντίστοιχο πεδίο παρέχεται αξιόπιστα από την πηγή.

### Tier 2 — Announcement-derived

Προκύπτουν από ανακοινώσεις/δημοσιεύσεις:

- `CAPITAL_INCREASE`
- `CAPITAL_DECREASE`
- `NEW_BRANCH`
- `BRANCH_CLOSED`
- `MANAGEMENT_CHANGE`
- `MERGER`
- `DISSOLUTION`
- `COMPANY_TRANSFORMATION`
- `OTHER_CORPORATE_EVENT`

Αυτά **δεν χαρακτηρίζονται αυτόματα 100% βέβαια**.

Κάθε τέτοιο signal θα έχει:

`confidence = HIGH / MEDIUM / LOW`

και σύνδεση προς την πρωτογενή πηγή.

Το Open Data ΓΕΜΗ περιλαμβάνει μεταξύ άλλων έγγραφα δημοσιότητας, αποφάσεις και αρχεία ανακοινώσεων με ημερομηνίες και θέματα, άρα υπάρχει διαθέσιμη βάση για αυτό το δεύτερο επίπεδο signals.

---

# 2. ΝΟΜΙΚΟ & COMPLIANCE FOUNDATION

Αυτό πρέπει να γίνει **πριν ενεργοποιηθεί οποιοδήποτε outreach automation**.

## 2.1 GEMI / Open Data

Να καταγραφούν:

- πηγή δεδομένων,
- ημερομηνία ανάκτησης,
- API endpoint/source type,
- attribution,
- raw response όπου χρειάζεται,
- έκδοση/parser version.

Η χρήση API και Open Data ΓΕΜΗ παρέχεται υπό ODC-BY-1.0.

Στο Terms/Attribution του προϊόντος πρέπει να υπάρχει η απαιτούμενη αναφορά στην πηγή.

---

## 2.2 Email

Δεν κατασκευάζουμε λειτουργία:

**«Βρήκα νέο lead → στείλε αυτόματα διαφημιστικό email.»**

Για direct marketing μέσω email/SMS κ.λπ. ισχύει κατά κανόνα προηγούμενη συγκατάθεση, με συγκεκριμένες εξαιρέσεις όπως προηγούμενη συναλλακτική σχέση.

Στο data model κάθε prospect email πρέπει προαιρετικά να μπορεί να έχει:

`email_marketing_status`

με:

- `UNKNOWN`
- `CONSENT`
- `EXISTING_RELATIONSHIP`
- `REQUESTED_FOLLOWUP`
- `DO_NOT_EMAIL`

και:

- lawful basis note
- consent/source timestamp
- source
- unsubscribe/objection timestamp.

**Default για email που απλώς βρέθηκε δημόσια: `UNKNOWN`.**

Το σύστημα δεν πρέπει να επιτρέπει promotional bulk send όταν η κατάσταση είναι `UNKNOWN`.

---

## 2.3 Personal data από δημόσιες πηγές

Αν αποθηκεύονται δεδομένα φυσικών προσώπων που προέρχονται από άλλη πηγή, πρέπει να εξεταστούν και οι υποχρεώσεις ενημέρωσης, μεταξύ άλλων κατά το άρθρο 14 GDPR. Η ίδια η ΑΠΔΠΧ επισημαίνει ότι όταν δεδομένα προέρχονται από άλλη νόμιμη πηγή, πρέπει να παρέχεται η προβλεπόμενη ενημέρωση και τρόπος εναντίωσης.

Πριν το production:

**νομικός/DPO → review privacy architecture → documented decision.**

---

# 3. ΑΝΘΡΩΠΙΝΕΣ ΤΗΛΕΦΩΝΙΚΕΣ ΚΛΗΣΕΙΣ

Αν το Gemi Leads υποστηρίξει sales calls, δημιουργείται ξεχωριστό compliance subsystem.

Η ΑΠΔΠΧ αναφέρει ότι για ανθρώπινες προωθητικές κλήσεις λειτουργούν μητρώα opt-out του άρθρου 11 στους τηλεπικοινωνιακούς παρόχους και ο διαφημιζόμενος πρέπει να λαμβάνει υπόψη επικαιροποιημένα μητρώα.

Χρειαζόμαστε:

`PhoneCompliance`

με:

```text
phone
provider
checked_at
registry_version
status
reason
```

Statuses:

```text
ELIGIBLE
BLOCKED
UNKNOWN
STALE
MANUAL_REVIEW
```

Επιπλέον:

`InternalDoNotCall`

για κάθε customer/workspace.

Αν κάποιος πει:

**«Μη με ξανακαλέσετε.»**

→ άμεσο block.

Αν τα compliance δεδομένα είναι παλιά/άγνωστα:

→ **δεν εμφανίζουμε πράσινο “safe to call”.**

---

# 4. ΑΠΟΚΤΗΣΗ ΕΠΙΣΗΜΟΥ GEMI API KEY

Production checklist:

- υποβολή αιτήματος Open Data ΓΕΜΗ,
- λήψη προσωπικού `api_key`,
- αποθήκευση σε secret manager,
- ποτέ στο frontend,
- ποτέ GitHub,
- ποτέ client-side request,
- staging/test configuration,
- production configuration,
- request monitoring,
- retry policy,
- rate-limit handling.

Η επίσημη τεχνική τεκμηρίωση αναφέρει ότι το δοκιμαστικό `api-docs-key` αφορά το test περιβάλλον και ότι απαιτείται προσωπικό `api_key` για πραγματική πρόσβαση.

---

# 5. ΑΡΧΙΤΕΚΤΟΝΙΚΗ

Προτεινόμενα logical components:

```text
GEMI API
   ↓
Collector
   ↓
Normalizer
   ↓
PostgreSQL
   ↓
Snapshot Engine
   ↓
Change Detector
   ↓
Signal Engine
   ↓
Matching Engine
   ↓
Opportunity Scoring
   ↓
API
   ↓
Web App / Notifications / Sales Actions
```

Παράλληλα:

```text
Announcements/Documents
        ↓
Document Pipeline
        ↓
Classification / Extraction
        ↓
Signal Engine
```

---

# 6. INFRASTRUCTURE

Χρειαζόμαστε τουλάχιστον:

- PostgreSQL
- application/backend API
- frontend
- worker service
- job scheduler
- queue
- object storage για αρχεία όπου επιτρέπεται/χρειάζεται
- secrets management
- logging
- monitoring
- staging environment
- production environment
- backups.

Για μικρό MVP η queue μπορεί να είναι DB-backed.

Σε μεγαλύτερη κλίμακα:

Redis / dedicated queue.

---

# 7. RAW SOURCE STORAGE

Πριν κάνουμε normalize οτιδήποτε, κρατάμε πληροφορία για την πηγή.

Table:

`source_records`

```text
id
source
source_entity_id
endpoint
payload_json
payload_hash
fetched_at
http_status
parser_version
```

Σκοπός:

αν αύριο αλλάξει parser ή schema, να μπορούμε να επεξεργαστούμε ξανά τα δεδομένα χωρίς να ξανακαλέσουμε υποχρεωτικά το ΓΕΜΗ.

Η περίοδος retention των raw δεδομένων πρέπει να περάσει από legal/data-governance review.

---

# 8. CANONICAL COMPANY MODEL

Κεντρικό table:

`companies`

Ενδεικτικά:

```text
id
gemi_number
afm
name
distinctive_title

legal_form_code
status_code
gemi_service_code

registration_date

prefecture_code
municipality_code

first_seen_at
last_seen_at
last_synced_at

current_version
source_updated_at

created_at
updated_at
```

**Δεν δημιουργούμε πεδία επειδή «λογικά πρέπει να υπάρχουν».**

Αποθηκεύουμε μόνο όσα πραγματικά έχουμε από την πηγή.

---

# 9. ΚΑΔ

Table:

`company_activities`

```text
id
company_id
kad_code
is_primary
first_seen_at
last_seen_at
active
```

Ξεχωριστό:

`kad_dictionary`

```text
code
description
parent_code
level
active
```

Έτσι επιτρέπουμε:

- exact ΚΑΔ
- κατηγορία ΚΑΔ
- parent ΚΑΔ
- groups
- industry templates.

---

# 10. PARAMETRIC DATA

Να δημιουργηθεί local cache για:

- νομικές μορφές
- νομούς
- δήμους
- statuses
- υπηρεσίες ΓΕΜΗ
- τύπους οργάνων
- τύπους εγγράφων
- τύπους αποφάσεων.

Το ίδιο το ΓΕΜΗ προτείνει local αποθήκευση και περιοδική ανανέωση αυτών των παραμετρικών δεδομένων, επειδή μεταβάλλονται σπάνια.

Job:

`sync_reference_data`

π.χ. εβδομαδιαία.

---

# 11. INITIAL IMPORT

Πρώτα κάνουμε bootstrap.

Δεν δημιουργούμε signals ακόμα.

Βήματα:

**Search → paginate → company IDs → detail fetch → normalize → store.**

Στόχος:

να αποκτήσουμε baseline.

---

# 12. ΠΟΛΥ ΣΗΜΑΝΤΙΚΟ: BASELINE ≠ HISTORY

Αν στις 15 Σεπτεμβρίου ξεκινήσουμε snapshots:

γνωρίζουμε τι ισχύει στις 15 Σεπτεμβρίου.

Δεν μπορούμε να ισχυριστούμε ότι ξέρουμε με βεβαιότητα όλες τις μεταβολές που έγιναν στις 10 Σεπτεμβρίου μόνο επειδή έχουμε το σημερινό state.

Ιστορικά γεγονότα μπορούν να αντλούνται όπου υπάρχει σχετική δημοσιευμένη ανακοίνωση/ημερομηνία.

Αλλά:

**snapshot-based historical tracking ξεκινά από τη στιγμή ενεργοποίησης της πλατφόρμας.**

---

# 13. COMPANY SNAPSHOTS

Table:

`company_snapshots`

```text
id
company_id
normalized_payload
normalized_hash
snapshot_at
source_record_id
```

Δεν χρειάζεται απαραίτητα νέο snapshot κάθε φορά.

Flow:

```text
fetch
↓
normalize
↓
hash
↓
compare with latest
↓
same → update last_checked
different → create snapshot
```

Αυτό μειώνει database size.

---

# 14. NORMALIZATION

Όλα τα δεδομένα πριν συγκριθούν περνούν normalization.

Παραδείγματα:

- trim strings
- consistent casing όπου επιτρέπεται
- normalize dates
- normalize ΚΑΔ
- sort unordered arrays
- normalize whitespace
- normalize Greek punctuation
- remove irrelevant ordering.

Σκοπός:

να μη δημιουργούμε ψεύτικο signal επειδή άλλαξε απλώς η σειρά ενός array.

---

# 15. CHANGE DETECTOR

Service:

`SignalDetector`

Input:

```text
previous snapshot
current snapshot
```

Output:

```text
zero or more detected changes
```

Παράδειγμα:

```text
previous KAD:
62.01

current:
62.01
73.11
```

→

`KAD_ADDED: 73.11`

---

# 16. SIGNAL DATA MODEL

Table:

`signals`

```text
id
company_id

type
category

detected_at
effective_at

source_type
source_id

old_value
new_value

confidence
confidence_score

dedupe_key

metadata_json

parser_version
rule_version

created_at
```

---

# 17. IDEMPOTENCY

Απαγορεύονται διπλά signals.

Δημιουργούμε deterministic `dedupe_key`.

Παράδειγμα:

```text
companyId
+
signalType
+
effectiveDate
+
normalizedValue
```

Unique constraint στο database.

Αν job ξανατρέξει:

**δεν ξαναδημιουργείται signal.**

---

# 18. SIGNAL CONFIDENCE

### 100

Direct structured comparison.

### 90+

Explicit structured announcement metadata.

### 70–89

Rule-based document extraction.

### 50–69

AI-assisted classification.

### <50

Δεν εμφανίζεται σαν verified signal.

Μπορεί να μπει:

`needs_review`.

---

# 19. ANNOUNCEMENT INGESTION

Ξεχωριστός pipeline:

```text
Company
↓
Announcement list
↓
Detect unseen announcement
↓
Store metadata
↓
Download/read if permitted and required
↓
Extract
↓
Classify
↓
Generate signal
```

Table:

`company_announcements`

```text
id
company_id
source_identifier
title
decision_date
announcement_date
registration_completed_at
document_reference
source_hash
processed_at
```

---

# 20. DOCUMENT CLASSIFICATION

Πρώτο layer:

rules / keywords / document type.

Δεύτερο:

LLM classifier.

Input:

- title
- announcement type
- relevant text
- metadata.

Output STRICT JSON:

```text
event_type
effective_date
entities
values
confidence
evidence
```

Το AI δεν γράφει απευθείας στη βάση.

Πρώτα:

schema validation → confidence checks → signal service.

---

# 21. HUMAN REVIEW QUEUE

Για αβέβαια events:

Admin:

**Signals → Needs Review**

Βλέπει:

- εταιρεία
- original announcement
- extracted event
- evidence
- confidence.

Actions:

**Approve / Correct / Reject**

Οι διορθώσεις χρησιμοποιούνται αργότερα για βελτίωση rules/prompts.

---

# 22. USER / ORGANIZATION ARCHITECTURE

Το SaaS πρέπει να είναι multi-tenant.

Tables:

`organizations`

`users`

`organization_members`

Roles:

- OWNER
- ADMIN
- SALES_MANAGER
- SALES_USER
- VIEWER

Όλα τα customer-specific records:

`organization_id`

---

# 23. CUSTOMER BUSINESS PROFILE

Κατά το onboarding:

**Τι πουλάς;**

Παράδειγμα:

```text
Business:
Insurance Broker

Products:
Vehicle insurance
Business insurance

Target:
Transport
Delivery
Restaurants

Location:
Attica
```

Table:

`organization_profiles`

---

# 24. IDEAL CUSTOMER PROFILE

Κάθε customer δημιουργεί ICP.

Παράμετροι:

- ΚΑΔ
- industry groups
- geographical areas
- legal forms
- business status
- company age
- signal types
- exclusions
- priorities.

---

# 25. RADAR

Radar = stored search + opportunity rules.

Table:

`radars`

```text
id
organization_id
name
active

score_threshold

created_at
```

Supporting:

`radar_kads`

`radar_regions`

`radar_legal_forms`

`radar_signal_types`

`radar_exclusions`

---

# 26. ONBOARDING WIZARD

Η πρώτη εμπειρία:

### Step 1
Τι πουλάς;

### Step 2
Σε ποιους;

### Step 3
Πού;

### Step 4
Ποια events σε ενδιαφέρουν;

### Step 5
Πόσο αυστηρό targeting;

### Step 6
Notification frequency.

Και δημιουργούμε αυτόματα πρώτο Radar.

---

# 27. INDUSTRY TEMPLATES

Για να μη χρειάζεται να ξέρει κάποιος ΚΑΔ.

Παράδειγμα:

**Insurance Agent**

templates:

- transport
- restaurants
- construction
- retail.

**Digital Agency**

- hospitality
- restaurants
- beauty
- fitness
- professional services.

**POS / Payments**

- retail
- restaurants
- hospitality.

Template:

→ αντιστοίχιση σε ΚΑΔ groups.

---

# 28. MATCHING ENGINE

Για κάθε νέο signal:

```text
signal
↓
company
↓
eligible active radars
↓
filters
↓
match / no match
```

Δεν κάνουμε loop σε όλους τους χρήστες αν μεγαλώσει το σύστημα.

Χρησιμοποιούμε indexed criteria / candidate selection.

---

# 29. OPPORTUNITY

Signal ≠ Opportunity.

Μία αύξηση κεφαλαίου είναι Signal.

Γίνεται Opportunity μόνο αν ενδιαφέρει συγκεκριμένο customer.

Table:

`opportunities`

```text
id
organization_id
radar_id
company_id
signal_id

score

status

reason
score_breakdown

created_at
expires_at
```

---

# 30. OPPORTUNITY SCORING

Δεν ξεκινάμε με AI.

Το scoring πρέπει να είναι explainable.

Παράδειγμα:

```text
Industry fit           25
Signal relevance       25
Geographic fit         15
Freshness              15
Legal form             5
Available contact      5
Company characteristics 10
```

Total:

`0–100`

---

# 31. SCORE CLASSES

```text
90–100  🔥 Priority
75–89   High
55–74   Medium
0–54    Low
```

Ο customer μπορεί αργότερα να αλλάζει thresholds.

---

# 32. SCORE BREAKDOWN

Πάντα εμφανίζεται το γιατί.

Παράδειγμα:

**92/100**

```text
+25 Exact industry
+25 New company signal
+15 Attica
+15 Detected today
+7 Contactability
+5 Legal form
```

Όχι black-box:

> AI thinks this is good.

---

# 33. FRESHNESS DECAY

Ένα signal χάνει αξία.

Παράδειγμα:

```text
0–24h       100%
1–3 days     90%
4–7 days     75%
8–14 days    50%
15–30 days   25%
```

Τα ακριβή weights πρέπει να είναι configurable.

---

# 34. DUPLICATE OPPORTUNITIES

Μία εταιρεία μπορεί να παράγει 4 signals.

Δεν θέλουμε spam:

```text
Company X

New Company
KAD Added
Announcement
Capital increase
```

Opportunity Aggregator:

> Company X — 3 relevant signals.

και μόνο ένα company card.

---

# 35. OPPORTUNITY FEED

Κεντρική οθόνη:

# Today's Opportunities

Filters:

- Priority
- Signal
- ΚΑΔ
- περιοχή
- score
- ημερομηνία
- Radar
- κατάσταση.

Sort:

**highest score + freshest.**

---

# 36. COMPANY PROFILE

Κάθε company page:

### Header

Επωνυμία  
διακριτικός τίτλος  
status  
ΓΕΜΗ  
νομική μορφή.

### Why this lead

Τα matching reasons.

### Current information

Ό,τι διαθέτει νόμιμα/αξιόπιστα η πηγή.

### Timeline

```text
15 Sep   New KAD
11 Sep   Announcement
2 Sep    Company registered
```

### Opportunities

Γιατί είναι σχετική με τον customer.

### Actions

Save  
Assign  
Call workflow  
Follow-up  
Dismiss.

---

# 37. COMPANY TIMELINE

Αυτό μπορεί να γίνει ένα από τα σημαντικότερα competitive advantages.

Αντί:

> εταιρεία = row.

Έχουμε:

> εταιρεία = evolving entity.

Timeline από:

- signals
- announcements
- verified changes.

---

# 38. CONTACT DATA

Contacts χρειάζονται ξεχωριστό model.

`company_contacts`

```text
company_id
type
value
source
first_seen_at
last_verified_at
confidence
```

Types:

- PHONE
- BUSINESS_EMAIL
- WEBSITE
- OTHER.

Δεν συγχέουμε:

**δημόσια διαθέσιμο**

με

**επιτρέπεται marketing.**

---

# 39. SALES ACTION STATES

Opportunity pipeline:

```text
NEW
VIEWED
SAVED
ASSIGNED
CONTACTED
INTERESTED
FOLLOW_UP
WON
LOST
NOT_RELEVANT
DO_NOT_CONTACT
```

Αυτό αρκεί για CRM-lite.

Δεν χρειάζεται να χτίσουμε Salesforce.

---

# 40. ASSIGNMENT

Teams:

Sales Manager → assign lead → salesperson.

Fields:

```text
assigned_to
assigned_at
```

Activity:

> Nikos assigned Company X to Maria.

---

# 41. NOTES

`opportunity_notes`

Προσωπικές/team notes.

Πάντα tenant-isolated.

---

# 42. TASKS

Απλό:

```text
Call tomorrow
Follow up Friday
Check again next week
```

Table:

`tasks`

Δεν χτίζουμε full project management system.

---

# 43. AI — ΜΟΝΟ ΑΦΟΥ ΔΟΥΛΕΨΟΥΝ ΤΑ ΠΑΡΑΠΑΝΩ

AI functions:

### Why this lead?

Μεταφράζει score + signal σε φυσική γλώσσα.

### Possible needs

Δεν λέει:

> «Η εταιρεία χρειάζεται website.»

Λέει:

> «Με βάση το ότι πρόκειται για νέα επιχείρηση στον Χ κλάδο, πιθανές σχετικές ανάγκες μπορεί να είναι…»

### Prepare Call

Δημιουργεί contextual sales opener.

### Company Brief

Πριν το call:

```text
Who they are
What happened
Why now
Possible needs
Questions to ask
```

---

# 44. AI GUARDRAILS

Το model δεν επιτρέπεται:

- να εφευρίσκει revenue
- να εφευρίσκει αριθμό εργαζομένων
- να λέει ότι κάποιος «χρειάζεται» υπηρεσία χωρίς evidence
- να ισχυρίζεται ότι μία πληροφορία υπάρχει στο ΓΕΜΗ αν δεν υπάρχει.

Prompt structure:

**SOURCE FACTS**

και ξεχωριστά:

**INFERENCE.**

---

# 45. AI OUTPUT CACHE

Δεν πληρώνουμε LLM κάθε page load.

`ai_insights`

με:

```text
company_id
organization_id
opportunity_id
type
input_hash
output
model
created_at
```

Αν δεν άλλαξε input:

return cache.

---

# 46. DAILY DIGEST

Ο customer επιλέγει:

- instant
- morning
- daily
- weekly.

Παράδειγμα:

> 14 νέες ευκαιρίες  
> 4 Priority  
> 7 High  
> 3 Medium

Αυτό είναι **notification προς τον πελάτη του Gemi Leads**, όχι marketing προς το prospect.

---

# 47. REAL-TIME ALERTS

Για premium:

> 🔥 New Priority Lead

Όχι «real-time» στο marketing αν τεχνικά polling γίνεται π.χ. κάθε 6 ώρες.

Χρησιμοποιούμε ακριβές claim:

> monitored daily

ή

> updated multiple times daily

ανάλογα με το πραγματικό system cadence.

---

# 48. IN-APP NOTIFICATIONS

`notifications`

Types:

```text
NEW_OPPORTUNITY
PRIORITY_SIGNAL
RADAR_MATCH
TASK_DUE
ASSIGNMENT
```

Unread counter.

---

# 49. FOLLOW-UP EMAIL

Αν prospect πει:

> «Στείλτε μου πληροφορίες.»

Salesperson:

`Mark → Requested Follow-up`

και τότε:

**Generate Follow-up**

AI draft:

> Σε συνέχεια της τηλεφωνικής μας επικοινωνίας...

Το system αποθηκεύει context ότι το email ακολουθεί προηγούμενη επαφή.

---

# 50. BULK COLD EMAIL

Δεν αποτελεί core feature.

Δεν προσθέτουμε:

**Select 500 → Send Campaign**

πάνω σε δημόσια scraped/GEMI emails.

Το default product workflow πρέπει να είναι compliant-by-design.

---

# 51. DO NOT CONTACT

Global για organization:

`contact_suppressions`

```text
organization_id
contact_type
contact_value
reason
created_at
source
```

Reason:

- explicit objection
- email unsubscribe
- call objection
- compliance registry
- manual.

Πρέπει να υπερισχύει οποιουδήποτε AI/Radar.

---

# 52. AUDIT LOG

Κάθε σημαντική ενέργεια:

```text
actor
organization
action
entity
timestamp
metadata
```

Παραδείγματα:

- prospect contacted
- suppression added
- Radar changed
- signal manually corrected
- opportunity reassigned
- compliance status checked.

---

# 53. API ENDPOINTS

Ενδεικτικά:

```text
GET /companies
GET /companies/:id

GET /signals
GET /signals/:id

GET /opportunities
GET /opportunities/:id

POST /radars
PATCH /radars/:id
DELETE /radars/:id

POST /opportunities/:id/assign
POST /opportunities/:id/status
POST /opportunities/:id/note

POST /ai/opportunity-brief
POST /ai/call-prep

GET /notifications

POST /suppressions
```

Internal:

```text
POST /internal/gemi/sync
POST /internal/signals/process
POST /internal/matching/process
```

με authentication που δεν είναι user-facing.

---

# 54. BACKGROUND JOBS

Χρειαζόμαστε τουλάχιστον:

```text
sync_reference_data
discover_companies
sync_company_details
sync_company_announcements
create_snapshots
detect_changes
classify_announcements
generate_signals
match_signals
recalculate_scores
send_digests
refresh_compliance_data
cleanup_expired_data
health_check
```

---

# 55. JOB IDEMPOTENCY

Κάθε job πρέπει να μπορεί να τρέξει δύο φορές χωρίς corruption.

Π.χ.:

GEMI timeout → retry.

Δεν πρέπει να έχουμε:

- duplicate company
- duplicate snapshot
- duplicate signal
- duplicate opportunity
- duplicate notification.

---

# 56. RETRIES

Retry μόνο σε recoverable errors.

Παράδειγμα:

```text
429
5xx
network timeout
```

Exponential backoff.

Όχι infinite retry.

Dead-letter queue / failed_jobs table.

---

# 57. API RATE CONTROL

Επειδή τα API resources είναι πεπερασμένα:

- concurrency limit
- request queue
- caching
- backoff
- priority sync.

Priority:

1. νέες εταιρείες
2. companies relevant to active Radars
3. companies with recent signals
4. cold historical records.

---

# 58. SMART MONITORING FREQUENCY

Δεν χρειάζεται ίδια συχνότητα για όλες τις εταιρείες.

Νέα/relevant:

πιο συχνά.

Παλιές/irrelevant:

λιγότερο.

Αυτό μειώνει:

- API usage
- compute
- database writes.

---

# 59. HISTORICAL BACKFILL

Χτίζουμε ξεχωριστό worker.

Προτεραιότητα:

- active companies
- target ΚΑΔ
- target regions.

Δεν μπλοκάρουμε live ingestion περιμένοντας να ολοκληρωθεί full history.

---

# 60. SEARCH

Company search:

- name
- GEMI number
- AFM where appropriate
- ΚΑΔ
- region
- signal
- dates.

Indexes από την αρχή.

---

# 61. DATABASE INDEXES

Τουλάχιστον:

```text
companies.gemi_number UNIQUE
companies.afm
companies.status
companies.registration_date

company_activities.kad_code

signals.company_id
signals.type
signals.detected_at

opportunities.organization_id
opportunities.score
opportunities.status

radars.organization_id
```

Composite indexes μετά από πραγματικό query analysis.

---

# 62. TENANT SECURITY

Κανένας customer δεν βλέπει:

- Radar άλλου
- opportunities άλλου
- notes άλλου
- call history άλλου
- suppression lists άλλου.

Authorization enforced στο backend.

Όχι μόνο frontend hiding.

---

# 63. AUTHENTICATION

- secure sessions / tokens
- verified email
- password reset
- MFA option για admins
- session revoke
- brute-force protection.

---

# 64. RBAC

OWNER:

όλα.

ADMIN:

organization management.

SALES_MANAGER:

team/leads.

SALES_USER:

assigned/visible opportunities.

VIEWER:

read only.

---

# 65. SECRETS

Ποτέ:

```text
GEMI_API_KEY
LLM_KEY
EMAIL_PROVIDER_KEY
DATABASE_PASSWORD
```

σε:

- Git
- frontend
- logs.

---

# 66. ENCRYPTION & TRANSPORT

- HTTPS
- encrypted provider/database storage
- encrypted backups
- secure secret storage.

Για ιδιαίτερα ευαίσθητα operational credentials, field/application-level encryption όπου χρειάζεται.

---

# 67. DATA RETENTION

Policy ανά category:

- raw API responses
- snapshots
- signals
- contacts
- logs
- AI prompts
- deleted users
- billing.

Δεν κρατάμε δεδομένα «για πάντα επειδή μπορεί να χρειαστούν».

Legal review → documented retention matrix.

---

# 68. DATA DELETION

Workflows:

- delete account
- delete organization
- personal data request
- suppress contact
- remove manually entered contact.

Deletion πρέπει να διαδίδεται όπου απαιτείται σε dependent systems.

---

# 69. PRIVACY DOCUMENTATION

Πριν production:

- Privacy Policy
- Terms of Service
- Cookie Policy όπου χρειάζεται
- Data Processing Agreement για B2B customers όπου απαιτείται
- subprocessors list
- retention policy
- security documentation
- acceptable use policy.

---

# 70. ACCEPTABLE USE POLICY

Customer συμφωνεί ότι δεν θα χρησιμοποιεί Gemi Leads για:

- unlawful spam
- harassment
- circumventing Do Not Contact
- illegal profiling
- unauthorized resale/export όπου απαγορεύεται
- misleading communications.

---

# 71. EXPORTS

CSV export μπορεί να υπάρχει.

Αλλά:

- plan limit
- audit log
- rate limit
- workspace ownership
- compliance warning.

Δεν αφήνουμε:

> Download entire Greek business database.

---

# 72. ADMIN PANEL

Internal Norva/Gemi Leads admin:

### Data

- companies
- snapshots
- signals
- announcements.

### Jobs

- running
- failed
- retries.

### Customers

- organizations
- subscriptions
- limits.

### Signals

- review queue.

### Compliance

- suppression incidents.

### System

- API health
- queue depth
- error rates.

---

# 73. DATA QUALITY DASHBOARD

Metrics:

```text
Companies synced today
New companies
Signals generated
Duplicate signals prevented
Failed fetches
Unprocessed announcements
Low confidence signals
```

---

# 74. OBSERVABILITY

Logs με:

```text
request_id
job_id
company_id
organization_id
```

Όχι προσωπικά δεδομένα χωρίς λόγο.

Monitoring:

- uptime
- error rate
- DB
- queue
- API failures
- notification failures.

---

# 75. ALERTING

Internal alert αν:

- GEMI API failure spike
- 0 companies discovered ασυνήθιστα
- 10× περισσότερα companies από normal
- queue stuck
- database near capacity
- signal detector produces abnormal volume.

---

# 76. SOURCE CHANGE DETECTION

Αν το ΓΕΜΗ αλλάξει response schema:

μην συνεχίζουμε και αποθηκεύουμε garbage.

Schema validation.

Αν required field αλλάξει:

job fails safely → alert.

---

# 77. FEATURE FLAGS

Advanced functionality πίσω από flags:

```text
ANNOUNCEMENT_AI
CAPITAL_SIGNALS
CALL_COMPLIANCE
AI_CALL_PREP
REALTIME_ALERTS
```

Μπορούμε να κάνουμε gradual rollout.

---

# 78. TEST ENVIRONMENT

Τρία layers:

### Development

Local.

### Staging

production-like.

### Production

real customers.

Δεν δοκιμάζουμε migrations απευθείας production.

---

# 79. UNIT TESTS

Critical:

- normalization
- diff
- deduplication
- scoring
- matching
- suppression
- permissions.

---

# 80. GOLDEN SIGNAL TESTS

Fixtures:

Previous snapshot + Current snapshot.

Expected:

```text
1 KAD_ADDED
0 other signals
```

Αν αλλάξει parser:

τρέχουν ξανά όλα.

---

# 81. MATCHING TESTS

Radar:

```text
Attica
Restaurants
NEW_COMPANY
```

Company:

```text
Attica
Restaurant
NEW_COMPANY
```

Expected:

MATCH.

Thessaloniki:

NO MATCH.

---

# 82. TENANT ISOLATION TEST

Organization A requests opportunity B.

Expected:

`404/403`

100% automated regression.

---

# 83. LOAD TEST

Πριν scale:

simulate:

- thousands company updates
- simultaneous Radars
- signal burst
- daily digest.

Παρακολουθούμε:

DB CPU  
query latency  
queue lag.

---

# 84. MIGRATIONS

Κάθε schema change:

migration.

Όχι manual production DB edits.

Migration history σε version control.

---

# 85. BACKUPS

Automatic backups.

Και σημαντικότερο:

**restore test.**

Backup που δεν έχει δοκιμαστεί ότι επαναφέρεται δεν θεωρείται πραγματικό backup.

---

# 86. BILLING

Plans based on πραγματική αξία.

Παράμετροι:

- active Radars
- regions
- signal types
- opportunities/month
- users
- notification frequency
- AI credits
- exports.

---

# 87. ΕΝΔΕΙΚΤΙΚΗ PRODUCT STRUCTURE

### Starter

New companies  
1–2 Radars  
basic filtering.

### Growth

Multiple Radars  
Signals  
scoring  
alerts  
AI briefing.

### Pro / Team

Multiple users  
assignments  
CRM-lite  
advanced signals  
exports  
team analytics.

Δεν χρειάζεται να κλειδώσουν τώρα οι τιμές.

---

# 88. SUBSCRIPTION LIMITS

Server-side enforcement.

Όχι:

frontend λέει «5 Radars» αλλά API επιτρέπει 500.

---

# 89. FREE TRIAL

Ιδανικά:

δείχνουμε πραγματική αξία.

Π.χ.:

> Βρήκαμε 37 εταιρείες που ταιριάζουν στα κριτήριά σου αυτή την εβδομάδα.

και περιορίζουμε:

- historical access
- exports
- advanced AI.

---

# 90. FIRST-VALUE EXPERIENCE

Στόχος onboarding:

ο customer σε λίγα λεπτά να δει:

**«Αυτές είναι οι επιχειρήσεις που θα κοιτούσα σήμερα.»**

Όχι άδειο dashboard.

---

# 91. VERTICAL PLAYBOOKS

Prebuilt setup για:

- ασφαλιστές
- digital agencies
- τηλεπικοινωνίες
- POS/payments
- B2B suppliers
- software/ERP
- commercial real estate
- professional services.

Κάθε vertical:

- recommended ΚΑΔ
- signals
- score weights
- call questions
- possible needs.

---

# 92. USER FEEDBACK LOOP

Κουμπιά:

👍 Good lead  
👎 Bad lead

και reason:

- wrong industry
- too small
- wrong region
- irrelevant signal
- already customer
- unreachable.

---

# 93. LEARNING FROM FEEDBACK

Αρχικά:

όχι autonomous ML.

Analytics:

> users dismiss KAD X 80% of the time.

Product team → adjust default template.

Αργότερα personalized scoring.

---

# 94. OUTCOME TRACKING

Για να ξέρουμε αν το Gemi Leads δουλεύει:

```text
opportunity
→ contacted
→ interested
→ won
```

Metrics:

- opportunities generated
- contact rate
- interest rate
- win rate.

---

# 95. ROI DASHBOARD

Premium future feature:

> 84 opportunities  
> 32 contacted  
> 8 interested  
> 3 won

Customer καταλαβαίνει την αξία της συνδρομής.

---

# 96. SIGNAL PERFORMANCE

Μετράμε ανά signal:

```text
NEW_COMPANY
→ 4.2% interest

CAPITAL_INCREASE
→ 11.6% interest
```

Μελλοντικά βελτιώνουμε scoring.

---

# 97. DATA PRODUCT ANALYTICS

Internal:

- ποιοι ΚΑΔ έχουν volume
- ποια regions
- ποια signals
- average freshness
- conversion.

Όχι μόνο GA page views.

---

# 98. MVP — ΤΙ ΧΤΙΖΟΥΜΕ ΠΡΩΤΟ

**MVP Core:**

1. Official GEMI API integration
2. reference-data sync
3. company ingestion
4. normalized companies
5. ΚΑΔ
6. baseline
7. snapshots
8. deterministic diff
9. `NEW_COMPANY`
10. `KAD_ADDED`
11. `KAD_REMOVED`
12. status changes
13. signal storage
14. organizations/users
15. Radar
16. matching
17. deterministic scoring
18. opportunities
19. opportunity feed
20. company profile
21. daily notifications
22. audit logging
23. admin health panel.

**Αυτό είναι το πραγματικό V1.**

---

# 99. V1.1

Μετά:

- team assignment
- notes
- statuses
- tasks
- suppression lists
- company timeline
- industry templates
- improved onboarding.

---

# 100. V1.2

Μετά:

- announcement ingestion
- document classification
- human review queue
- advanced corporate-event signals.

---

# 101. V1.3

Μετά:

- AI opportunity brief
- AI possible needs
- AI call preparation
- AI follow-up drafting.

---

# 102. V1.4

Μετά:

- proper phone-compliance workflows
- registry freshness handling
- call-action gating
- objection management.

---

# 103. V2

- advanced signal scoring
- user feedback personalization
- analytics
- ROI attribution
- CRM integrations
- webhook/API.

---

# 104. V3

Πιθανή επέκταση με εξωτερικές νόμιμες πηγές:

```text
GEMI
+
company website
+
other official/open datasets
+
customer's own CRM
```

Τότε το Gemi Leads γίνεται πραγματικό:

# Business Intelligence & Sales Signals Platform

και όχι απλώς «GEMI viewer».

---

# 105. ΤΙ ΔΕΝ ΧΤΙΖΟΥΜΕ ΣΤΗΝ ΑΡΧΗ

Όχι:

- full CRM
- advanced ML
- autonomous AI agent
- huge enrichment engine
- 50 signal types
- automatic mass cold email
- complex telephony
- mobile application.

Θα καθυστερήσουν το core.

---

# 106. CORE MOAT

Το moat δεν είναι:

**«Έχουμε δεδομένα ΓΕΜΗ.»**

Τα δεδομένα προέρχονται από δημόσια/ανοικτή πηγή.

Το moat πρέπει να είναι:

**Historical business graph + Signal detection + Customer-specific relevance + Timing + Scoring + Workflow.**

Με άλλα λόγια:

> ξέρουμε τι άλλαξε,
> πότε άλλαξε,
> γιατί έχει σημασία για εσένα,
> και ποια επιχείρηση αξίζει να κοιτάξεις πρώτη.

---

# 107. ΚΡΙΣΙΜΑ KPIs ΠΡΙΝ ΤΟ LAUNCH

Τεχνικά:

- ingestion reliability
- signal precision
- duplicate rate
- data freshness
- API failure rate
- notification reliability.

Product:

- Radar creation rate
- opportunities viewed
- save rate
- contacted rate
- dismissed rate.

Business:

- trial → paid
- churn
- activation
- weekly active accounts.

---

# 108. DEFINITION OF DONE ΓΙΑ SIGNAL

Ένα signal θεωρείται production-ready μόνο όταν:

- έχει source
- έχει timestamp
- έχει idempotency
- έχει tests
- έχει clear meaning
- έχει confidence logic
- έχει UI representation
- έχει matching behavior
- έχει scoring behavior
- μπορεί να auditαριστεί.

---

# 109. DEFINITION OF DONE ΓΙΑ FEATURE

Κάθε feature χρειάζεται:

- database migration
- backend
- authorization
- frontend
- loading state
- empty state
- error state
- audit behavior όπου απαιτείται
- tests
- analytics
- documentation
- rollback strategy.

---

# 110. PRODUCTION LAUNCH CHECKLIST

Πριν ανοίξει σε paying users:

- API key production
- GEMI attribution
- legal review
- Privacy Policy
- Terms
- backups
- restore test
- monitoring
- error alerts
- tenant-isolation test
- rate limits
- audit log
- billing
- subscription limits
- support email
- status/incident procedure
- suppression workflow
- logging reviewed για PII
- staged deployment
- smoke tests.

---

# 111. PILOT

Δεν ανοίγουμε αμέσως σε 1.000 χρήστες.

Pilot:

**5–15 πραγματικές επιχειρήσεις.**

Ιδανικά από διαφορετικούς κλάδους:

- ασφαλιστής
- agency
- POS
- B2B supplier
- software provider.

Για 2–4 εβδομάδες συλλέγουμε:

- relevant leads?
- irrelevant leads?
- missing filters?
- score quality?
- actual contacts?
- conversions?

---

# 112. PILOT FEEDBACK

Για κάθε opportunity:

```text
Relevant?
Contacted?
Response?
Outcome?
```

Αυτό θα είναι πολύ πιο πολύτιμο από το να μαντεύουμε weights.

---

# 113. SIGNAL PRECISION BEFORE SIGNAL QUANTITY

Καλύτερα:

> 8 πραγματικά χρήσιμες opportunities

παρά:

> 173 «AI opportunities».

Το προϊόν θα κριθεί από:

**signal-to-noise ratio.**

---

# 114. POSITIONING

Παλιό:

> Βρες νέες επιχειρήσεις από το ΓΕΜΗ.

Νέο:

> **Βρες ποια επιχείρηση μπορεί να χρειάζεται την υπηρεσία σου — τη στιγμή που δημιουργείται η ανάγκη.**

ή:

> **Turn Greek business activity into sales opportunities.**

---

# 115. ΚΕΝΤΡΙΚΗ UX ΛΟΓΙΚΗ

Ο customer δεν πρέπει να σκέφτεται:

**«Τι φίλτρα να βάλω στη βάση;»**

Πρέπει να σκέφτεται:

**«Σε ποιον πρέπει να μιλήσω σήμερα;»**

Άρα homepage:

# Good morning.
## 12 new opportunities match your business.

και όχι:

# Database
## 1,238,921 companies.

---

# 116. ΤΟ ΠΡΑΓΜΑΤΙΚΟ FLOW ΤΟΥ ΧΡΗΣΤΗ

```text
Sign Up

↓
What do you sell?

↓
Who do you sell to?

↓
Where?

↓
Create Radar

↓
Gemi Leads monitors business activity

↓
Signal detected

↓
Radar matched

↓
Opportunity scored

↓
User notified

↓
User opens company

↓
Understands "why now"

↓
Prepares approach

↓
Contacts company

↓
Records outcome

↓
Gemi Leads learns which signals create value
```

---

# 117. ΤΟ ΠΡΑΓΜΑΤΙΚΟ FLOW ΤΟΥ BACKEND

```text
Scheduler

↓
GEMI Discovery

↓
Company IDs

↓
Fetch current company information

↓
Store source payload

↓
Normalize

↓
Compare hash

↓
Create snapshot if changed

↓
Diff snapshots

↓
Create deterministic signals

↓

Fetch unseen announcements

↓
Classify/extract

↓
Create verified/probabilistic signals

↓

Signal queue

↓
Find compatible Radars

↓
Apply filters

↓
Calculate score

↓
Create/update Opportunity

↓
Generate notification

↓
Customer action

↓
Outcome data
```

---

# 118. Η ΣΩΣΤΗ ΣΕΙΡΑ ΥΛΟΠΟΙΗΣΗΣ

Αν ξεκινούσαμε αύριο, η σειρά πρέπει να είναι **ακριβώς αυτή**:

### PHASE A — Data Foundation

1. GEMI production access.
2. Audit υπάρχοντος Gemi Leads.
3. Schema migration plan.
4. Raw source records.
5. Reference tables.
6. Company canonical model.
7. ΚΑΔ model.
8. GEMI collector.
9. normalization.
10. initial baseline.
11. indexes.
12. monitoring.

### PHASE B — Historical Engine

13. Snapshots.
14. hashes.
15. change detector.
16. deterministic signals.
17. deduplication.
18. signal tests.
19. company timeline.

### PHASE C — Customer Intelligence

20. Organization profile.
21. ICP.
22. Radar.
23. industry templates.
24. matching engine.
25. scoring.
26. score breakdown.
27. opportunity model.
28. opportunity feed.

### PHASE D — Workflow

29. company opportunity page.
30. save.
31. assign.
32. statuses.
33. notes.
34. tasks.
35. do-not-contact.
36. audit log.
37. notifications.

### PHASE E — Advanced Signals

38. announcement sync.
39. announcement fingerprints.
40. document extraction.
41. rule classifier.
42. AI classifier.
43. confidence engine.
44. human review.
45. advanced corporate signals.

### PHASE F — AI

46. opportunity explanation.
47. potential-needs analysis.
48. company brief.
49. call preparation.
50. follow-up drafting.
51. AI caching.
52. hallucination checks.

### PHASE G — Compliance

53. phone compliance infrastructure.
54. registry handling process.
55. freshness status.
56. call eligibility.
57. email status/basis.
58. suppression enforcement.
59. privacy review.
60. retention enforcement.

### PHASE H — Commercial SaaS

61. billing.
62. plan limits.
63. trial.
64. onboarding.
65. analytics.
66. admin.
67. support.
68. security review.
69. production checklist.

### PHASE I — Pilot

70. 5–15 pilot customers.
71. collect relevance data.
72. adjust scoring.
73. remove noisy signals.
74. improve templates.
75. measure activation/conversion.

### PHASE J — Public Launch

76. pricing.
77. website.
78. demo.
79. case studies.
80. sales.
81. monitoring.
82. weekly product review.

### PHASE K — Scale

83. optimize collector.
84. queue scaling.
85. caching.
86. database optimization.
87. advanced analytics.
88. integrations.
89. APIs/webhooks.
90. personalized ranking.

---

# 119. ΤΟ ΚΡΙΣΙΜΟ MVP CUT

Αν χρειαστεί να κόψουμε οτιδήποτε, **δεν κόβουμε**:

```text
GEMI
↓
Snapshot
↓
Signal
↓
Radar
↓
Match
↓
Score
↓
Opportunity
```

Αυτός είναι ο πυρήνας.

Όλα τα υπόλοιπα μπορούν να έρθουν μετά.

---

# 120. Η ΜΙΑ ΠΡΟΤΑΣΗ ΠΟΥ ΠΡΕΠΕΙ ΝΑ ΘΥΜΟΜΑΣΤΕ

Το Gemi Leads δεν πρέπει να πουλάει:

**δεδομένα.**

Πρέπει να πουλάει:

# TIMING.

Να λέει στον επιχειρηματία:

> **«Αυτή η εταιρεία έγινε πιθανός πελάτης σου τώρα — και αυτός είναι ο λόγος.»**

Αν καταφέρουμε να το κάνουμε αξιόπιστα, τότε το Gemi Leads παύει να είναι ένα interface πάνω από το ΓΕΜΗ και γίνεται πραγματικό B2B sales intelligence product.