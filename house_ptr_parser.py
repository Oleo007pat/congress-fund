"""
house_ptr_parser.py
-------------------
Læser tekst fra en PTR-indberetning (Periodic Transaction Report) fra Repræsentanternes
Hus og returnerer handlerne som almindelige Python-ord.

Bygget og testet på tekst udtrukket med pypdf fra rigtige indberetninger. Formatet:

    SP Charles Schwab Corporation (SCHW)        <- ejer (SP/JT/DC, ellers medlemmet selv) + aktiv + ticker
    [ST]                                        <- aktivtype (ST = aktie, ET = ETF, GS = statsobligation ...)
    P 12/14/2023 01/08/2024 $50,001 -           <- type (P køb, S salg, E bytte), handelsdato, indberetningsdato, beløb
    $100,000
    F S: New                                    <- afslutter handlen
    S O: R.W. Allen & Associates, Inc.          <- konto handlen ligger i ("sub-holding")

Kendte begrænsninger (derfor returneres også en liste `warnings`):
  - Er teksten fra en scannet PDF uden tekstlag, findes der ingen handler.
  - Er en "S O:"-linje brudt over flere rækker, kan kontoteksten blive afkortet.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

OWNER_CODES = {"SP": "spouse", "JT": "joint", "DC": "dependent child"}
STOCK_TYPES = {"ST", "ET"}          # aktier og ETF'er - det vi kan købe via Alpaca
SIDE = {"P": "buy", "S": "sell", "E": "exchange"}

# Linjer der gentages som kolonneoverskrifter (og på hver side)
HEADER_LINES = {
    "T", "ID Owner Asset Transaction", "Type", "Date Notification", "Date",
    "Amount Cap.", "Gains >", "$200?",
}

TAIL_RE = re.compile(
    r"\[(?P<atype>[A-Z]{2})\]\s*"
    r"(?P<ttype>P|S|E)(?:\s*\((?P<partial>partial)\))?\s+"
    r"(?P<tdate>\d{2}/\d{2}/\d{4})\s+"
    r"(?P<ndate>\d{2}/\d{2}/\d{4})\s+"
    r"(?P<amount>(?:Over\s+)?\$[\d,]+(?:\s*-\s*(?:\$[\d,]+)?)?)"
)
TICKER_RE = re.compile(r"\(([A-Z][A-Z0-9.\-]{0,6})\)\s*$")
OPTION_WORDS = re.compile(r"\b(call|put|strike|option|expiration)\b", re.IGNORECASE)

# Afslutningslinjer. pypdf kan sætte NUL- og mellemrumstegn ind i dem, fx "F\x00\x00 S\x00: New"
FS_RE = re.compile(r"^F\s*S\s*:")
SO_RE = re.compile(r"^S\s*O\s*:\s*")
DESC_RE = re.compile(r"^D\s*:\s*")
COMMENT_RE = re.compile(r"^C\s*:\s*")


def normalize(text: str) -> str:
    """Fjerner NUL- og andre kontroltegn, som pypdf kan efterlade."""
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)


def _iso(d: str) -> str:
    return datetime.strptime(d, "%m/%d/%Y").date().isoformat()


def _parse_amount(text: str) -> tuple[Optional[float], Optional[float]]:
    nums = [float(n.replace(",", "")) for n in re.findall(r"\$([\d,]+)", text)]
    if not nums:
        return None, None
    if text.strip().lower().startswith("over"):
        return nums[0], None
    return nums[0], (nums[1] if len(nums) > 1 else None)


def parse_header(text: str) -> dict:
    name = re.search(r"Name:\s*(.+)", text)
    state = re.search(r"State/District:\s*(\S+)", text)
    filing = re.search(r"Filing ID\s*#\s*(\d+)", text)
    clean_name = re.sub(r"^(Hon\.|Mr\.|Mrs\.|Ms\.|Dr\.)\s*", "", name.group(1).strip()) if name else ""
    return {
        "member": clean_name,
        "state_dst": state.group(1) if state else "",
        "filing_id": filing.group(1) if filing else "",
    }


def _table_lines(text: str) -> list[str]:
    """Skærer ned til selve transaktionstabellen og markerer afslutningslinjerne."""
    lines = [ln.strip() for ln in text.splitlines()]
    start = next((i for i, ln in enumerate(lines) if "$200?" in ln), None)
    if start is None:
        return []
    out: list[str] = []
    for ln in lines[start + 1:]:
        if ln.startswith("* For the complete list"):
            break
        if not ln or ln in HEADER_LINES or ln.startswith("Filing ID"):
            continue
        if FS_RE.match(ln):
            out.append("§FS§")
        elif SO_RE.match(ln):
            out.append("§SO:" + SO_RE.sub("", ln).strip() + "§")
        elif DESC_RE.match(ln):
            out.append("§D:" + DESC_RE.sub("", ln).strip() + "§")
        elif COMMENT_RE.match(ln):
            out.append("§X§")
        else:
            out.append(ln)
    return out


def _attach_trailer(record: dict, segment: str) -> None:
    """Afslutningslinjerne efter en handel (konto og beskrivelse) hører til den handel."""
    so = re.findall(r"§SO:(.*?)§", segment)
    if so:
        record["sub_holding"] = so[0].strip()
    desc = re.findall(r"§D:(.*?)§", segment)
    if desc:
        record["description"] = " ".join(d.strip() for d in desc)


def parse_ptr_text(text: str) -> dict:
    """Returnerer {'header': {...}, 'trades': [...], 'warnings': [...]}."""
    text = normalize(text)
    header = parse_header(text)
    lines = _table_lines(text)
    joined = " ".join(lines)
    warnings: list[str] = []

    records: list[dict] = []
    pos = 0
    for m in TAIL_RE.finditer(joined):
        segment = joined[pos:m.start()]
        pos = m.end()

        # Afslutningsmarkører i begyndelsen hører til den foregående handel
        if records:
            _attach_trailer(records[-1], segment)
        asset = re.sub(r"§[^§]*§", " ", segment)
        asset = re.sub(r"\s+", " ", asset).strip()

        owner_m = re.match(r"(SP|JT|DC)\s+", asset)
        owner = OWNER_CODES[owner_m.group(1)] if owner_m else "self"
        if owner_m:
            asset = asset[owner_m.end():]

        ticker_m = TICKER_RE.search(asset)
        low, high = _parse_amount(m.group("amount"))
        records.append({
            "owner": owner,
            "asset": asset,
            "ticker": ticker_m.group(1) if ticker_m else "",
            "asset_type": m.group("atype"),
            "side": SIDE[m.group("ttype")],
            "partial": bool(m.group("partial")),
            "transaction_date": _iso(m.group("tdate")),
            "disclosure_date": _iso(m.group("ndate")),
            "amount_low": low,
            "amount_high": high,
            "sub_holding": "",
            "description": "",
        })

    # Afslutningsmarkører efter den sidste handel
    if records:
        _attach_trailer(records[-1], joined[pos:])

    n_markers = sum(1 for ln in lines if ln == "§FS§")
    if n_markers != len(records):
        warnings.append(f"{len(records)} handler fundet, men {n_markers} 'F S:'-markører i teksten")
    if not records and text.strip():
        warnings.append("ingen handler fundet i teksten")
    if not text.strip():
        warnings.append("ingen tekst (sandsynligvis scannet PDF)")

    for r in records:
        r["member"] = header["member"]
        r["state_dst"] = header["state_dst"]
        r["filing_id"] = header["filing_id"]
        r["option_like"] = bool(OPTION_WORDS.search(r["description"]))
        # Optionshandler noteret under aktien holdes ude af backtesten
        r["is_stock"] = r["asset_type"] in STOCK_TYPES and bool(r["ticker"]) and not r["option_like"]

    return {"header": header, "trades": records, "warnings": warnings}


if __name__ == "__main__":
    import sys

    for path in sys.argv[1:]:
        result = parse_ptr_text(open(path, encoding="utf-8").read())
        print(f"== {path}: {result['header']}")
        for t in result["trades"]:
            flag = "AKTIE" if t["is_stock"] else "andet"
            print(f"  [{flag}] {t['side']:<5} {t['ticker'] or '-':<6} {t['transaction_date']} -> {t['disclosure_date']} "
                  f"{t['amount_low']}-{t['amount_high']} ejer={t['owner']} konto={t['sub_holding']!r}")
        for w in result["warnings"]:
            print("  ADVARSEL:", w)
