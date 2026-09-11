# Privacy-Safe Marketing & Demo Dataset Document

## Overview

This document details the privacy-safe, 100% synthetic marketing and demo dataset created for public NORVA website screenshots, product storytelling, and demonstration captures of **Gemi Leads**.

None of the records in this dataset correspond to real individuals, active commercial entities, actual VAT numbers, or private customer data.

---

## Management Command & Reversibility

To ensure zero risk to production data schemas and to enable non-destructive seeding and cleanup, the dedicated Django management command `seed_demo_marketing_data` has been implemented:

### Commands
- **Seed Marketing Dataset**:
  ```powershell
  python manage.py seed_demo_marketing_data
  ```
- **Clean Up Marketing Dataset**:
  ```powershell
  python manage.py seed_demo_marketing_data --clean
  ```

---

## Fictional Business Examples

The dataset consists of 4 fictional Greek commercial entities exercising all supported product states (Signal Intake, Radar Match, Lead Inbox qualification, and Business Dossier):

### 1. ΑΙΓΑΙΟ LOGISTICS ΜΟΝ. Ι.Κ.Ε.
- **GEMI Number**: `999000101000` *(Synthetic)*
- **VAT Number**: `999900101` *(Synthetic)*
- **Legal Form**: `Μονοπρόσωπη Ιδιωτική Κεφαλαιουχική Εταιρεία`
- **Status**: `Ενεργή`
- **Incorporation Date**: `08/09/2026`
- **Region / Location**: `Θεσσαλονίκη, Εγνατίας 154, 54636`
- **Email**: `info@aegean-logistics-demo.gr` *(Demo domain)*
- **Website**: `https://www.aegean-logistics-demo.gr`
- **Activities (KAD)**: `52.29 — Άλλες συνοδευτικές της μεταφοράς δραστηριότητες`
- **Synthetic Person/Manager**: `Ελένη Παπαδοπούλου (Διαχειριστής & Νόμιμος Εκπρόσωπος, 100%)`
- **Matched Radar**: `Logistics & Εφοδιαστική`
- **Lead State**: `interested` *(Ενδιαφέρεται)* | **Favorite**: `Yes`
- **Notes**: `"Επικοινωνία με κ. Παπαδοπούλου στις 08/09. Ζήτησε προσφορά για ετήσιο συμβόλαιο εφοδιαστικής αλυσίδας."`

---

### 2. HELLAS CLOUD & DATA LABS Α.Ε.
- **GEMI Number**: `999000102000` *(Synthetic)*
- **VAT Number**: `999900102` *(Synthetic)*
- **Legal Form**: `Ανώνυμη Εταιρεία`
- **Status**: `Ενεργή`
- **Incorporation Date**: `07/09/2026`
- **Region / Location**: `Αθήνα, Λεωφ. Κηφισίας 220, 15124`
- **Email**: `contact@hellascloud-demo.gr` *(Demo domain)*
- **Website**: `https://www.hellascloud-demo.gr`
- **Activities (KAD)**: `62.01 — Δραστηριότητες προγραμματισμού ηλεκτρονικών συστημάτων`
- **Synthetic Person/Manager**: `Νικόλαος Αλεξίου (Διευθύνων Σύμβουλος)`
- **Matched Radar**: `Tech Startups & IT`
- **Lead State**: `contacted` *(Επικοινώνησα)* | **Favorite**: `No`
- **Notes**: `"Αποστολή εταιρικής παρουσίασης για cloud infrastructure & DevOps services."`

---

### 3. GREEN GRID SOLAR SOLUTIONS Ι.Κ.Ε.
- **GEMI Number**: `999000103000` *(Synthetic)*
- **VAT Number**: `999900103` *(Synthetic)*
- **Legal Form**: `Ιδιωτική Κεφαλαιουχική Εταιρεία`
- **Status**: `Ενεργή`
- **Incorporation Date**: `06/09/2026`
- **Region / Location**: `Λάρισα, Φαρσάλων 45, 41335`
- **Email**: `sales@greengrid-demo.gr` *(Demo domain)*
- **Website**: `https://www.greengrid-demo.gr`
- **Activities (KAD)**: `43.21 — Ηλεκτρικές εγκαταστάσεις`
- **Synthetic Person/Manager**: `Γεώργιος Δημητρίου (Διαχειριστής, 50%)`
- **Matched Radar**: `Ανανεώσιμες Πηγές Ενέργειας`
- **Lead State**: `new` *(Νέο)* | **Favorite**: `Yes`
- **Notes**: `""`

---

### 4. KALYPSO HOSPITALITY & TRADING Ε.Ε.
- **GEMI Number**: `999000104000` *(Synthetic)*
- **VAT Number**: `999900104` *(Synthetic)*
- **Legal Form**: `Ετερόρρυθμη Εταιρεία`
- **Status**: `Ενεργή`
- **Incorporation Date**: `05/09/2026`
- **Region / Location**: `Χανιά, Ακτή Τομπάζη 12, 73132`
- **Email**: `info@kalypso-demo.gr` *(Demo domain)*
- **Website**: `https://www.kalypso-demo.gr`
- **Activities (KAD)**: `56.10 — Δραστηριότητες υπηρεσιών εστιατορίων και κινητών μονάδων εστίασης`
- **Synthetic Person/Manager**: `Μαρία Κωνσταντίνου (Ομόρρυθμος Εταίρος, 60%)`
- **Matched Radar**: `Τουρισμός & Εστίαση`
- **Lead State**: `viewed` *(Προβλήθηκε)* | **Favorite**: `No`
- **Notes**: `"Επισκόπηση προφίλ εταιρείας."`

---

## Review Captures Summary

The captured marketing review images are stored in the artifact directory:

| Screen / View | Viewport | Target URL | Captures Artifact |
| :--- | :--- | :--- | :--- |
| **Signals Desktop** | 1440x900 | `/` | `marketing_signals_desktop` |
| **Radars Desktop** | 1440x900 | `/radars/` | `marketing_radars_desktop` |
| **Business Dossier Desktop** | 1440x900 | `/companies/999000101000/` | `marketing_dossier_desktop` |
| **Leads Desktop** | 1440x900 | `/leads/` | `marketing_leads_desktop` |
| **Signals Mobile** | 390x844 | `/` | `marketing_signals_mobile` |
| **Business Dossier Mobile** | 390x844 | `/companies/999000101000/` | `marketing_dossier_mobile` |

All images strictly preserve the governing **Signal Ledger UI** direction and contain zero real or identifiable personal/corporate data.
