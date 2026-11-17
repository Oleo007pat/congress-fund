"""Selection after removing rows whose description says they are not discretionary purchases."""
import numpy as np, pandas as pd
from src.backtest import config
from src.backtest.engine import load_trades
from src.backtest.prices import load_pricebook
from src.backtest.run import Evaluator, build_signal_table
trades = load_trades()
txt = (trades.description.fillna("") + " " + trades.comments.fillna("")).str.lower()
nd = set(trades.loc[txt.str.contains(r"reinvest|dividend|vested|compensation|employee stock|transfer in"), "row_id"])
print("non-discretionary buy rows flagged:", len(nd & set(trades.loc[trades.type == "buy", "row_id"])))
tmap = pd.read_csv(config.TICKER_MAP_CSV, keep_default_na=False, na_values=[])
pb = load_pricebook(set(tmap.loc[tmap["price_symbol"] != "", "price_symbol"]))
sig = build_signal_table(trades, tmap, pb)
ev = Evaluator(sig, pb)
m = lambda tk, pr: ~tk.row_id.isin(nd)
rows = [ev.evaluate("in_sample_cut2021", r, w, x, extra_mask=m)[0] for r in config.EXIT_RULES for w in config.WEIGHTINGS for x in config.X_GRID]
g = pd.DataFrame(rows); g = g[g.n_trades >= 200].sort_values("sharpe", ascending=False)
print(g[["exit_rule", "weighting", "X", "sharpe", "spy_sharpe", "n_trades"]].head(4).to_string())
c = g.iloc[0]
o = ev.evaluate("out_of_sample", c.exit_rule, c.weighting, None if c.X == "none" else float(c.X), extra_mask=m)[0]
print(f"OOS: total {o['total_return']:.4f} sharpe {o['sharpe']:.4f} vs SPY {o['spy_total_return']:.4f}/{o['spy_sharpe']:.4f}")
