"""Independent helpers for the bias review (do not import src/backtest)."""
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
PD = ROOT / "data" / "prices" / "daily"
COST = 0.001


@lru_cache(maxsize=None)
def px(sym):
    df = pd.read_csv(PD / f"{sym}.csv")
    df["date"] = pd.to_datetime(df["date"])
    return df.set_index("date")


@lru_cache(maxsize=None)
def spy():
    return px("SPY")


def cal():
    return spy().index


def load_bt():
    return pd.read_csv(ROOT / "results" / "trades_backtest.csv", low_memory=False)


def load_trades():
    t = pd.read_csv(ROOT / "data" / "processed" / "trades.csv", low_memory=False,
                    keep_default_na=False, na_values=[""])
    t["row_id"] = t.index
    for c in ("include_v1", "short_related"):
        t[c] = t[c].astype(str).str.lower().eq("true")
    return t


def metrics(dates, r):
    r = np.asarray(r, float)
    eq = np.cumprod(1 + r)
    days = (dates[-1] - dates[0]).days + 1
    yrs = days / 365.25
    cagr = eq[-1] ** (1 / yrs) - 1
    vol = r.std(ddof=1) * np.sqrt(252)
    sh = r.mean() * 252 / vol
    peak = np.maximum.accumulate(np.r_[1, eq])
    mdd = (np.r_[1, eq] / peak - 1).min()
    return dict(total=eq[-1] - 1, cagr=cagr, vol=vol, sharpe=sh, mdd=mdd, n=len(r))
