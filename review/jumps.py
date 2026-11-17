"""Scan held symbols for extreme one-day moves and measure their weight in hold_12m trades."""
import numpy as np, pandas as pd
from review.common import load_bt, px
pd.set_option("display.width", 250)
bt = load_bt(); p = bt[bt.entry_idx.notna()]
rows = []
for sym in p.price_symbol.unique():
    d = px(sym); d = d[(d.volume > 0) & (d.open > 0) & (d.close > 0)]
    r = d.close / d.close.shift(1)
    for dt in r[(r > 2) | (r < 0.5)].index:
        rows.append(dict(sym=sym, date=dt, ratio=r[dt], prev_close=d.close.shift(1)[dt], open=d.open[dt], close=d.close[dt], vol=d.volume[dt]))
j = pd.DataFrame(rows)
print("extreme one-day close/close moves in used symbols:", len(j))
# trades held across a jump (hold_12m), contribution
res = []
for r in p.itertuples(index=False):
    jj = j[(j.sym == r.price_symbol) & (j.date > pd.Timestamp(r.entry_date)) & (j.date <= pd.Timestamp(r.hold_12m_exit_date))]
    if len(jj):
        res.append(dict(row_id=r.row_id, sample=r.sample, sym=r.price_symbol, entry=r.entry_date, exit=r.hold_12m_exit_date,
                        jumps=";".join(f"{d.date()}:{x:.2f}" for d, x in zip(jj.date, jj.ratio)), net=r.hold_12m_net_ret,
                        spy=r.hold_12m_spy_ret, w=r.weight_amount, ex=r.weight_amount * (r.hold_12m_net_ret - r.hold_12m_spy_ret)))
res = pd.DataFrame(res)
print(res.groupby("sample").agg(n=("row_id", "size"), w=("w", "sum"), ex=("ex", "sum")))
print(res.sort_values("ex", key=abs, ascending=False).head(25).to_string())
j.to_csv("review/_jumps.csv", index=False)
