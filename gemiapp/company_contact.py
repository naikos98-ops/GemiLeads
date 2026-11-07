"""The company's GEMI contact phone, read through from the stored GEMI record.

Hotfix bridge: the importer already stores the whole GEMI company record in ``Company.raw_data``, and GEMI
publishes the company's contact telephone at one top-level field, ``phone``. This module reads only that field
-- never ``persons`` (partners, managers, representatives), ``fax``, ``objective`` or any other text, and never
a pattern search over the JSON -- so no person's number can surface by accident.

Nothing is persisted, logged or fetched: the value is derived on each read from data already stored, and
opening a Dossier makes no GEMI request. A sole trader's contact phone is usually a natural person's own number,
so it is never shown or exported (Founder decision, 2026-11-07): ``company_phones`` returns nothing for a sole
trader (GEMI legalType id 16 or an "ΑΤΟΜΙΚ..." label) or for a company whose legal form is unknown. Everything
else stays behind the same subscription gate as the contact email, and a dedicated, minimised company contact
layer (explicit source, verification time, retention and privacy semantics) must still replace this bridge.

Normalisation is deliberately minimal: surrounding and repeated whitespace is collapsed, exact duplicates are
dropped, and a value with no digit at all (e.g. a placeholder) is not a phone. No country code is guessed, no
number is reformatted and nothing is validated against the outside world. The ``tel:`` URI keeps a leading
``+`` and the digits only.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

# The only approved source: GEMI's company-level contact telephone.
APPROVED_PHONE_FIELD = "phone"
# GEMI legalType id of a sole trader (ΑΤΟΜΙΚΗ).
SOLE_TRADER_LEGAL_TYPE_ID = "16"
_COMPANY_CONTACT_MODEL_FIELDS = (
    "email", "website", "address", "postal_code", "city", "municipality", "prefecture", "legal_type", "raw_data",
)


@dataclass(frozen=True)
class CompanyPhone:
    display: str
    tel_uri: str


@dataclass(frozen=True)
class CompanyContact:
    """Display-safe contact values already persisted on ``Company``.

    The phone remains the deliberately narrow raw-payload bridge described above. Every other value comes from a
    structured Company column populated by the existing importer. This object is presentation-only: it does not
    copy contact data into opportunities, signals or snapshots.
    """

    phones: tuple[CompanyPhone, ...]
    email: str
    website: str
    website_href: str | None
    address: str

    @property
    def available(self) -> bool:
        return bool(self.phones or self.email or self.website or self.address)


def company_contact_model_fields(prefix: str = "") -> tuple[str, ...]:
    """Fields an authorized caller must load before passing a Company to ``company_contact``."""
    return tuple(f"{prefix}{field}" for field in _COMPANY_CONTACT_MODEL_FIELDS)


def _values(raw: Any) -> list:
    if isinstance(raw, bool) or raw is None:
        return []
    if isinstance(raw, (str, int)):
        return [raw]
    if isinstance(raw, (list, tuple)):
        return [item for item in raw if isinstance(item, (str, int)) and not isinstance(item, bool)]
    return []


def extract_company_contact_phones(raw_data: Any) -> tuple[CompanyPhone, ...]:
    """The distinct GEMI company contact phones in a stored record. Never mutates ``raw_data``."""
    if not isinstance(raw_data, dict):
        return ()
    phones: dict[str, CompanyPhone] = {}
    for value in _values(raw_data.get(APPROVED_PHONE_FIELD)):
        display = " ".join(str(value).split())
        digits = "".join(character for character in display if character.isdigit())
        if not digits or display in phones:
            continue
        phones[display] = CompanyPhone(display=display, tel_uri=f"tel:{'+' if display.startswith('+') else ''}{digits}")
    return tuple(phones.values())


def _fold(value: str) -> str:
    """Upper-case without accents, so "Ατομική" and "ΑΤΟΜΙΚΗ" compare equal."""
    decomposed = unicodedata.normalize("NFD", value)
    return "".join(c for c in decomposed if unicodedata.category(c) != "Mn").upper()


def phone_may_be_shown(company) -> bool:
    """False for a sole trader and for a company whose legal form is unknown (fail closed)."""
    raw = company.raw_data if isinstance(company.raw_data, dict) else {}
    legal = raw.get("legalType") if isinstance(raw.get("legalType"), dict) else {}
    source_id = _text(legal.get("id"))
    label = _fold(_text(company.legal_type) or _text(legal.get("descr")))
    if not source_id and not label:
        return False
    return source_id != SOLE_TRADER_LEGAL_TYPE_ID and "ΑΤΟΜΙΚ" not in label


def company_phones(company) -> tuple[CompanyPhone, ...]:
    """The GEMI contact phones that may be shown or exported for this company; empty for sole traders."""
    if not phone_may_be_shown(company):
        return ()
    return extract_company_contact_phones(company.raw_data)


def phone_withheld(company) -> bool:
    """GEMI publishes a phone, but it is hidden because the company is a sole trader or its form is unknown."""
    return not phone_may_be_shown(company) and bool(extract_company_contact_phones(company.raw_data))


def _text(value: Any) -> str:
    """Collapse whitespace in a scalar display value; never stringify containers or booleans."""
    if value is None or isinstance(value, (bool, dict, list, tuple, set)):
        return ""
    return " ".join(str(value).split())


def _website_href(value: str) -> str | None:
    """Return only an absolute HTTP(S) URL for an external link; unsafe schemes remain unlinked text."""
    if not value or any(ord(character) < 32 for character in value):
        return None
    parsed = urlsplit(value)
    return value if parsed.scheme.lower() in {"http", "https"} and parsed.netloc else None


def company_contact(company) -> CompanyContact:
    """Build the current company contact read model from the existing Company row, without I/O or mutation."""
    website = _text(company.website)
    address_parts = []
    for value in (company.address, company.postal_code, company.city, company.municipality, company.prefecture):
        part = _text(value)
        if part and part not in address_parts:
            address_parts.append(part)
    return CompanyContact(
        phones=company_phones(company),
        email=_text(company.email),
        website=website,
        website_href=_website_href(website),
        address=", ".join(address_parts),
    )
