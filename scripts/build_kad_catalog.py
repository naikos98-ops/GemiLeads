"""Convert the official semicolon-delimited KAD CSV to the tracked app catalog."""

from __future__ import annotations

import csv
import json
import re
import sys
from pathlib import Path


def normalize_code(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("Usage: python scripts/build_kad_catalog.py INPUT.csv OUTPUT.json")
    input_path, output_path = map(Path, sys.argv[1:])
    catalog = []
    with input_path.open(encoding="utf-8-sig", newline="") as source:
        reader = csv.reader(source, delimiter=";")
        header = next(reader)
        if len(header) < 3:
            raise ValueError("Το CSV πρέπει να έχει στήλες ΚΑΔ, περιγραφή και πηγή.")
        for row_number, row in enumerate(reader, start=2):
            if len(row) < 3:
                raise ValueError(f"Μη έγκυρη γραμμή CSV: {row_number}")
            code, description, source_url = (cell.strip() for cell in row[:3])
            normalized_code = normalize_code(code)
            if not normalized_code or not description:
                raise ValueError(f"Κενός ΚΑΔ ή περιγραφή στη γραμμή {row_number}")
            catalog.append({
                "code": code,
                "normalized_code": normalized_code,
                "description": description,
                "source": source_url,
            })
    if len({item["normalized_code"] for item in catalog}) != len(catalog):
        raise ValueError("Βρέθηκαν διπλότυποι κανονικοποιημένοι ΚΑΔ.")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(catalog, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"Wrote {len(catalog)} KAD entries to {output_path}")


if __name__ == "__main__":
    main()
