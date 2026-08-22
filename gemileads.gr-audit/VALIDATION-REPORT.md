# Sitemap Architecture Audit — gemileads.gr

**Date:** 2026-08-22 · **Live commit:** `340c26b` · **Local HEAD:** `446a28f` (**unpushed**)
**Verdict: no sitemap exists in production. The pending implementation is correct on every
substantive check and needs one small change before deploy.**

Nothing was modified.

---

## 1 · Does a sitemap exist? — **NO**

Discovery ran against the declared location and all common candidate paths:

| Path | Status |
|---|---|
| `/robots.txt` (would declare the sitemap) | **404** |
| `/sitemap.xml` | **404** |
| `/sitemap_index.xml` · `/sitemap-index.xml` | 404 · 404 |
| `/sitemap.xml.gz` · `/sitemap1.xml` | 404 · 404 |
| `/sitemap/sitemap.xml` · `/wp-sitemap.xml` · `/sitemap.txt` | 404 · 404 · 404 |

No sitemap is declared and none is discoverable. **Severity: High.**

Because none exists, several requested checks are **vacuously clean** — an absence of data, not a
clean result: no invalid XML, no redirects inside a sitemap, no 404/5xx entries, no duplicates, no
canonical conflicts, no parameter URLs, no staging routes, no size breach.

**However**, commit `446a28f` (unpushed) implements one. The rest of this report validates **that**
implementation, since it is what will ship.

---

## 2 · Validation of the pending sitemap

I rendered the real XML through Django under production settings rather than reading the class
definition:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" xmlns:xhtml="http://www.w3.org/1999/xhtml">
<url><loc>https://gemileads.gr/</loc><changefreq>daily</changefreq><priority>1.0</priority></url>
<url><loc>https://gemileads.gr/pricing/</loc><changefreq>daily</changefreq><priority>0.9</priority></url>
<url><loc>https://gemileads.gr/signup/</loc><changefreq>daily</changefreq><priority>0.7</priority></url>
<url><loc>https://gemileads.gr/login/</loc><changefreq>daily</changefreq><priority>0.5</priority></url>
</urlset>
```

### Results

| Check | Result | Status |
|---|---|---|
| Valid XML | Parses without error | ✅ |
| Content-Type | `application/xml` | ✅ |
| URL count | 4 (limit 50,000) | ✅ |
| Uncompressed size | 563 bytes (limit 50 MB) | ✅ |
| Sitemap index required? | No — 49,996 URLs of headroom | ✅ |
| Duplicate URLs | 0 | ✅ |
| HTTPS only | All 4 | ✅ |
| HTTP status of every URL | **200 · 200 · 200 · 200** | ✅ |
| Redirects inside sitemap | None | ✅ |
| 404 / 5xx entries | None | ✅ |
| Parameter URLs | None | ✅ |
| Dev / staging routes | None | ✅ |
| Auth pages wrongly included | None | ✅ |
| Admin pages wrongly included | None | ✅ |
| Pagination entries | None (site has none) | ✅ |
| noindex/sitemap conflict | None — see §3 | ✅ |
| `<lastmod>` | **Absent** | ⚠️ |
| `<priority>` / `<changefreq>` | **Present — ignored by Google** | ℹ️ |

---

## 3 · Coverage — indexable URLs included, non-indexable excluded

I enumerated the **complete URL surface** from Django's resolver: **154 patterns, 70 static**.

**Included (4/4 correct):**

| URL | Live | Indexable | In sitemap |
|---|---|---|---|
| `/` | 200 | yes | ✅ |
| `/pricing/` | 200 | yes | ✅ |
| `/signup/` | 200 | yes | ✅ |
| `/login/` | 200 | yes | ✅ |

**Correctly excluded (66 routes):**

| Category | Count | Examples |
|---|---:|---|
| Django admin | 38 | `/admin/`, `/admin/gemiapp/company/`, `/admin/auth/user/` |
| Superadmin | 11 | `/superadmin/`, `/superadmin/users/`, `/superadmin/health/` |
| Machine/API | 5 | `/api/kads/`, `/api/stripe/webhook/`, `/export/` |
| Auth & utility | 6 | `/logout/`, `/password_reset/`, `/resend-verification/`, `/reset/done/` |
| Login-gated app | 6 | `/dashboard/`, `/leads/`, `/radars/`, `/settings/` |

**noindex cross-check — no conflicts.** All 9 `noindex` templates are absent from the sitemap, and
none of the 4 sitemap URLs carries a `noindex` directive:

```
noindexed & correctly excluded:
  dashboard.html · settings.html · unsubscribed.html
  registration/password_reset_{form,done,confirm,complete}.html
  registration/resend_verification.html · registration/verify_pending.html
```

**Dynamic route decision is correct.** `/companies/<gemi_number>/` is `@login_required`. Adding
~17,789 gated company pages would create severe index bloat and every URL would return a 302 to
login. Correctly excluded.

**No thin pages wrongly included.** `/signup/` (83 words) and `/login/` (67 words) are thin, but are
legitimate navigational targets for brand queries. Including them is correct; they should **not** be
optimised for content depth.

---

## 4 · Issues Found

### S1 · No sitemap in production — **High**
**File:** `config/urls.py` (route exists only in unpushed `446a28f`)
**Fix:** deploy `446a28f`. **Confidence:** Verified.

### S2 · `<lastmod>` missing — **Medium**
**File:** `gemiapp/seo.py` — `StaticViewSitemap` defines no `lastmod`.

Google **only honours `lastmod` when it is consistently and verifiably accurate**, and ignores it
otherwise. Omitting it is safer than faking it — but these are static marketing pages whose real
change date is the template's last edit, which is knowable.

**Recommendation:** add a truthful, hardcoded date bumped only on genuine content change. Do **not**
wire it to `timezone.now()` or to `Company.updated_at` — a homepage `lastmod` that changes daily
because *company data* changed is exactly the "suspiciously uniform / always fresh" pattern Google
learns to distrust.

### S3 · `<priority>` and `<changefreq>` present — **Info**
**File:** `gemiapp/seo.py` lines defining `changefreq = "daily"` and `priority()`.

Both are **ignored by Google**. `changefreq = "daily"` is also inaccurate — these pages do not change
daily. Harmless, but removing them shrinks the file and eliminates a false signal.

---

## 5 · Exact Recommended Changes

**File: `gemiapp/seo.py`** — replace the `StaticViewSitemap` class:

```python
from datetime import date

class StaticViewSitemap(Sitemap):
    """The public marketing and entry pages.

    priority/changefreq are omitted deliberately: Google ignores both.
    lastmod is a hardcoded, truthful date, bump it only when the page content
    actually changes. Never wire it to Company data or to now(): an always-fresh
    lastmod is the pattern Google learns to distrust.
    """

    protocol = "https"

    # Bump when the corresponding template's user-visible content changes.
    LAST_MODIFIED = {
        "home": date(2026, 8, 22),
        "pricing": date(2026, 8, 22),
        "signup": date(2026, 8, 22),
        "login": date(2026, 8, 22),
    }

    def items(self):
        return ["home", "pricing", "signup", "login"]

    def location(self, item):
        return reverse(item)

    def lastmod(self, item):
        return self.LAST_MODIFIED.get(item)
```

Resulting XML:

```xml
<url><loc>https://gemileads.gr/</loc><lastmod>2026-08-22</lastmod></url>
<url><loc>https://gemileads.gr/pricing/</loc><lastmod>2026-08-22</lastmod></url>
<url><loc>https://gemileads.gr/signup/</loc><lastmod>2026-08-22</lastmod></url>
<url><loc>https://gemileads.gr/login/</loc><lastmod>2026-08-22</lastmod></url>
```

**Note:** all four dates being identical is *truthful* here — the templates were last changed together
in `446a28f`. It becomes a problem only if they never diverge while pages actually change.

**No change needed** to `robots.txt`; it already declares `Sitemap:` and correctly emits `https://`
in production (the `http://` seen in local rendering is a DEBUG-only artefact of
`SECURE_PROXY_SSL_HEADER` being set under `if not DEBUG` — verified).

### Not recommended

| Suggestion | Why not |
|---|---|
| Sitemap index file | 4 URLs vs a 50,000 limit. Pointless indirection. |
| Split by content type | Only one content type exists. |
| Add `/companies/<gemi>/` | 17,789 login-gated pages → index bloat + 302s. |
| Add `/password_reset/`, `/resend-verification/` | Correctly `noindex`; including them would create the exact conflict this audit checks for. |
| Image/video/news extension sitemaps | No `<img>` elements exist on public pages (all inline SVG), no video, not a news publisher. |

---

## 6 · Post-Deploy Verification

```bash
curl -s -o /dev/null -w "%{http_code}\n" https://gemileads.gr/sitemap.xml   # expect 200
curl -s https://gemileads.gr/robots.txt | grep -i sitemap                   # expect https:// URL
curl -s https://gemileads.gr/sitemap.xml | grep -c "<loc>"                  # expect 4
```

Then submit in Search Console.

**How this fails:** the sitemap returns 200 but Search Console reports "Couldn't fetch" — almost
always a scheme mismatch between the `robots.txt` declaration and the canonical host. Verified correct
in production config, but worth confirming once live.

**Leading indicator:** Search Console → Pages shows 4 discovered URLs within roughly a week. If
`/pricing/` stays undiscovered, that confirms the separate orphan-page problem (zero internal links
point to it) rather than a sitemap fault.

---

## Priority Summary

| # | Action | Severity | Effort |
|---|---|---|---|
| S1 | Deploy `446a28f` to ship the sitemap | **High** | Easy |
| S2 | Add truthful `lastmod` | Medium | Easy |
| S3 | Remove `priority` / `changefreq` | Info | Easy |
| — | Submit to Search Console | — | Easy |

S2 and S3 are the same one-class edit and should ship together with S1.

---

## Methodology & Limits

- Discovery and URL status checks performed live 2026-08-22.
- The pending sitemap was **rendered through Django under production settings**, not inferred from
  source — so the XML validated above is exactly what will ship.
- URL surface enumerated from Django's resolver (154 patterns) for complete coverage analysis, not a
  crawl sample.
- No traffic, ranking or Search Console data was available and none was invented.
