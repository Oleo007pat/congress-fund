"""House Clerk access for the live fund: index, new filings, PDF download and parsing.

Reuses ingestion/collect_house_raw.py (URLs, index parsing, retrying GET) and
ingestion/parse_house_ptr.py (text extraction + parsing + include_v1 + duplicates), so the live parsing
is identical to the backtest's.  The parse module keeps its cache directories in module globals; they are
pointed at data/live/cache/ for the duration of a call (see patched_dirs).
"""
from __future__ import annotations

import contextlib
import csv
import io
import time
from datetime import date
from pathlib import Path

import requests

from ingestion import collect_house_raw as chr_
from ingestion import parse_house_ptr as hpp

USER_AGENT = chr_.USER_AGENT          # "congress-fund-research/0.1" (generic, no contact details)
PAUSE_SECONDS = 0.5
SCANNED_PREFIXES = ("8", "9")         # DocIDs starting with 8 or 9 are scanned paper filings
MAX_DOC_FAILURES = 5


class HouseUnreachable(Exception):
    pass


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    return s


def index_years(today: date) -> list[int]:
    """Current year; also the previous year during January (late-filed documents)."""
    return [today.year - 1, today.year] if today.month == 1 else [today.year]


def fetch_index(session, year: int, sleep=time.sleep) -> list[dict]:
    """PTR index rows for a year (each tagged with 'year').  Raises HouseUnreachable."""
    content, status = chr_.get_with_retry(session, chr_.index_url(year))
    sleep(PAUSE_SECONDS)
    if content is None:
        hint = " (blokeret af house.gov?)" if status in ("403", "429") else ""
        raise HouseUnreachable(f"house.gov kunne ikke nås for {year}-indekset: {status}{hint}")
    try:
        _, xml_bytes = chr_.extract_xml(content, year)
        rows = chr_.parse_index_xml(xml_bytes)
    except Exception as exc:                          # bad zip / xml
        raise HouseUnreachable(f"{year}-indekset fra house.gov kunne ikke læses ({type(exc).__name__})") from exc
    return [{**r, "year": str(year)} for r in rows]


def select_new_filings(rows: list[dict], start_date: str, before_date: str, seen: set[str]) -> list[dict]:
    """PTRs filed on/after the fund's start date and STRICTLY before the entry session's date
    (the backtest enters on the first trading day strictly after filing_date), not seen before."""
    out = [r for r in rows if r["doc_id"] not in seen and r["filing_date"] and start_date <= r["filing_date"] < before_date]
    out.sort(key=lambda r: (r["filing_date"], r["doc_id"]))
    return out


def download_pdf(session, row: dict, sleep=time.sleep) -> tuple[bytes | None, str]:
    content, status = chr_.get_with_retry(session, chr_.pdf_url(int(row["year"]), row["doc_id"]))
    sleep(PAUSE_SECONDS)
    if content is not None and not content.startswith(b"%PDF"):
        return None, "ikke_pdf"
    return content, status


@contextlib.contextmanager
def patched_dirs(root: Path):
    """Point parse_house_ptr's module-level cache directories at <root>/data/live/cache."""
    cache = Path(root) / "data" / "live" / "cache"
    names = ("ROOT", "RAW_DIR", "TEXT_DIR", "LAYOUT_DIR")
    old = {n: getattr(hpp, n) for n in names}
    hpp.ROOT = Path(root)
    hpp.RAW_DIR = cache / "pdf"
    hpp.TEXT_DIR = cache / "text"
    hpp.LAYOUT_DIR = cache / "text_layout"
    try:
        yield
    finally:
        for n, v in old.items():
            setattr(hpp, n, v)


def parse_filing(root: Path, row: dict, pdf_bytes: bytes) -> dict:
    """Parse one downloaded PDF with the backtest's parser.
    Returns {'status': 'parsed'|'unreadable'|'parse_error', 'trades': [row dicts], 'warnings': [...]}.
    'unreadable' = scanned / no text layer (logged as 'ikke læsbar')."""
    with patched_dirs(root):
        chr_.atomic_write(hpp.pdf_path(row), pdf_bytes)
        st = hpp.extract_phase([row], False, None)
        if st["fejl"]:
            return {"status": "parse_error", "trades": [], "warnings": list(st["fejl"].values())}
        trades, _scanned, report = hpp.build_outputs([row])
    rep = report[0]
    if rep["status"] == "scanned":
        return {"status": "unreadable", "trades": [], "warnings": [rep["warnings"]]}
    if rep["status"] != "parsed":
        return {"status": "parse_error", "trades": [], "warnings": [f"status {rep['status']}"]}
    return {"status": "parsed", "trades": trades, "warnings": [w for w in rep["warnings"].split(" | ") if w]}


def finalize(rows: list[dict]) -> dict:
    """Duplicates / deleted / date_suspect / final include_v1 across ALL stored live rows (backtest logic)."""
    return hpp.finalize_trades(rows)


def write_parsed(path: Path, rows: list[dict]) -> None:
    buf = io.StringIO(newline="")
    w = csv.DictWriter(buf, fieldnames=hpp.TRADE_FIELDS, extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    chr_.atomic_write(path, buf.getvalue().encode("utf-8"))


def read_parsed(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return [{f: r.get(f, "") for f in hpp.TRADE_FIELDS} for r in csv.DictReader(fh)]
