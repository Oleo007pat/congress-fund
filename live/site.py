"""Builds docs/data/live.json for the static site (schema documented in live/README.md)."""
from __future__ import annotations

from datetime import datetime, timezone

from live import strategy as strat

DISCLAIMER = "Papirfond med fiktive penge. Ikke investeringsrådgivning."
SCHEMA_VERSION = 1
N_DECISIONS = 25


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def build_curve(history: list[dict]) -> dict:
    """Fund equity and SPY, both normalised to 100 at the first history row that has both values."""
    rows = [(r["date"], _f(r.get("equity")), _f(r.get("spy_close"))) for r in history]
    rows = [r for r in rows if r[1] and r[2]]
    if not rows:
        return {"dates": [], "fund": [], "spy": []}
    e0, s0 = rows[0][1], rows[0][2]
    return {"dates": [r[0] for r in rows], "fund": [round(r[1] / e0 * 100, 4) for r in rows],
            "spy": [round(r[2] / s0 * 100, 4) for r in rows]}


def total_return(curve: dict) -> dict:
    if len(curve["dates"]) < 1:
        return {"fund_pct": None, "spy_pct": None, "excess_pct_points": None}
    f, s = curve["fund"][-1] - 100.0, curve["spy"][-1] - 100.0
    return {"fund_pct": round(f, 2), "spy_pct": round(s, 2), "excess_pct_points": round(f - s, 2)}


def build_positions(lots: dict, positions: dict) -> list[dict]:
    out = []
    for lot in sorted(lots.values(), key=lambda l: (l.get("entry_date", ""), l["lot_id"])):
        if lot.get("status") != "open":
            continue
        cur = _f((positions.get(lot["symbol"]) or {}).get("current_price"))
        entry = _f(lot.get("entry_price"))
        out.append({"ticker": lot["symbol"], "member": lot["member"], "owner": lot["owner"],
                    "filed": lot["filing_date"], "entered": lot.get("entry_date"), "entry_price": entry,
                    "current_price": cur,
                    "return_pct": round((cur / entry - 1) * 100, 2) if cur and entry else None,
                    "planned_exit": lot.get("planned_exit") or "når medlemmet sælger"})
    return out


def build_decisions(log_rows: list[dict]) -> list[dict]:
    sig = [r for r in log_rows if r.get("kind") == "signal" and r.get("on_site") in ("1", 1, True, "True")]
    sig = sig[-N_DECISIONS:][::-1]          # newest first
    return [{"member": r["member"], "ticker": r["ticker"], "filed": r["filing_date"], "decision": r["decision"],
             "reason": r["reason"]} for r in sig]


def build_live_json(state: dict, cfg: dict, history: list[dict], positions: dict, log_rows: list[dict],
                    last_run: dict, now: datetime | None = None) -> dict:
    curve = build_curve(history)
    changed = bool(state.get("strategy_changes"))
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": (now or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
        "start_date": state["start_date"],
        "start_equity": state["start_equity"],
        "last_run": last_run,
        "latest_equity": _f(history[-1].get("equity")) if history else None,
        "curve": curve,
        "total_return": total_return(curve),
        "positions": build_positions(state["lots"], positions),
        "decisions": build_decisions(log_rows),
        "strategy": {"name": cfg["name"], "hash": strat.strategy_hash(cfg), "changed_since_start": changed,
                     "description": strat.describe_da(cfg),
                     "config": {"exit_rule": cfg["exit"]["rule"], "weighting": cfg["weighting"],
                                "skip_rise_pct": cfg["skip_rise_pct"],
                                "member_filter_enabled": cfg["member_filter"]["enabled"]}},
        "disclaimer": DISCLAIMER,
    }
