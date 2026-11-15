"""
price_common.py
---------------
Fælles hjælpefunktioner for prices/-scripts: stier, indlæsning af handler, læsning/skrivning
af kursfiler, fremdriftsfil (fetch_state.json) og no_data.csv.
Kræver kun standardbiblioteket.
"""

from __future__ import annotations

import csv
import json
import os
from datetime import date, datetime, timezone
from pathlib import Path

DAILY_COLS = ["date", "open", "high", "low", "close", "volume"]
NO_DATA_COLS = ["symbol", "reason", "checked_at", "feed", "asof_tried",
                "corp_checked", "corp_note", "name_change_to"]
DEFAULT_START = "2015-12-01"
SPY = "SPY"


class Paths:
    """Alle stier udledes fra projektroden."""

    def __init__(self, root) -> None:
        self.root = Path(root)
        self.data = self.root / "data"
        self.prices = self.data / "prices"
        self.daily = self.prices / "daily"
        self.trades = self.data / "processed" / "trades.csv"
        self.state = self.prices / "fetch_state.json"
        self.meta = self.prices / "fetch_meta.json"
        self.no_data = self.prices / "no_data.csv"
        self.ticker_map = self.prices / "ticker_map.csv"
        self.coverage = self.prices / "coverage_summary.csv"
        self.unmatched = self.prices / "unmatched_trades.csv"
        self.reasons = self.prices / "coverage_reasons.csv"

    def daily_file(self, symbol: str) -> Path:
        return self.daily / (symbol.replace("/", "_") + ".csv")


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_date(text) -> "date | None":
    """Streng ISO-dato (YYYY-MM-DD). Alt andet giver None."""
    if not text:
        return None
    try:
        return date.fromisoformat(str(text).strip()[:10])
    except ValueError:
        return None


def truthy(text) -> bool:
    return str(text).strip().lower() in ("true", "1", "yes", "ja")


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def write_csv(path: Path, columns: list, rows: list) -> None:
    """Skriver en CSV atomisk. rows er dicts eller lister."""
    import io
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(columns)
    for r in rows:
        w.writerow([r.get(c, "") for c in columns] if isinstance(r, dict) else r)
    atomic_write_text(path, buf.getvalue())


# ------------------------------------------------------------------ handler

def load_trades(path: Path) -> list:
    """Læser trades.csv og returnerer kun include_v1-handler med ticker.
    Tilføjer felterne _ticker, _tx, _filed (date eller None). Tåler manglende kolonner."""
    out = []
    with Path(path).open(encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            if not truthy(r.get("include_v1", "")):
                continue
            tk = (r.get("ticker") or "").strip()
            if not tk:
                continue
            r["_ticker"] = tk
            r["_tx"] = parse_date(r.get("transaction_date"))
            r["_filed"] = parse_date(r.get("filing_date"))
            out.append(r)
    return out


# --------------------------------------------------------------- kursfiler

def read_daily(path: Path) -> list:
    """Returnerer [(date, open, high, low, close, volume)] sorteret efter dato."""
    path = Path(path)
    if not path.exists():
        return []
    rows = []
    with path.open(encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            d = parse_date(r["date"])
            if d is None:
                continue
            rows.append((d, float(r["open"]), float(r["high"]), float(r["low"]),
                         float(r["close"]), float(r["volume"] or 0)))
    rows.sort()
    return rows


def write_daily(path: Path, rows: list) -> None:
    def fmt_vol(v):
        return str(int(v)) if float(v).is_integer() else repr(float(v))
    out = [[d.isoformat(), repr(float(o)), repr(float(h)), repr(float(lo)), repr(float(c)), fmt_vol(v)]
           for d, o, h, lo, c, v in sorted(rows)]
    write_csv(path, DAILY_COLS, out)


def read_daily_dates(path: Path) -> list:
    return [r[0] for r in read_daily(path)]


# ------------------------------------------------------ fremdrift og no_data

def load_state(paths: Paths) -> dict:
    if paths.state.exists():
        try:
            st = json.loads(paths.state.read_text(encoding="utf-8"))
            st.setdefault("symbols", {})
            return st
        except ValueError:
            pass
    return {"version": 1, "symbols": {}}


def save_state(paths: Paths, state: dict) -> None:
    atomic_write_text(paths.state, json.dumps(state, indent=1, sort_keys=True))


def load_no_data(paths: Paths) -> dict:
    out = {}
    if paths.no_data.exists():
        with paths.no_data.open(encoding="utf-8", newline="") as fh:
            for r in csv.DictReader(fh):
                out[r["symbol"]] = {c: r.get(c, "") for c in NO_DATA_COLS}
    return out


def save_no_data(paths: Paths, no_data: dict) -> None:
    write_csv(paths.no_data, NO_DATA_COLS, [no_data[k] for k in sorted(no_data)])
