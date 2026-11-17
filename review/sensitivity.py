"""Sensitivities using the author's Evaluator (its portfolio math was verified
independently by review/recompute_portfolio.py).  Read-only use of src/."""
import numpy as np
import pandas as pd

from src.backtest import config
from src.backtest.engine import load_trades
from src.backtest.prices import load_pricebook
from src.backtest.run import Evaluator, build_signal_table

pd.set_option("display.width", 250)
trades = load_trades()
tmap = pd.read_csv(config.TICKER_MAP_CSV, keep_default_na=False, na_values=[])
pb = load_pricebook(set(tmap.loc[tmap["price_symbol"] != "", "price_symbol"]))
sig = build_signal_table(trades, tmap, pb)
sig["identical_row_repeat"] = False
ev = Evaluator(sig, pb)
K = ["n_trades", "total_return", "cagr", "sharpe", "spy_total_return", "spy_sharpe", "mean_excess_vs_spy"]


def show(label, row):
    print(f"{label:70s} " + " ".join(f"{k}={row.get(k, np.nan):.4f}" if isinstance(row.get(k), float) else f"{k}={row.get(k)}" for k in K))


GREEN = {"Mark Green", "Mark Dr Green"}
# 1. Concentration, OOS, recommended config
print("== 1. OOS hold_12m amount X=none: concentration")
show("base", ev.evaluate("out_of_sample", "hold_12m", "amount", None)[0])
show("equal weighting", ev.evaluate("out_of_sample", "hold_12m", "equal", None)[0])
show("excl. Mark Green (both spellings)", ev.evaluate("out_of_sample", "hold_12m", "amount", None, extra_mask=lambda tk, pr: ~tk.member.isin(GREEN))[0])
show("excl. Nancy Pelosi", ev.evaluate("out_of_sample", "hold_12m", "amount", None, extra_mask=lambda tk, pr: tk.member != "Nancy Pelosi")[0])
show("excl. Mark Green + Pelosi", ev.evaluate("out_of_sample", "hold_12m", "amount", None, extra_mask=lambda tk, pr: ~tk.member.isin(GREEN | {"Nancy Pelosi"}))[0])
oos = sig[(sig["sample"] == "out_of_sample") & sig.entry_idx.notna()]
ex = oos.weight_amount * (oos.hold_12m_net_ret - oos.hold_12m_spy_ret)
top10 = set(oos.loc[ex.nlargest(10).index, "row_id"])
show("excl. 10 trades with largest $ excess", ev.evaluate("out_of_sample", "hold_12m", "amount", None, extra_mask=lambda tk, pr: ~tk.row_id.isin(top10))[0])
capw = lambda tk, pr: np.ones(len(tk), bool)
# amount weight capped at 50k midpoint
sig["weight_cap"] = sig.weight_amount.clip(upper=32500.5)
show("amount weight capped at 32,500.5 (the $15k-50k midpoint)", ev.evaluate("out_of_sample", "hold_12m", "cap", None)[0])

# 2. In-sample selection: DelBene (spouse MSFT RSU vesting, 'Transfer in of vested stock')
print("\n== 2. Selection sample (in_sample_cut2021), hold_12m X=none")
for wt in ("amount", "equal"):
    show(f"{wt} base", ev.evaluate("in_sample_cut2021", "hold_12m", wt, None)[0])
    show(f"{wt} excl. Suzan K. DelBene", ev.evaluate("in_sample_cut2021", "hold_12m", wt, None, extra_mask=lambda tk, pr: tk.member != "Suzan K. DelBene")[0])
# full selection re-run excluding DelBene
rows = []
for rule in config.EXIT_RULES:
    for wt in config.WEIGHTINGS:
        for x in config.X_GRID:
            r = ev.evaluate("in_sample_cut2021", rule, wt, x, extra_mask=lambda tk, pr: tk.member != "Suzan K. DelBene")[0]
            rows.append(r)
g = pd.DataFrame(rows)
g = g[g.n_trades >= 200].sort_values("sharpe", ascending=False)
print("selection excl. DelBene, top 5:\n", g[["exit_rule", "weighting", "X", "sharpe", "spy_sharpe", "cagr", "n_trades"]].head(5).to_string())
chosen = g.iloc[0]
show(f"OOS of that choice ({chosen.exit_rule},{chosen.weighting},{chosen.X})",
     ev.evaluate("out_of_sample", chosen.exit_rule, chosen.weighting, None if chosen.X == "none" else float(chosen.X))[0])

# 3. Missing reference taken (instead of skipped) in the selection sample: does it change the X ranking?
print("\n== 3. Selection sample, ref-missing taken instead of skipped (hold_12m amount)")
for x in (None, 30.0, 20.0, 10.0, 0.0):
    a = ev.evaluate("in_sample_cut2021", "hold_12m", "amount", x)[0]
    b = ev.evaluate("in_sample_cut2021", "hold_12m", "amount", x,
                    rise_override=lambda p: np.nan_to_num(p["rise_pct"].to_numpy(float) / 100.0, nan=-1.0))[0]
    print(f"  X={x}: sharpe skip-missing {a['sharpe']:.4f} (n={a['n_trades']})  take-missing {b['sharpe']:.4f} (n={b['n_trades']})")

# 4. Bankrupt / failed delistings set to -100 %
print("\n== 4. Delisted exits with last price < 1/4 of entry treated as total loss")
sig2 = sig.copy()
for rule in ("hold_12m", "member_sells"):
    m = (sig2[f"{rule}_exit_reason"] == "delisted") & (sig2[f"{rule}_exit_price"] < 0.25 * sig2.entry_open)
    print(f"  {rule}: {int(m.sum())} trades affected:", sorted(set(sig2.loc[m, 'price_symbol'])))
for s in ("out_of_sample", "in_sample"):
    show(f"{s} hold_12m amount base", ev.evaluate(s, "hold_12m", "amount", None)[0])
