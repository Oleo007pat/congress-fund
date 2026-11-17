"""Calendar-time portfolio, performance metrics and the in-sample selection.

Portfolio method (documented in ASSUMPTIONS.md):
  Each executed trade i is a buy-and-hold position with starting notional w_i
  (1 for equal weighting, the amount midpoint for amount weighting).  On day t
  the portfolio return is

      r_t = sum_i (End_i,t - Start_i,t) / sum_i Start_i,t

  over all positions open on day t, where
      Start_i,t = w_i                          on the entry day (bought at the open)
                = h_i * C_i,t-1                on later days (value at previous close)
      End_i,t   = h_i * C_i,t                  while the position stays open
                = h_i * P_exit_i * (1 - c)     on the exit day (open or close price)
      h_i       = w_i * (1 - c) / O_i,entry    (shares after the entry cost)
  i.e. the value-weighted average of the positions' daily returns, net of costs on
  entry and exit days.  Days without open positions earn 0 (cash).  C is the last
  valid close carried forward inside the symbol's price segment (a day without a
  trade has return 0 for that position).  The series runs from the first entry day
  to the last exit day of the trades in the configuration.

Implementation: per-symbol difference arrays of h (no Python loop over trades),
cross-checked against a naive per-trade loop in tests/test_backtest.py.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import config


def portfolio_daily_returns(Cz: np.ndarray, col: np.ndarray, e: np.ndarray, x: np.ndarray,
                            entry_px: np.ndarray, exit_px: np.ndarray, w: np.ndarray,
                            cost: float = config.COST):
    """Return (first_idx, last_idx, r) with r[k] = portfolio return on day first_idx+k.

    Cz: (n, S) valuation closes (carried forward inside segments, 0 elsewhere).
    col, e, x: symbol column, entry day index, exit day index per trade (x >= e).
    """
    n, S = Cz.shape
    if len(e) == 0:
        return None, None, np.array([])
    assert np.all(x >= e)
    h = w * (1.0 - cost) / entry_px
    d_end = np.zeros((n + 1, S))
    d_start = np.zeros((n + 2, S))
    np.add.at(d_end, (e, col), h)
    np.add.at(d_end, (x, col), -h)
    np.add.at(d_start, (e + 1, col), h)
    np.add.at(d_start, (x + 1, col), -h)
    cum_end = np.cumsum(d_end[:n], axis=0)        # h active on days e <= t < x
    cum_start = np.cumsum(d_start[:n], axis=0)    # h active on days e <  t <= x
    C_prev = np.vstack([np.zeros((1, S)), Cz[:-1]])
    end_sum = (cum_end * Cz).sum(axis=1) + np.bincount(x, h * exit_px * (1.0 - cost), minlength=n)
    start_sum = (cum_start * C_prev).sum(axis=1) + np.bincount(e, w, minlength=n)
    first, last = int(e.min()), int(x.max())
    ss, es = start_sum[first:last + 1], end_sum[first:last + 1]
    r = np.zeros(len(ss))
    m = ss > 1e-12 * max(1.0, float(np.abs(ss).max()))
    r[m] = es[m] / ss[m] - 1.0
    return first, last, r


def naive_portfolio_daily_returns(Cz, col, e, x, entry_px, exit_px, w, cost=config.COST):
    """Slow reference implementation (one loop per trade) used only by tests."""
    n = Cz.shape[0]
    start = np.zeros(n)
    end = np.zeros(n)
    for i in range(len(e)):
        hi = w[i] * (1 - cost) / entry_px[i]
        for t in range(e[i], x[i] + 1):
            start[t] += w[i] if t == e[i] else hi * Cz[t - 1, col[i]]
            end[t] += hi * exit_px[i] * (1 - cost) if t == x[i] else hi * Cz[t, col[i]]
    first, last = int(min(e)), int(max(x))
    r = np.where(start[first:last + 1] > 0, end[first:last + 1] / np.where(start[first:last + 1] > 0, start[first:last + 1], 1) - 1, 0.0)
    return first, last, r


def bench_daily_returns(o: np.ndarray, c: np.ndarray, first: int, last: int) -> np.ndarray:
    """Benchmark held from the OPEN of `first` to the CLOSE of `last` (no costs)."""
    r = np.empty(last - first + 1)
    r[0] = c[first] / o[first] - 1.0
    r[1:] = c[first + 1:last + 1] / c[first:last] - 1.0
    return r


def series_metrics(dates: np.ndarray, r: np.ndarray, prefix: str = "") -> dict:
    """Total return, CAGR, annualised vol, Sharpe (rf = 0, R12), max drawdown."""
    if len(r) == 0:
        return {f"{prefix}{k}": np.nan for k in
                ("total_return", "cagr", "volatility", "sharpe", "max_drawdown")} | {f"{prefix}n_days": 0}
    eq = np.cumprod(1.0 + r)
    total = eq[-1] - 1.0
    days = (dates[-1] - dates[0]).astype("timedelta64[D]").astype(int) + 1  # open of first .. close of last
    years = days / 365.25
    cagr = eq[-1] ** (1.0 / years) - 1.0 if years > 0 and eq[-1] > 0 else np.nan
    sd = r.std(ddof=1) if len(r) > 1 else np.nan
    vol = sd * np.sqrt(config.ANNUALIZATION)
    sharpe = r.mean() * config.ANNUALIZATION / vol if vol and vol > 0 else np.nan
    peak = np.maximum.accumulate(np.r_[1.0, eq])
    mdd = float(np.min(np.r_[1.0, eq] / peak - 1.0))
    return {f"{prefix}total_return": total, f"{prefix}cagr": cagr, f"{prefix}volatility": vol,
            f"{prefix}sharpe": sharpe, f"{prefix}max_drawdown": mdd, f"{prefix}n_days": len(r)}


def x_label(x) -> str:
    return "none" if x is None else f"{int(x)}"


def select_configuration(grid_in_sample: pd.DataFrame) -> pd.Series:
    """R11: apply the pre-registered criterion (config.SELECTION_TEXT).
    Receives ONLY rows of the selection sample; refuses anything else."""
    samples = set(grid_in_sample["sample"].unique())
    if samples != {config.SELECTION_SAMPLE}:
        raise ValueError(f"selection must see only {config.SELECTION_SAMPLE}, got {samples}")
    g = grid_in_sample[grid_in_sample["n_trades"] >= config.SELECT_MIN_TRADES].copy()
    g = g[np.isfinite(g[config.SELECTION_METRIC])]
    order_exit = {k: i for i, k in enumerate(config.EXIT_RULES)}
    order_w = {k: i for i, k in enumerate(config.WEIGHTINGS)}
    order_x = {x_label(k): i for i, k in enumerate(config.X_GRID)}
    g["_o"] = g["exit_rule"].map(order_exit) * 10000 + g["weighting"].map(order_w) * 100 + g["X"].map(order_x)
    best = g[config.SELECTION_METRIC].max()
    top = g[g[config.SELECTION_METRIC] >= best - 1e-12].sort_values("_o")
    return top.iloc[0].drop(labels="_o")
