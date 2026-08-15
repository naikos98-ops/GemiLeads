from __future__ import annotations

import re
import unicodedata


def normalize_kad_code(value: object) -> str:
    """Return the digits-only representation shared by GEMI and the KAD catalog."""
    return re.sub(r"\D", "", str(value or ""))


def display_kad_code(value: object) -> str:
    code = normalize_kad_code(value)
    return ".".join(code[index:index + 2] for index in range(0, 8, 2)) if len(code) == 8 else code


def normalize_kad_search(value: object) -> str:
    """Uppercase and remove Greek accents for predictable SQLite text search."""
    decomposed = unicodedata.normalize("NFD", str(value or ""))
    without_accents = "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
    return " ".join(without_accents.upper().split())
