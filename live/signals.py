"""Pure signal logic for the live fund: reference price, skip rule, sizing and caps.

The skip decision itself is src.backtest.engine.skip_decision (identical semantics: skip ONLY on a rise
greater than X).  Differences from the backtest are listed in live/README.md ("Afvigelser fra backtesten").
"""
from __future__ import annotations

import math
from datetime import date, datetime

from src.backtest import config as bt_config
from src.backtest.engine import amount_weight, skip_decision

# Danish explanations of the backtest's drop reasons (select_buy_signals).
DROP_REASON_DA = {
    "short_related_excluded": "short-relateret handel (dækning af shortposition)",
    "exact_duplicate_row": "dublet: identisk række i samme indberetning",
    "cross_doc_duplicate": "dublet: samme handel er indberettet i en tidligere indberetning",
}


def drop_reason_da(code: str) -> str:
    if code in DROP_REASON_DA:
        return DROP_REASON_DA[code]
    if code.startswith("nondiscretionary_"):
        return "ikke medlemmets eget valg (%s)" % code[len("nondiscretionary_"):].replace("_", " ")
    return code


def _bar_date(bar: dict) -> date:
    return datetime.fromisoformat(str(bar["t"]).replace("Z", "+00:00")).date()


def valid_bar(bar: dict) -> bool:
    """Same validity as the backtest: volume > 0 and open, close > 0."""
    try:
        return float(bar.get("v", 0)) > 0 and float(bar["o"]) > 0 and float(bar["c"]) > 0
    except (KeyError, TypeError, ValueError):
        return False


def reference_close(bars: list[dict], txn_date: str) -> tuple[float | None, str, str]:
    """Close on the member's transaction date, or the last valid close BEFORE it (never a later one),
    at most REF_MAX_STALENESS_DAYS calendar days old.  Returns (price, bar_date, status)."""
    t = date.fromisoformat(txn_date)
    cands = [b for b in bars if valid_bar(b) and _bar_date(b) <= t]
    if not cands:
        return None, "", "ref_missing_no_bar_on_or_before_txn_date"
    b = max(cands, key=_bar_date)
    if (t - _bar_date(b)).days > bt_config.REF_MAX_STALENESS_DAYS:
        return None, _bar_date(b).isoformat(), "ref_missing_stale"
    return float(b["c"]), _bar_date(b).isoformat(), "ok"


def latest_close(bars: list[dict]) -> tuple[float | None, str]:
    """Most recent valid daily close (adjustment=all, i.e. the same series as the reference)."""
    cands = [b for b in bars if valid_bar(b)]
    if not cands:
        return None, ""
    b = max(cands, key=_bar_date)
    return float(b["c"]), _bar_date(b).isoformat()


def snapshot_price(snap: dict | None) -> float | None:
    """Latest IEX trade, else today's/prior daily-bar close, from a snapshot."""
    if not isinstance(snap, dict):
        return None
    for path in (("latestTrade", "p"), ("dailyBar", "c"), ("prevDailyBar", "c")):
        v = snap.get(path[0])
        if isinstance(v, dict) and isinstance(v.get(path[1]), (int, float)) and v[path[1]] > 0:
            return float(v[path[1]])
    return None


def rise_since_reference(last: float, ref: float) -> float:
    return last / ref - 1.0


def should_skip(rise: float, x_pct: float | None) -> bool:
    """Skip ONLY on a rise greater than X percent (None = never skip).  A fall never skips."""
    return skip_decision(rise, x_pct)


def midpoint(row: dict) -> float:
    return amount_weight(row["amount_low"], row["amount_high"], row.get("amount_flag"))


def target_notional(equity: float, weighting: str, amount_mid: float, sizing: dict) -> float:
    """equity x base % x clip(amount_mid / median, clip_min, clip_max); 'equal' = base % flat."""
    base = equity * sizing["base_pct_of_equity"] / 100.0
    if weighting == "equal":
        return base
    ratio = amount_mid / sizing["median_midpoint_usd"]
    return base * min(max(ratio, sizing["clip_min"]), sizing["clip_max"])


def apply_position_cap(notional: float, equity: float, existing_value: float, sizing: dict) -> tuple[float, bool]:
    """Hard cap per position (symbol): existing market value + already committed + new <= cap % of equity."""
    room = equity * sizing["max_position_pct_of_equity"] / 100.0 - existing_value
    room = max(room, 0.0)
    return (room, True) if notional > room else (notional, False)


def apply_cash_limit(notional: float, available_cash: float, gap_buffer_pct: float) -> tuple[float, bool]:
    """Cash only: the order (plus a gap buffer for the open price) must fit in available cash.
    Returns (possibly scaled-down notional, scaled?).  Never negative."""
    max_notional = max(available_cash, 0.0) / (1.0 + gap_buffer_pct / 100.0)
    return (max_notional, True) if notional > max_notional else (notional, False)


def whole_shares(notional: float, price: float) -> int:
    return int(math.floor(notional / price + 1e-9)) if price > 0 else 0
