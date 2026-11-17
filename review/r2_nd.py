"""Does the F3 keyword filter favour the result?  Re-run with the 933 rows put back."""
import numpy as np, pandas as pd
from src.backtest import config
from src.backtest.engine import load_trades, add_canonical_member
from src.backtest.prices import load_pricebook
from src.backtest.run import Evaluator, build_signal_table, grid_rows, PRIMARY, SEL
from src.backtest.portfolio import select_configuration
tr = add_canonical_member(load_trades())
tmap = pd.read_csv(config.TICKER_MAP_CSV, keep_default_na=False, na_values=[])
pb = load_pricebook(set(tmap.loc[tmap["price_symbol"] != "", "price_symbol"]))
base = build_signal_table(tr, tmap, pb)
nd_ids = set(base.loc[base.drop_reason.str.startswith("nondiscretionary_"), "row_id"])
cats = base.set_index("row_id").nondiscretionary_category
tr2 = tr.copy(); tr2.loc[tr2.row_id.isin(nd_ids), ["description", "comments"]] = np.nan
sig2 = build_signal_table(tr2, tmap, pb)
ev = Evaluator(sig2, pb); ev0 = Evaluator(base, pb)
def show(lab, rw): print(f"{lab:62s} n={rw['n_trades']:6d} total {rw['total_return']:.4f} sharpe {rw['sharpe']:.4f} | SPY {rw['spy_total_return']:.4f}/{rw['spy_sharpe']:.4f}")
show("baseline (ND excluded) PRIMARY hold_6m/amount/10", ev0.evaluate(PRIMARY, "hold_6m", "amount", 10.0)["row"])
show("ND rows put back       PRIMARY hold_6m/amount/10", ev.evaluate(PRIMARY, "hold_6m", "amount", 10.0)["row"])
for cat in sorted(set(cats[cats != ""])):
    ids = set(cats[cats == cat].index)
    keep_out = nd_ids - ids  # put back only this category
    show(f"  put back only {cat}", ev.evaluate(PRIMARY, "hold_6m", "amount", 10.0, extra_mask=lambda tk, pr, s=keep_out: ~tk.row_id.isin(s))["row"])
show("baseline selection-sample hold_6m/amount/10", ev0.evaluate(SEL, "hold_6m", "amount", 10.0)["row"])
show("ND put back selection-sample hold_6m/amount/10", ev.evaluate(SEL, "hold_6m", "amount", 10.0)["row"])
c = select_configuration(pd.DataFrame(grid_rows(ev, SEL)))
print("selection with ND rows put back:", c.exit_rule, c.weighting, c.X, round(c.sharpe, 4), "spy", round(c.spy_sharpe, 4))
o = ev.evaluate(PRIMARY, c.exit_rule, c.weighting, None if c.X == "none" else float(c.X))["row"]; show("  its PRIMARY OOS", o)
# per-trade outcome of the excluded rows vs kept (hold_6m), OOS and IS
s2 = sig2[sig2.drop_reason == ""]
for smp in ("in_sample", "out_of_sample"):
    a = s2[(s2["sample"] == smp)]
    ex = (a.hold_6m_net_ret - a.hold_6m_spy_ret)
    m = a.row_id.isin(nd_ids)
    print(f"{smp}: excluded ND rows n={int(m.sum())} mean excess {ex[m].mean():+.4f} (amount-w {np.average(ex[m], weights=a.weight_amount[m]) if m.any() else np.nan:+.4f}); kept n={int((~m).sum())} mean excess {ex[~m].mean():+.4f}")
