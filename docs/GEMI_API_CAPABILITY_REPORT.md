# GEMI Open Data API — Capability Report

**Purpose:** establish, empirically, what the official ΓΕΜΗ Open Data API supports, so that the
Gemi Leads 2.0 collector is designed around the real source instead of assumed capabilities.
Tier-1 change detection must not be built before this report exists
(see `docs/GEMI_LEADS_2_BLUEPRINT.md`).

**Date of spike:** 2026-09-15 · **Performed by:** read-only capability spike (Claude Code)

## Method

| Source | What was used |
|---|---|
| Official OpenAPI spec | `GET https://opendata-api.businessportal.gr/api-docs` (Swagger 2.0, public, no key) |
| Official docs | `https://opendata.businessportal.gr/techdocs/`, `https://opendata.businessportal.gr/` |
| Live API | ~87 authenticated `GET` requests with the application's existing `GEMI_API_KEY`, in three runs |
| Code | `gemiapp/services.py` (current importer), `gemiapp/data/kad_2025.json` (app KAD catalogue) |

Safety constraints honoured:

- `GET` only, plus one `HEAD` (rejected with 405). No document file was downloaded.
- The API key was read from the environment and never printed, logged or written to disk. The
  "document URL carries our key" check was evaluated as a boolean inside the process.
- Natural-person entries (`persons`) were counted, never printed or stored.
- Raw probe output was kept in a session scratchpad, not in the repository.
- The first run exceeded the rate limit (28 × HTTP 429); probes were re-run at 8.2 s spacing.

## Legend

| Status | Meaning |
|---|---|
| **SUPPORTED** | Verified in the spec **and** observed in live responses |
| **NOT SUPPORTED** | Absent from the spec **and** empirically absent (e.g. parameter silently ignored, rejected) |
| **UNKNOWN** | Neither documented nor observable without unsafe testing; no number is invented |
| **REQUIRES WORKAROUND** | Not provided directly, but achievable from what the source does provide |

## Headline facts

- **Rate limit: 8 requests per minute per API key**, enforced by a Kong gateway
  (`X-RateLimit-Limit-Minute: 8`, `RateLimit-Limit: 8`, `RateLimit-Remaining`, `RateLimit-Reset`,
  and `Retry-After` on HTTP 429). No hourly or daily limit header was observed; a daily quota is
  **UNKNOWN**. Theoretical ceiling at a sustained 8/min: 11,520 requests/day.
- **There is no incremental mechanism.** No modified-since filter, no change feed, no cursor, no
  date-range filter, no sort by modification. Unknown query parameters are **silently ignored**
  (the result set is unchanged), which makes typos dangerous.
- **Search returns full company records.** A `/companies` search item is field-for-field identical
  to `/companies/{arGemi}` for the same company (verified on two companies), including `persons`,
  `capital`, `stocks`, `lastStatusChange` and activities with `dtFrom`/`dtTo` history. One search
  request returns up to 200 complete records.
- **Registry size:** 1,686,965 companies (no criteria), 1,051,939 active, 634,897 inactive.
- **Activities carry history.** Each activity has `type` (Κύρια / Δευτερεύουσα / Βοηθητική / Λοιπή),
  `dtFrom`, `dtTo` and `kadVersion` (`kad_2008` or `kad_2026`). A mass KAD reclassification
  (2008 codes ended, 2026 codes started, on **2026-03-01**) is present in company data.
- **Documents are per company only**, newest first, with coded decision subjects that map to
  `/metadata/assemblySubjects`.
- **Source data quality:** `incorporationDate` contains impossible values at both ends
  (`9011-12-09`, `5015-02-05`, `3006-03-03` … and `1821-01-01`). 27 of the 200 "newest" results
  sorted by `-incorporationDate` were future-dated.
- **Upstream instability:** HTTP 500 with `Connection terminated due to connection timeout` and
  `connect ECONNREFUSED …:5432` was observed, with latencies up to 30 s.

---

## 1. Access, operations and error behaviour

| Capability | Status | Evidence / notes |
|---|---|---|
| Authentication by personal key in `api_key` header | SUPPORTED | Spec `securityDefinitions.api_key` (header). No key → `401 {"message":"No API key found in request"}`. |
| Test key for docs environment | SUPPORTED (docs only) | Techdocs: `api-docs-key` for the Swagger test environment, IP-limited. Not used. |
| Health endpoint | SUPPORTED | `GET /health` → 200. |
| Structured JSON errors | REQUIRES WORKAROUND | Gateway errors are JSON (`{"message","request_id"}`); upstream errors are JSON arrays (`[{"id":"systemError","descr":…}]`); **validation errors are HTML with a stack trace** (`swagger-tools`). The client must handle all three. |
| Empty search result | REQUIRES WORKAROUND | A search with no matches returns **HTTP 404 with an empty body**, not `200` with `[]`. The current `_get()` raises on 404, so an empty search would fail an import. |
| Nonexistent company | SUPPORTED | `GET /companies/1` → 404. |
| Retry signalling | SUPPORTED | `Retry-After` (seconds) on 429; `RateLimit-Reset` on every response. |
| 5xx behaviour | REQUIRES WORKAROUND | 500s with database connection errors and 30 s latencies observed. Needs bounded retries with backoff and a failed-jobs record. |
| HTTP caching / conditional requests | NOT SUPPORTED | No `ETag`, `Last-Modified`, `Cache-Control` or `Expires` on company responses. |
| Licence and attribution | SUPPORTED (obligation) | ODC-BY-1.0. Attribution to «Κεντρική Υπηρεσία ΓΕΜΗ» and «Κεντρική Ένωση Επιμελητηρίων Ελλάδος». |
| Bulk download / data dump | NOT SUPPORTED | Techdocs and portal describe API access only. |

## 2. Rate limits, quotas, concurrency

| Capability | Status | Evidence / notes |
|---|---|---|
| Per-minute rate limit | SUPPORTED (known) | **8 requests/minute** (`X-RateLimit-Limit-Minute: 8`). Exceeding it returns 429 with `Retry-After` 10–17 s. |
| Scope of the limit | UNKNOWN | Almost certainly per API key (Kong consumer). **Any environment using the same key shares the same 8/min budget.** Not provable without a second key. |
| Hourly / daily quota | UNKNOWN | No hour/day headers observed; techdocs give no numbers. Must be measured in shadow operation. |
| Concurrency limit | UNKNOWN | Not documented, not tested (testing would require deliberate bursts). |
| Application-side throttling | SUPPORTED (since A1) | At the time of the spike, `gemiapp/services.py::_get` had no pacing, no shared budget, retried only 429/5xx with 1/3/9/27 s sleeps, did not retry network timeouts, and `import_companies_since_date` retried forever. All GEMI access now goes through `gemiapp/ingestion/client.py::GemiClient`: a shared database-cache budget of at most 7 requests in any rolling minute, priority lanes, `Retry-After`/`RateLimit-Reset` honoured as a cross-process cooldown, and bounded retries. |
| Page size | SUPPORTED | `resultsSize` maximum **200**; 201 and 500 → HTTP 400 "greater than the configured maximum (200)". |
| Deep pagination | SUPPORTED | `resultsOffset` 10,000 / 100,000 / 400,000 returned rows; an offset past the total returns 0 rows. Offset paging over a changing dataset can skip or duplicate rows; stable ordering by `+arGemi` mitigates. |

## 3. Company search (`GET /companies`)

| Capability | Status | Evidence / notes |
|---|---|---|
| Search by GEMI number | SUPPORTED | `arGemi` (string). |
| Search by AFM | SUPPORTED | `afm`, must be 9 digits with leading zeros. |
| Search by name / distinctive title | SUPPORTED (semantics UNKNOWN) | `name` searches both. Substring, prefix or token matching was not verified. |
| KAD filter | SUPPORTED | `activities` (comma-separated ids). `53200200` → 32,572 companies. Matches **primary and secondary** entries (36 Κύρια, 14 Δευτερεύουσα in 50 results). |
| KAD filter on ended (historical) codes | UNKNOWN (likely current only) | An ended `kad_2008` code with `isActive=true` returned 404 (no results), and no matched entry in 50 results had `dtTo`. Suggests current activities only; not proven. |
| Prefecture filter | SUPPORTED | `prefectures` (ids). `5` → 580,866. |
| Municipality filter | SUPPORTED | `municipalities` (ids). `61190` → 13,180. |
| Legal-form filter | SUPPORTED | `legalTypes` (ids). |
| Status filter | SUPPORTED | `statuses` (ids) and `isActive` (boolean). `statuses=3` = 1,051,939 = active total. |
| GEMI office filter | SUPPORTED (spec) | `gemiOffices` (ids). Not exercised. |
| Multi-value semantics | SUPPORTED | Comma-separated ids inside one criterion are **OR** (two prefectures: 593,156 > 580,866); different criteria are **AND** (KAD ∧ prefecture = 13,768). **Same semantics as Gemi Leads Radars.** |
| Search without criteria | SUPPORTED (contradicts docs) | The spec says at least one criterion is required; live call returned 200 with totalCount 1,686,965. |
| Ordering | SUPPORTED (limited) | Only `±coName`, `±afm`, `±arGemi`, `±incorporationDate`. `-lastStatusChange` → 400 "not an allowable value". |
| Ordering by GEMI number as a discovery order | REQUIRES WORKAROUND (empirically consistent) | `-arGemi` returned the most recent registrations first, including companies whose `incorporationDate` was 2026-09-09 and 2025-12-15 (late publications). Monotonicity of `arGemi` with publication order is **observed in a 30-row sample, not guaranteed**; needs shadow verification. |
| Date filters (incorporation, status change, registration) | NOT SUPPORTED | Not in spec. `incorporationDateFrom`, `dateFrom`, `lastStatusChangeFrom` silently ignored (total unchanged). |
| Response metadata | SUPPORTED | `searchMetadata.totalCount`, `resultsOffset`, `resultsSize`. |

## 4. Company record (`GET /companies/{arGemi}` and search items)

Search items and detail responses have the same 30 keys: `activities, afm, arGemi, autoRegistered,
branch, capital, city, coNameEl, coNamesEn, coTitlesEl, coTitlesEn, email, fax, gemiOffice,
incorporationDate, isBranch, lastStatusChange, legalType, municipality, objective, persons, phone,
poBox, prefecture, status, stocks, street, streetNumber, url, zipCode`. `phone` and `fax` are
returned but are not in the spec.

| Attribute (blueprint need) | Status | Evidence / notes |
|---|---|---|
| Stable identifier | SUPPORTED | `arGemi` (12 digits; string in search, integer in spec). |
| Name, distinctive titles (EL/EN) | SUPPORTED | `coNameEl`, `coNamesEn[]`, `coTitlesEl[]`, `coTitlesEn[]`. For sole traders (ΑΤΟΜΙΚΗ) the name is a natural person's name. |
| Legal form | SUPPORTED | `legalType {id, descr}`; 26 values in reference data. |
| Company status | SUPPORTED | `status {id, descr}`; 12 statuses with `isActive` in reference data. |
| Status change date | SUPPORTED | `lastStatusChange` (date), non-null in 400/400 sampled records. **Usable as `effective_at` for `STATUS_CHANGED`.** |
| Registration / incorporation date | SUPPORTED (dirty) | `incorporationDate`; future and 1821 values exist. Needs validation, not clamping. |
| GEMI registration-completion date for the company | NOT SUPPORTED | No such field on the company; only on individual documents (`dateRegistrated`). |
| KADs with primary / secondary | SUPPORTED | `activities[].type` values observed: Κύρια, Δευτερεύουσα, Βοηθητική, Λοιπή. |
| KAD validity dates | SUPPORTED | `activities[].dtFrom` / `dtTo`; 633 of 1,354 activity entries in the older sample had `dtTo`. **Dated KAD history is published by the source.** |
| KAD version | SUPPORTED | `activities[].activity.kadVersion` = `kad_2008` / `kad_2026` (some `null`). |
| Address / location | SUPPORTED | `street`, `streetNumber`, `zipCode`, `city`, `poBox`, `municipality {id, descr}`, `prefecture {id, descr}`. Street present in ~99 %, street number in 27–95 % depending on sample. |
| Location change date | NOT SUPPORTED | No date on address fields. Corroboration possible via document subject 1 «Αλλαγή δ/νσης γραφείων-έδρας». |
| Legal form change date | NOT SUPPORTED | No date on `legalType`. |
| Record modification timestamp | NOT SUPPORTED | No `lastModified` / `updatedAt` on company, activity or address. |
| Branch information | SUPPORTED (semantics UNKNOWN) | `isBranch` (true = branch, false = parent) and `branch[]` (integers per spec, presumably related GEMI numbers). 5–8 branches per 200 sampled; `branch[]` non-empty in 3 of 200. |
| Capital | SUPPORTED | `capital[] {capitalStock, currency, ecsokefalaiikes, eggiitikes}` — company-level; present for ~48 % of new companies, rare for older partnerships. No date. |
| Shares | SUPPORTED | `stocks[] {stockTypeId, amount, nominalPrice, stockType}`. |
| Purpose | SUPPORTED | `objective` (free text). |
| Registration completeness flag | SUPPORTED (unreliable) | `autoRegistered` — spec: `false` means incomplete self-registration and incomplete data. It was `false` in **400/400** sampled records, including active long-established companies, so it cannot be used as a quality gate without further investigation. |
| Contact data | SUPPORTED (personal-data risk) | `email` (~92 % of new companies), `url` (rare), `phone`, `fax`. May belong to natural persons (sole traders, accountants). |
| Natural persons | SUPPORTED (excluded from MVP) | `persons[] {personName, businessName, role, dtFrom, dtTo, isRepresentativeAlone, isRepresentativeInCommon, percentage, category}`. Present in 48 % (new) / 12 % (older) of sampled search items. **Included in every search page**, so minimisation must happen at ingestion. |

## 5. Announcements and documents (`GET /companies/{arGemi}/documents`)

| Capability | Status | Evidence / notes |
|---|---|---|
| Document list per company | SUPPORTED | Returns `{decision: [...], publication: [...]}`. 0–30 decisions observed per company. |
| Independent / global document query | NOT SUPPORTED | No document search endpoint in the spec; only per company. Discovering new announcements requires one request per monitored company. |
| Date-range filter on documents | NOT SUPPORTED | Unknown parameters (`dateFrom`, `modifiedSince`) ignored. |
| Ordering | SUPPORTED (observed) | `decision[]` sorted by `dateAnnounced` descending in both multi-document companies examined. |
| Announcement metadata | SUPPORTED | `decisionSubject`, `decisionSubjectID`, `summary` (≤ 223 chars observed), `assembly` (free text: Γενική Συνέλευση, Διοικητικό Συμβούλιο, Εταιρεία, Αυτεπάγγελτη Καταχώρηση …). |
| Decision date | SUPPORTED | `dateAssemblyDecided`. |
| Announcement date | SUPPORTED | `dateAnnounced`. |
| Registration completion date | SUPPORTED | `dateRegistrated`. |
| Document identifier | SUPPORTED | `kak` (registration number); `referenceKak` for corrections/revocations (null in all sampled). |
| Application status | SUPPORTED | `applicationStatusId` / `applicationStatusDescription` (all sampled: `2 : ΚΑΤΑΧΩΡΙΣΗ`). |
| Coded subjects (rule-based classification) | SUPPORTED | `decisionSubjectID` maps to `/metadata/assemblySubjects` (114 subjects), e.g. 1 address change, 3/31 capital increase, 5/52 dissolution, 38 bankruptcy, 96 revival, 99/100 registration suspension/lift, 10 board election (person-related). |
| Document download | SUPPORTED (not downloaded) | `assemblyDecisionUrl` = `…/api/opendata/v1/downloadFile?key=…&elementId=…` (7-digit element id). The `key` parameter is a document-specific key, **not** the application's API key (verified as a boolean). `HEAD` → 405; `GET` is documented to return the file as an attachment. Content type, size and rate-limit cost of downloads are UNKNOWN. |
| Publications (ΥΜΣ) | SUPPORTED (spec), UNKNOWN (live) | `publication[] {url, kad}`; empty in every sampled company. |
| Document types / organ types reference lists | NOT SUPPORTED | Techdocs mention them, but the API spec exposes no such endpoints; `assembly` is free text. |

## 6. Reference / parametric data (`/metadata/*`)

| Endpoint | Status | Count | Notes |
|---|---|---|---|
| `/metadata/activities` (KAD) | SUPPORTED | 19,368 | 9,652 `kad_2026` + 9,716 `kad_2008`; 3,636 ids exist in both versions. No parent/level hierarchy (hierarchy must be derived from code structure). The app catalogue `kad_2025.json` (9,651) matches `kad_2026` for 9,648 codes; 3 codes match neither version. |
| `/metadata/prefectures` | SUPPORTED | 56 | Includes `0 Inadequate Info` and units 52–55 (Ανατολικής Αττικής, Δυτικής Αττικής, Αθηνών, Πειραιά). |
| `/metadata/municipalities` | SUPPORTED | 333 | `prefectureId` references only prefectures 0–51; none reference 52–55. Company records carry ids that exist here (e.g. 61190 → prefecture 5). |
| `/metadata/companyStatuses` | SUPPORTED | 12 | With `isActive`; only `3 Ενεργή` is active. |
| `/metadata/legalTypes` | SUPPORTED | 26 | Includes `0 Inadequate Info`. |
| `/metadata/gemiOffices` | SUPPORTED | 61 | Contains office contact fields. |
| `/metadata/assemblySubjects` (decision subjects) | SUPPORTED | 114 | Maps to `decisionSubjectID`. |
| Document types | NOT SUPPORTED | — | No endpoint. |
| Organ types | NOT SUPPORTED | — | No endpoint; `assembly` free text. |
| `lastUpdated` on reference rows | SUPPORTED | — | Allows detecting reference-data changes during a weekly sync. |

## 7. Incremental collection (most important)

| Mechanism | Status | Evidence |
|---|---|---|
| `modifiedSince` / `updatedAfter` / `lastModified` | NOT SUPPORTED | Not in spec; live parameters ignored (total unchanged at 1,051,939). |
| Registration / incorporation date range | NOT SUPPORTED | Not in spec; `incorporationDateFrom`, `dateFrom` ignored. |
| Status-change date range | NOT SUPPORTED | `lastStatusChangeFrom` ignored. |
| Announcement date range | NOT SUPPORTED | No document search; `dateFrom` on documents ignored. |
| Ordering by modification date | NOT SUPPORTED | Only `coName`, `afm`, `arGemi`, `incorporationDate`; `-lastStatusChange` rejected. |
| Change feed / event stream / webhook | NOT SUPPORTED | None in spec or documentation. |
| Cursor | NOT SUPPORTED | `cursor` ignored; paging is offset-based only. |
| Conditional requests (`ETag`, `If-Modified-Since`) | NOT SUPPORTED | No validators returned. |
| **New-company discovery** | REQUIRES WORKAROUND | Page `resultsSortBy=-arGemi` from the top until reaching the highest `arGemi` already stored (high-water mark). Catches late publications that the current `incorporationDate == target` importer misses. A handful of requests per run. |
| **Change detection for known companies** | REQUIRES WORKAROUND | Re-fetch and diff. Cheapest form: page a search whose criteria equal a Radar's criteria (OR within / AND across, identical to the API), `resultsSortBy=+arGemi`, 200 full records per request. Per-company detail costs 1 request per company. |
| **Dated history without snapshots** | REQUIRES WORKAROUND (partial) | `activities[].dtFrom/dtTo` and `lastStatusChange` provide source-dated KAD and status history on first fetch. Legal form and address carry no dates and can only be observed as diffs between our own snapshots. |
| **New announcements** | REQUIRES WORKAROUND | One `/documents` request per monitored company, diffed by `kak`. Must be scoped to the monitoring universe. |

## 8. Capacity arithmetic (for planning, not guarantees)

| Operation | Cost at the known limit |
|---|---|
| Sustained ceiling | 8 req/min → 480/hour → 11,520/day (daily quota UNKNOWN) |
| Full registry via search pages | 1,686,965 / 200 ≈ 8,435 requests ≈ 17.6 h — **not viable as a routine job** |
| One Radar-shaped refresh, e.g. KAD 53200200 ∧ Αττική | 13,768 / 200 = 69 requests ≈ 9 min |
| Per-company detail refresh | 1 request per company → at most ~11,500 companies/day using the entire budget |
| Per-company document check | 1 request per company |
| Daily new-company discovery | a few requests per run |
| Reference sync (7 endpoints) | 7 requests, weekly |

**All collectors — daily import, intraday import, superadmin manual runs, reference sync, refresh
and documents — draw from one 8/min budget per key.** The current code has no shared budget.

## 9. Blueprint capability matrix (summary)

| Blueprint requirement | Status |
|---|---|
| §4 Personal API key, secret handling | SUPPORTED |
| §7 Raw source records with endpoint, timestamp, hash, status | REQUIRES WORKAROUND (no source `lastModified`; persons in every payload → store minimised) |
| §8 Canonical company model with codes | SUPPORTED |
| §8 `source_updated_at` | NOT SUPPORTED |
| §9 KAD with primary flag, first/last seen | SUPPORTED (`type`, `dtFrom`, `dtTo` from source) |
| §9 KAD dictionary with parent/level | REQUIRES WORKAROUND (derive from code structure; two KAD versions) |
| §10 Legal forms, prefectures, municipalities, statuses, GEMI offices | SUPPORTED |
| §10 Organ types, document types | NOT SUPPORTED |
| §10 Decision types | SUPPORTED (`assemblySubjects`) |
| §11 Initial import: search → paginate → IDs → detail | SUPPORTED, but detail step is redundant (search returns full records) |
| §13 Snapshots with hash compare | REQUIRES WORKAROUND (re-fetch required; no conditional requests) |
| §15 `NEW_COMPANY` | REQUIRES WORKAROUND (`-arGemi` high-water mark; validate dates) |
| §15 `STATUS_CHANGED` with effective date | SUPPORTED (`status.id` + `lastStatusChange`) |
| §15 `KAD_ADDED` / `KAD_REMOVED` with effective date | SUPPORTED (`dtFrom`/`dtTo`), must exclude the 2008→2026 reclassification |
| §15 `LEGAL_FORM_CHANGED` | REQUIRES WORKAROUND (snapshot diff only; effective date unknown) |
| §15 `LOCATION_CHANGED` | REQUIRES WORKAROUND (snapshot diff only; corroborate with subject-1 announcements) |
| §1 Tier-2 capital, branch, dissolution, merger, transformation | REQUIRES WORKAROUND (per-company documents with coded subjects; capital also as structured field) |
| §1 `MANAGEMENT_CHANGE` | SUPPORTED by source, **excluded from MVP** (natural persons) |
| §19 Announcement ingestion | REQUIRES WORKAROUND (per company only) |
| §20 Rule-based classification | SUPPORTED (`decisionSubjectID`) |
| §56 Retry on 429 / 5xx / timeouts | REQUIRES WORKAROUND (headers available; client must implement) |
| §57 Rate control and priority sync | REQUIRES WORKAROUND (single shared budget, 8/min) |
| §58 Smart monitoring frequency | REQUIRES WORKAROUND (mandatory given the budget) |
| §59 Full historical backfill | NOT SUPPORTED as a routine operation at this rate limit |
| §76 Source schema-change detection | SUPPORTED (since A2): successful responses are validated against versioned contracts in `gemiapp/ingestion/schemas.py` before any data reaches the importer |

## 10. Open verifications (non-blocking unless noted)

1. **Daily quota and key scope.** Measure in shadow operation. If staging uses the production key it
   consumes production's 8/min budget (blocking for any staging collector).
2. **`arGemi` monotonicity** as a discovery order — verify over at least 14 days of shadow discovery
   against the existing incorporation-date importer.
3. **Activities filter on historical codes** — confirm whether ended activities are searchable.
4. **`name` search semantics** — substring, prefix or token.
5. **`branch[]` semantics** — confirm it lists GEMI numbers of branches.
6. **`autoRegistered`** — understand why it is `false` for established active companies.
7. **`downloadFile`** — content type, size, and whether downloads count against the 8/min limit.
8. **`publication[]`** — find a company with ΥΜΣ publications to observe the shape.
