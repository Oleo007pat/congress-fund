"""F6 round 2: positions exited at a piece end although the symbol trades again later."""
import numpy as np, pandas as pd
from review.common import load_bt, px, cal
pd.set_option("display.width", 250)
bt = load_bt(); C = cal()
el = bt[bt.drop_reason.isna()]
for rule in ("hold_6m", "hold_12m", "member_sells"):
    d = el[el[f"{rule}_exit_reason"] == "delisted"]
    out = []
    for r in d.itertuples(index=False):
        v = px(r.price_symbol); v = v[(v.volume > 0) & (v.open > 0) & (v.close > 0)]
        xd = pd.Timestamp(getattr(r, f"{rule}_exit_date"))
        after = v[v.index > xd]
        if not len(after):
            continue
        gap = int(((C > xd) & (C < after.index[0])).sum())
        ratio = after.open.iloc[0] / getattr(r, f"{rule}_exit_price")
        planned = getattr(r, f"{rule}_planned_exit")
        out.append(dict(row_id=r.row_id, sample=r.sample, sym=r.price_symbol, entry=r.entry_date, exit=xd.date(), planned=planned,
                        resume=after.index[0].date(), gap_td=gap, resume_ratio=round(ratio, 3),
                        why=("jump" if (ratio > 2 or ratio < .5) else "") + ("+long" if gap > 63 else ""),
                        planned_after_resume=str(planned) >= str(after.index[0].date()), w=r.weight_amount,
                        net=round(getattr(r, f"{rule}_net_ret"), 4)))
    o = pd.DataFrame(out)
    print(f"\n== {rule}: delisted exits {len(d)}, of which symbol trades again later: {len(o)}")
    if len(o):
        print(o.groupby(["why", "planned_after_resume"]).size())
        print(o[o.planned_after_resume].sort_values("resume_ratio").to_string())
