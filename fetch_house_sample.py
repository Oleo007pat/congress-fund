"""
fetch_house_sample.py
---------------------
TRIN 1 af den gratis historiske datakilde: hent et lille udsnit af Repræsentanternes
Hus' officielle indberetninger (Periodic Transaction Reports) og vis den rå tekst,
så vi kan bygge en parser efter det, filerne faktisk indeholder.

Kør:  python3 fetch_house_sample.py 2024 3
      (år, antal PDF'er der hentes som prøve)

Kræver:  pip3 install requests pypdf
"""

from __future__ import annotations

import io
import sys
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

BASE = "https://disclosures-clerk.house.gov/public_disc"
OUT_DIR = Path("house_sample")


def fetch_index(year: int) -> list[dict]:
    """Henter årets indeks og returnerer kun PTR-indberetninger (FilingType 'P')."""
    url = f"{BASE}/financial-pdfs/{year}FD.zip"
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()

    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        xml_name = next(n for n in zf.namelist() if n.lower().endswith(".xml"))
        root = ET.fromstring(zf.read(xml_name))

    return parse_index(root)


def parse_index(root: ET.Element) -> list[dict]:
    filings = []
    for m in root.iter("Member"):
        if (m.findtext("FilingType") or "").strip() != "P":
            continue
        filings.append({
            "doc_id": (m.findtext("DocID") or "").strip(),
            "first": (m.findtext("First") or "").strip(),
            "last": (m.findtext("Last") or "").strip(),
            "state_dst": (m.findtext("StateDst") or "").strip(),
            "filing_date": (m.findtext("FilingDate") or "").strip(),
        })
    return [f for f in filings if f["doc_id"]]


def pdf_url(year: int, doc_id: str) -> str:
    return f"{BASE}/ptr-pdfs/{year}/{doc_id}.pdf"


def pdf_to_text(pdf_bytes: bytes) -> str:
    from pypdf import PdfReader  # importeres her, så index-delen kan testes uden pakken

    reader = PdfReader(io.BytesIO(pdf_bytes))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def main() -> None:
    year = int(sys.argv[1]) if len(sys.argv) > 1 else 2024
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 3

    filings = fetch_index(year)
    print(f"{year}: {len(filings)} PTR-indberetninger i indekset.")
    OUT_DIR.mkdir(exist_ok=True)

    for f in filings[:n]:
        resp = requests.get(pdf_url(year, f["doc_id"]), timeout=60)
        if resp.status_code != 200:
            print(f"DocID {f['doc_id']}: HTTP {resp.status_code}")
            continue
        text = pdf_to_text(resp.content)
        (OUT_DIR / f"{f['doc_id']}.txt").write_text(text, encoding="utf-8")
        print("=" * 70)
        print(f"DocID {f['doc_id']} - {f['first']} {f['last']} ({f['state_dst']}), indberettet {f['filing_date']}")
        print(f"Tegn udtrukket: {len(text)}")
        print(text[:1500] if text.strip() else "(ingen tekst - sandsynligvis en scannet PDF)")


if __name__ == "__main__":
    main()
