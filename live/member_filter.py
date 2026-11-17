"""Optional member-selection filter (hook; DISABLED by default in live/strategy.json).

A member's track record is built only from COMPLETED, COPIED trades of this fund (lots that were entered and
exited) within a trailing window ending at the decision date.  Score = shrunk mean excess return vs SPY:

    score = sum(excess_i) / (n + K)          (shrinks the mean toward 0 with K pseudo-observations)

Rules: "score_gt_0" (score > 0) or "top_percentile" (member ranks in the top P % of scored members).
Members with fewer than min_completed_trades completed trades have insufficient history; the configured
on_insufficient_history ("skip" = conservative default, or "allow") decides.

The history is injected (history_fn(as_of) -> {member: [{"exit_date": "YYYY-MM-DD", "excess": float}, ...]}),
so the rule can be unit-tested with a stub; lots_history() builds it from the fund's own closed lots.
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Callable

from src.backtest.engine import trade_returns


def shrunk_score(excess: list[float], k: float) -> float:
    return sum(excess) / (len(excess) + k) if (len(excess) + k) > 0 else 0.0


class MemberFilter:
    def __init__(self, cfg: dict, history_fn: Callable[[date], dict]):
        self.cfg = cfg
        self.history_fn = history_fn

    def scores(self, as_of: date) -> dict[str, tuple[int, float]]:
        """member -> (n completed trades in window, shrunk score)."""
        start = as_of - timedelta(days=int(self.cfg["trailing_window_days"]))
        out = {}
        for member, trades in self.history_fn(as_of).items():
            ex = [t["excess"] for t in trades
                  if t.get("excess") is not None and start < date.fromisoformat(t["exit_date"]) <= as_of]
            if ex:
                out[member] = (len(ex), shrunk_score(ex, float(self.cfg["shrinkage_k"])))
        return out

    def allows(self, member: str, as_of: date) -> tuple[bool, str]:
        cfg = self.cfg
        if not cfg.get("enabled"):
            return True, "medlemsfilter er slået fra"
        scores = self.scores(as_of)
        n, score = scores.get(member, (0, 0.0))
        if n < int(cfg["min_completed_trades"]):
            if cfg.get("on_insufficient_history", "skip") == "allow":
                return True, f"medlemsfilter: for få afsluttede handler ({n}), tillader"
            return False, f"medlemsfilter: for få afsluttede handler ({n} < {int(cfg['min_completed_trades'])})"
        if cfg["rule"] == "score_gt_0":
            ok = score > 0
            return ok, f"medlemsfilter: score {score:.4f} {'>' if ok else '<='} 0 (n={n})"
        eligible = sorted((s for m, (c, s) in scores.items() if c >= int(cfg["min_completed_trades"])), reverse=True)
        keep = max(1, math.ceil(len(eligible) * float(cfg["top_percentile"]) / 100.0))
        cutoff = eligible[keep - 1]
        ok = score >= cutoff
        return ok, (f"medlemsfilter: score {score:.4f} {'i' if ok else 'ikke i'} top {cfg['top_percentile']:g} % "
                    f"(grænse {cutoff:.4f}, n={n})")


def lots_history(lots: dict) -> Callable[[date], dict]:
    """Completed copied trades from closed lots: excess = net return (backtest cost model) - SPY return over
    the same window.  Lots without stored SPY prices are left out (never guessed)."""
    def fn(as_of: date) -> dict:
        out: dict[str, list[dict]] = {}
        for lot in lots.values():
            if lot.get("status") != "closed" or not lot.get("exit_date"):
                continue
            if not (lot.get("entry_price") and lot.get("exit_price") and lot.get("spy_entry") and lot.get("spy_exit")):
                continue
            if date.fromisoformat(lot["exit_date"]) > as_of:
                continue
            _gross, net = trade_returns(float(lot["entry_price"]), float(lot["exit_price"]))
            excess = net - (float(lot["spy_exit"]) / float(lot["spy_entry"]) - 1.0)
            out.setdefault(lot["member_canon"], []).append({"exit_date": lot["exit_date"], "excess": excess})
        return out
    return fn
