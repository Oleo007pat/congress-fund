"""Continuous (live-like) portfolio: all signals 2016..today in one book; report the
calendar window 2022-01-03..end (in-sample-filed positions still open stay in the book)."""
import numpy as np, pandas as pd
from src.backtest import config
from src.backtest.engine import load_trades
from src.backtest.prices import load_pricebook
from src.backtest.run import Evaluator, build_signal_table
from src.backtest.portfolio import series_metrics
trades = load_trades()
tmap = pd.read_csv(config.TICKER_MAP_CSV, keep_default_na=False, na_values=[])
pb = load_pricebook(set(tmap.loc[tmap["price_symbol"] != "", "price_symbol"]))
sig = build_signal_table(trades, tmap, pb)
sig["sample"] = "all"
ev = Evaluator(sig, pb)
G = {"Mark Green", "Mark Dr Green"}
for lab, kw in (("all", {}), ("excl Mark Green", dict(extra_mask=lambda tk, pr: ~tk.member.isin(G)))):
    for wt in ("amount", "equal"):
        row, dates, r, br = ev.evaluate("all", "hold_12m", wt, None, **kw)
        for a, b in (("2016-01-05", "2021-12-31"), ("2022-01-03", "2026-11-16")):
            m = (dates >= np.datetime64(a)) & (dates <= np.datetime64(b))
            s = series_metrics(dates[m], r[m]); sb = series_metrics(dates[m], br[m])
            print(f"{lab:16s} {wt:6s} {a}..{b}: total {s['total_return']:.4f} cagr {s['cagr']:.4f} sharpe {s['sharpe']:.4f} mdd {s['max_drawdown']:.4f} | SPY total {sb['total_return']:.4f} sharpe {sb['sharpe']:.4f}")
