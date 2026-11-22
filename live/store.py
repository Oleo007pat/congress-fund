"""File storage for the live fund: state.json, history.csv, trades_log.csv, parsed_trades.csv.

Dry-run writes to a sandbox (data/live/dryrun/, docs/data/live_dryrun.json), seeded with a copy of the
real files, so a dry run can never consume filings or change the real fund.
"""
from __future__ import annotations

import csv
import io
import json
import os
import shutil
from pathlib import Path

HISTORY_FIELDS = ["date", "equity", "cash", "n_positions", "spy_close", "spy_close_date", "sim_pnl"]
# equity = Alpaca equity + sim_pnl (P&L of simulated fills, live/simfill.py); empty sim_pnl = none recorded
LOG_FIELDS = ["ts_utc", "kind", "doc_id", "member", "owner", "ticker", "symbol", "filing_date", "txn_date",
              "trade_type", "amount_low", "amount_high", "decision", "reason", "ref_close", "ref_date",
              "last_price", "rise_pct", "side", "tif", "notional", "qty", "client_order_id", "order_id",
              "order_status", "filled_qty", "fill_price", "fill_time", "on_site", "dry_run"]
STATE_VERSION = 1


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def write_json(path: Path, obj) -> None:
    text = json.dumps(obj, indent=1, sort_keys=True, ensure_ascii=False) + "\n"
    atomic_write_bytes(path, text.encode("utf-8"))


class LivePaths:
    def __init__(self, root: Path, dry_run: bool = False):
        self.root = Path(root)
        self.dry_run = dry_run
        real = self.root / "data" / "live"
        self.real_dir = real
        self.dir = real / "dryrun" if dry_run else real
        self.cache = real / "cache"                       # PDFs and extracted text; never committed
        self.state = self.dir / "state.json"
        self.history = self.dir / "history.csv"
        self.trades_log = self.dir / "trades_log.csv"
        self.parsed = self.dir / "parsed_trades.csv"
        docs = self.root / "docs" / "data"
        self.live_json = docs / ("live_dryrun.json" if dry_run else "live.json")
        self.strategy = self.root / "live" / "strategy.json"

    def seed_dry_run(self) -> None:
        """Dry run: start from a copy of the real files (if any), discarding the previous sandbox."""
        if not self.dry_run:
            return
        if self.dir.exists():
            shutil.rmtree(self.dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        for name in ("state.json", "history.csv", "trades_log.csv", "parsed_trades.csv"):
            src = self.real_dir / name
            if src.is_file():
                shutil.copy2(src, self.dir / name)


# ----------------------------------------------------------------------------- state

def new_state(start_date: str, start_equity: float, started_at: str, strategy_hash: str, strategy_name: str) -> dict:
    return {
        "version": STATE_VERSION,
        "start_date": start_date,
        "start_equity": start_equity,
        "started_at": started_at,
        "strategy_hash": strategy_hash,
        "strategy_name": strategy_name,
        "strategy_changes": [],
        "seen_doc_ids": {},        # doc_id -> {filing_date, status, seen_at}
        "doc_failures": {},        # doc_id -> number of failed download/parse attempts
        "lots": {},                # lot_id -> lot dict (see live/execution.py)
        "pending_orders": {},      # client_order_id -> pending order dict
        "sim_pending": {},         # client_order_id -> queued simulated buy (live/simfill.py)
        "capabilities": {"notional_opg": None},
        "last_run": {},
    }


def load_state(path: Path) -> dict | None:
    if not path.is_file():
        return None
    state = json.loads(path.read_text(encoding="utf-8"))
    for key, default in (("seen_doc_ids", {}), ("doc_failures", {}), ("lots", {}), ("pending_orders", {}),
                         ("capabilities", {"notional_opg": None}), ("strategy_changes", []), ("last_run", {}),
                         ("sim_pending", {})):
        state.setdefault(key, default)
    return state


def save_state(path: Path, state: dict) -> None:
    write_json(path, state)


# ----------------------------------------------------------------------------- csv helpers

def read_csv(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    buf = io.StringIO(newline="")
    w = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    atomic_write_bytes(path, buf.getvalue().encode("utf-8"))


def append_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    if not rows:
        return
    existing = read_csv(path)
    write_csv(path, fields, existing + rows)


def upsert_history(path: Path, row: dict) -> list[dict]:
    """One row per run date: a rerun on the same date replaces that day's row."""
    rows = [r for r in read_csv(path) if r.get("date") != row["date"]]
    rows.append({k: row.get(k, "") for k in HISTORY_FIELDS})
    rows.sort(key=lambda r: r["date"])
    write_csv(path, HISTORY_FIELDS, rows)
    return rows


def rewrite_history(path: Path, rows: list[dict]) -> list[dict]:
    rows = sorted(({k: r.get(k, "") for k in HISTORY_FIELDS} for r in rows), key=lambda r: r["date"])
    write_csv(path, HISTORY_FIELDS, rows)
    return rows
