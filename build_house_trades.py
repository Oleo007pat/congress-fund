"""
build_house_trades.py
---------------------
Henter mange PTR-indberetninger fra Repræsentanternes Hus og samler alle aktiehandler
i én CSV-fil, klar til backtest.

Kør (fra projektmappen):
    python3 build_house_trades.py 2024 50        # prøvekørsel: 50 indberetninger fra 2024
    python3 build_house_trades.py 2021-2026      # alt fra 2021 til 2026 (tager lang tid)

Rart at vide:
  - PDF-teksten gemmes i house_cache/, så en afbrudt kørsel kan genoptages uden at hente igen.
  - Der holdes en kort pause mellem hver hentning, så vi ikke belaster Husets server.
  - Resultatet skrives til house_trades.csv. Indberetninger der ikke kunne læses, står i house_failures.csv.
  - Kun aktier og ETF'er med ticker kommer med i house_trades.csv.
"""

from __future__ import annotations

import csv
import sys
import time
from pathlib import Path

import requests

from fetch_house_sample import fetch_index, pdf_to_text, pdf_url
from house_ptr_parser import parse_ptr_text

CACHE_DIR = Path("house_cache")
TRADES_CSV = Path("house_trades.csv")
FAILURES_CSV = Path("house_failures.csv")
PAUSE_SECONDS = 0.3

TRADE_FIELDS = [
    "member", "state_dst", "filing_id", "year", "owner", "sub_holding", "ticker", "asset",
    "asset_type", "side", "partial", "amount_low", "amount_high",
    "transaction_date", "notification_date", "filing_date",
    "short_related", "description",
]


def index_date(value: str) -> str | None:
    """Indeksets FilingDate står som M/D/YYYY, fx '1/2/2024'."""
    try:
        m, d, y = (int(x) for x in value.strip().split("/"))
        return f"{y:04d}-{m:02d}-{d:02d}"
    except (ValueError, AttributeError):
        return None


def get_text(session: requests.Session, year: int, doc_id: str) -> str:
    """Henter PDF-tekst (fra cache hvis den findes)."""
    path = CACHE_DIR / str(year) / f"{doc_id}.txt"
    if path.exists():
        return path.read_text(encoding="utf-8")
    resp = session.get(pdf_url(year, doc_id), timeout=60)
    resp.raise_for_status()
    text = pdf_to_text(resp.content)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    time.sleep(PAUSE_SECONDS)
    return text


def member_name(f: dict) -> str:
    return f"{f['first']} {f['last']}".strip()


def parse_years(arg: str) -> list[int]:
    if "-" in arg:
        a, b = arg.split("-")
        return list(range(int(a), int(b) + 1))
    return [int(arg)]


def main() -> None:
    years = parse_years(sys.argv[1] if len(sys.argv) > 1 else "2024")
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else None

    session = requests.Session()
    session.headers["User-Agent"] = "congress-fund-research/0.1"
    n_trades = n_ok = 0
    failures: list[dict] = []

    with TRADES_CSV.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=TRADE_FIELDS, extrasaction="ignore")
        writer.writeheader()

        for year in years:
            filings = fetch_index(year)
            if limit:
                filings = filings[:limit]
            print(f"{year}: behandler {len(filings)} indberetninger ...")

            for i, f in enumerate(filings, 1):
                try:
                    text = get_text(session, year, f["doc_id"])
                    result = parse_ptr_text(text)
                except Exception as exc:  # én dårlig fil må ikke stoppe resten
                    failures.append({"year": year, "doc_id": f["doc_id"], "member": member_name(f),
                                     "reason": f"fejl: {exc}"})
                    continue

                if result["warnings"]:
                    failures.append({"year": year, "doc_id": f["doc_id"], "member": member_name(f),
                                     "reason": "; ".join(result["warnings"])})
                n_ok += 1
                for t in result["trades"]:
                    if t["is_stock"]:
                        # Navn, stat, ID og indleveringsdato fra indekset er mest pålidelige
                        t["member"] = member_name(f) or t["member"]
                        t["state_dst"] = f["state_dst"] or t["state_dst"]
                        t["filing_id"] = f["doc_id"]
                        t["filing_date"] = index_date(f["filing_date"]) or t["filing_date"]
                        t["year"] = year
                        writer.writerow(t)
                        n_trades += 1

                if i % 25 == 0:
                    print(f"  {i}/{len(filings)} ... {n_trades} aktiehandler indtil nu")

    with FAILURES_CSV.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["year", "doc_id", "member", "reason"])
        w.writeheader()
        w.writerows(failures)

    print(f"\nFærdig. {n_ok} indberetninger læst, {n_trades} aktiehandler gemt i {TRADES_CSV}.")
    print(f"{len(failures)} indberetninger med advarsel/fejl, se {FAILURES_CSV}.")


if __name__ == "__main__":
    main()
