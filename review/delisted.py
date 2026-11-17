"""Delisted exits and segment (gap) look-ahead for a given exit rule."""
import sys

import numpy as np
import pandas as pd

from review.common import load_bt, px, cal

rule = sys.argv[1] if len(sys.argv) > 1 else "hold_12m"
bt = load_bt()
C = cal()
d = bt[bt[f"{rule}_exit_reason"] == "delisted"].copy()
print(f"{rule}: delisted exits {len(d)}  by sample:", d["sample"].value_counts().to_dict())
print("net ret mean %.4f median %.4f; spy mean %.4f" % (d[f"{rule}_net_ret"].mean(), d[f"{rule}_net_ret"].median(), d[f"{rule}_spy_ret"].mean()))
print("amount weight share of delisted in sample:",
      (d.groupby("sample").weight_amount.sum() / bt[bt.entry_idx.notna()].groupby("sample").weight_amount.sum()).round(4).to_dict())

rows = []
for r in d.itertuples(index=False):
    df = px(r.price_symbol)
    ok = df[(df.volume > 0) & (df.open > 0) & (df.close > 0)]
    xd = pd.Timestamp(getattr(r, f"{rule}_exit_date"))
    after = ok[ok.index > xd]
    planned = getattr(r, f"{rule}_planned_exit")
    lastbar = df.index.max()
    nxt = after.iloc[0] if len(after) else None
    gap_td = int(((C > xd) & (C < after.index[0])).sum()) if len(after) else None
    zero_after = int(((df.index > xd) & (df.volume == 0)).sum())
    rows.append(dict(row_id=r.row_id, sample=r.sample, sym=r.price_symbol, member=r.member, entry=r.entry_date,
                     exit=xd.date(), planned=planned, exit_px=getattr(r, f"{rule}_exit_price"),
                     net=getattr(r, f"{rule}_net_ret"), w=r.weight_amount,
                     resumes=nxt is not None, resume_date=None if nxt is None else after.index[0].date(),
                     gap_tdays=gap_td, resume_open=None if nxt is None else nxt.open,
                     resume_vs_exit=None if nxt is None else nxt.open / getattr(r, f"{rule}_exit_price") - 1,
                     zero_vol_bars_after=zero_after, raw_last_bar=lastbar.date()))
o = pd.DataFrame(rows)
res = o[o.resumes]
print(f"\nDelisted exits whose symbol TRADES AGAIN later (segment split by gap>10d): {len(res)}")
# planned exit must be after resume for the look-ahead to matter
res = res.assign(planned_after_resume=pd.to_datetime(res.planned) >= pd.to_datetime(res.resume_date))
print(res.sort_values("resume_vs_exit").to_string(max_rows=80))
o.to_csv(f"review/_delisted_{rule}.csv", index=False)
