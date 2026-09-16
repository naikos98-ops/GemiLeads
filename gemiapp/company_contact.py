"""The company's GEMI contact phone, read through from the stored GEMI record.

Hotfix bridge: the importer already stores the whole GEMI company record in ``Company.raw_data``, and GEMI
publishes the company's contact telephone at one top-level field, ``phone``. This module reads only that field
-- never ``persons`` (partners, managers, representatives), ``fax``, ``objective`` or any other text, and never
a pattern search over the JSON -- so no person's number can surface by accident.

Nothing is persisted, logged or fetched: the value is derived on each read from data already stored, and
opening a Dossier makes no GEMI request. A sole trader's contact phone can still be personal data, which is why
it stays behind the same subscription gate as the contact email, and why a dedicated, minimised company
contact layer (explicit source, verification time, retention and privacy semantics) must replace this bridge.

Normalisation is deliberately minimal: surrounding and repeated whitespace is collapsed, exact duplicates are
dropped, and a value with no digit at all (e.g. a placeholder) is not a phone. No country code is guessed, no
number is reformatted and nothing is validated against the outside world. The ``tel:`` URI keeps a leading
``+`` and the digits only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# The only approved source: GEMI's company-level contact telephone.
APPROVED_PHONE_FIELD = "phone"


@dataclass(frozen=True)
class CompanyPhone:
    display: str
    tel_uri: str


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
