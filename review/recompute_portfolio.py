"""Independent recomputation of the calendar-time portfolio (naive per-trade loop
built from the raw price CSVs) for a given exit rule / weighting, X = none.
Usage: python3 -m review.recompute_portfolio [rule] [weighting]
"""
import sys

import numpy as np
import pandas as pd

from review.common import COST, cal, load_bt, metrics, px, spy

rule = sys.argv[1] if len(sys.argv) > 1 else "hold_12m"
wt = sys.argv[2] if len(sys.argv) > 2 else "amount"

bt = load_bt()
C = cal()
n = len(C)
pos = {d: i for i, d in enumerate(C)}

closes = {}


def valid_close(sym):
    if sym not in closes:
        df = px(sym)
        ok = (df.volume > 0) & (df.open > 0) & (df.close > 0)
        s = df.loc[ok, "close"].reindex(C)
        closes[sym] = s.to_numpy()
    return closes[sym]


def run(sample, prefix=""):
    s = bt[(bt["sample"] == sample) & bt.entry_idx.notna()]
    if prefix:
        s = s[s[f"{prefix}{rule}_exit_idx"].notna()]
    start = np.zeros(n)
    end = np.zeros(n)
    for r in s.itertuples(index=False):
        d = r._asdict()
        e = pos[pd.Timestamp(d["entry_date"])]
        x = pos[pd.Timestamp(d[f"{prefix}{rule}_exit_date"])]
        w = d[f"weight_{wt}"]
        h = w * (1 - COST) / d["entry_open"]
        c = valid_close(d["price_symbol"]).copy()
        # forward fill from entry (independent of the code's segment logic)
        last = np.nan
        for t in range(e, x + 1):
            if not np.isnan(c[t]):
                last = c[t]
            ct = last
            st = w if t == e else h * prev
            if t == x:
                en = h * d[f"{prefix}{rule}_exit_price"] * (1 - COST)
            else:
                en = h * ct
            start[t] += st
            end[t] += en
            prev = ct
    first = int(np.flatnonzero(start)[0]); lastd = int(np.flatnonzero(start)[-1])
    r = np.where(start[first:lastd + 1] > 0, end[first:lastd + 1] / np.where(start[first:lastd + 1] > 0, start[first:lastd + 1], 1) - 1, 0)
    dates = C[first:lastd + 1]
    sp = spy()
    br = np.r_[sp.close.iloc[first] / sp.open.iloc[first] - 1, sp.close.iloc[first + 1:lastd + 1].to_numpy() / sp.close.iloc[first:lastd].to_numpy() - 1]
    return dates, r, br


if __name__ == "__main__":
    g = pd.read_csv("results/grid_summary.csv")
    for sample, prefix, gs in (("in_sample", "", "in_sample"), ("out_of_sample", "", "out_of_sample"),
                               ("in_sample", "cut_", "in_sample_cut2021")):
        dates, r, br = run(sample, prefix)
        m, mb = metrics(dates, r), metrics(dates, br)
        row = g[(g["sample"] == gs) & (g.exit_rule == rule) & (g.weighting == wt) & (g.X == "none")].iloc[0]
        print(f"== {gs} {rule} {wt}  {dates[0].date()}..{dates[-1].date()}")
        for k, kk in (("total", "total_return"), ("cagr", "cagr"), ("vol", "volatility"), ("sharpe", "sharpe"), ("mdd", "max_drawdown")):
            print(f"  {k:7s} mine {m[k]: .6f} file {row[kk]: .6f} | SPY mine {mb[k]: .6f} file {row['spy_' + kk]: .6f}")
        np.save(f"review/_r_{gs}_{rule}_{wt}.npy", np.c_[dates.values.astype('datetime64[D]').astype(int), r, br])
