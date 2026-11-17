"""Independent selection-sample (cut 2021) Sharpe for hold_6m/amount at several X, from the file's
per-trade fields + raw closes (own loop)."""
import numpy as np, pandas as pd
from review.common import COST, cal, load_bt, metrics, px, spy
bt = load_bt(); C = cal(); S = spy(); n = len(C); pos = {d: i for i, d in enumerate(C)}
cut = int(np.searchsorted(C.values, np.datetime64("2021-12-31"), side="right")) - 1
el = bt[bt.drop_reason.isna() & (bt["sample"] == "in_sample")]
el = el[pd.to_datetime(el.entry_date) <= C[cut]]
cl = {}
def closes(sym):
    if sym not in cl:
        d = px(sym); v = d[(d.volume > 0) & (d.open > 0) & (d.close > 0)]
        a = np.full(n, np.nan); a[[pos[t] for t in v.index]] = v.close.to_numpy(); cl[sym] = a
    return cl[sym]
def book(tk, rule="hold_6m", wcol="weight_amount"):
    start = np.zeros(n); end = np.zeros(n)
    for r in tk.itertuples(index=False):
        c = closes(r.price_symbol); e = int(r.entry_idx); x = int(getattr(r, f"{rule}_exit_idx")); xp = getattr(r, f"{rule}_exit_price")
        if x > cut:
            x = cut; xp = c[:cut + 1][~np.isnan(c[:cut + 1])][-1]
        w = getattr(r, wcol); h = w * (1 - COST) / r.entry_open; last = prev = np.nan
        for t in range(e, x + 1):
            if not np.isnan(c[t]): last = c[t]
            start[t] += w if t == e else h * prev
            end[t] += h * xp * (1 - COST) if t == x else h * last
            prev = last
    f = int(np.flatnonzero(start)[0]); l = int(np.flatnonzero(start)[-1])
    r_ = np.where(start[f:l + 1] > 0, end[f:l + 1] / np.where(start[f:l + 1] > 0, start[f:l + 1], 1) - 1, 0)
    b = np.r_[S.close.iloc[f] / S.open.iloc[f] - 1, S.close.to_numpy()[f + 1:l + 1] / S.close.to_numpy()[f:l] - 1]
    return metrics(C[f:l + 1], r_)["sharpe"], metrics(C[f:l + 1], b)["sharpe"], len(tk)
g = pd.read_csv("results/grid_summary.csv"); g = g[g["sample"] == "in_sample_cut2021"]
for X in (8, 10, 12, None):
    tk = el if X is None else el[el.rise_pct <= X + 1e-7]
    s, sb, k = book(tk)
    lab = "none" if X is None else str(X)
    fr = g[(g.exit_rule == "hold_6m") & (g.weighting == "amount") & (g.X == lab)].iloc[0]
    print(f"hold_6m/amount/X={lab}: mine {s:.6f} (SPY {sb:.6f}, n={k}) file {fr.sharpe:.6f} (SPY {fr.spy_sharpe:.6f}, n={fr.n_trades})")
tk = el[(el.rise_pct <= 30 + 1e-7) & (el.member_canon != "Suzan K. DelBene")]
print("robustness excl DelBene hold_3m/amount/30: mine sharpe %.6f (SPY %.6f) n=%d" % book(tk, "hold_3m"))
print("grid: configs with sharpe > own spy_sharpe:", int((g.sharpe > g.spy_sharpe).sum()), g[g.sharpe > g.spy_sharpe][["exit_rule", "weighting", "X", "sharpe", "spy_sharpe"]].to_string())
