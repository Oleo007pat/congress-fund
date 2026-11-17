"""Vectorised re-derivation of entry, reference, hold_12m/member_sells exits and returns for ALL priced signals."""
import numpy as np, pandas as pd
from review.common import COST, ROOT, cal, load_bt, load_trades, px, spy
bt = load_bt(); C = cal(); S = spy()
raw = load_trades().set_index("row_id")
p = bt[bt.entry_idx.notna()].copy()
p["fd"] = pd.to_datetime(p.filing_date); p["td"] = pd.to_datetime(p.transaction_date)
p["exp_entry"] = C[np.searchsorted(C.values, p.fd.values, side="right")]
bad = {}
bad["entry_date"] = (p.exp_entry.dt.strftime("%Y-%m-%d") != p.entry_date)
eo, rp, xp12, ms_px = [], [], [], []
for sym, g in p.groupby("price_symbol"):
    d = px(sym); v = d[(d.volume > 0) & (d.open > 0) & (d.close > 0)]
    eo.append(pd.Series(v.open.reindex(pd.to_datetime(g.entry_date)).to_numpy(), index=g.index))
    # reference: last valid close on/before txn date
    pos = np.searchsorted(v.index.values, g.td.values, side="right") - 1
    rp.append(pd.Series(np.where(pos >= 0, v.close.to_numpy()[np.maximum(pos, 0)], np.nan), index=g.index))
    xp12.append(pd.Series(d.close.reindex(pd.to_datetime(g.hold_12m_exit_date)).to_numpy(), index=g.index))
    k = g.member_sells_exit_price_type == "open"
    ms_px.append(pd.Series(np.where(k, d.open.reindex(pd.to_datetime(g.member_sells_exit_date)).to_numpy(),
                                     d.close.reindex(pd.to_datetime(g.member_sells_exit_date)).to_numpy()), index=g.index))
p["exp_open"] = pd.concat(eo); p["exp_ref"] = pd.concat(rp); p["exp_x12"] = pd.concat(xp12); p["exp_msx"] = pd.concat(ms_px)
bad["entry_open"] = ~np.isclose(p.exp_open, p.entry_open)
okref = p.ref_status == "ok"
bad["ref_price"] = okref & ~np.isclose(p.exp_ref, p.ref_price)
bad["ref_before_entry"] = okref & (pd.to_datetime(p.ref_date) >= pd.to_datetime(p.entry_date))
bad["ref_after_txn"] = okref & (pd.to_datetime(p.ref_date) > p.td)
bad["rise"] = okref & ~np.isclose(100 * (p.entry_open / p.ref_price - 1), p.rise_pct)
# hold_12m horizon exit = first SPY day >= entry + 12 months (next valid bar)
tgt = pd.to_datetime(p.entry_date).apply(lambda x: x + pd.DateOffset(months=12))
first = C[np.minimum(np.searchsorted(C.values, tgt.values, side="left"), len(C) - 1)]
hz = p.hold_12m_exit_reason == "horizon"
bad["h12_exit_before_target"] = hz & (pd.to_datetime(p.hold_12m_exit_date) < first)
bad["h12_exit_not_first_day"] = hz & (pd.to_datetime(p.hold_12m_exit_date) != first)
bad["h12_exit_price"] = ~np.isclose(p.exp_x12, p.hold_12m_exit_price)
g12 = p.hold_12m_exit_price / p.entry_open
bad["h12_net"] = ~np.isclose((1 - COST) ** 2 * g12 - 1, p.hold_12m_net_ret)
spy12 = S.close.reindex(pd.to_datetime(p.hold_12m_exit_date)).to_numpy() / S.open.reindex(pd.to_datetime(p.entry_date)).to_numpy() - 1
bad["h12_spy"] = ~np.isclose(spy12, p.hold_12m_spy_ret)
bad["ms_exit_price"] = ~np.isclose(p.exp_msx, p.member_sells_exit_price)
# member_sells: exit must be strictly after the sale's FILING date (never on/before), and never before entry
ms = p.member_sells_exit_reason.isin(["member_sale", "member_sale_at_entry"])
bad["ms_exit_on_or_before_sale_filing"] = ms & (pd.to_datetime(p.member_sells_exit_date) <= pd.to_datetime(p.first_sale_filing_date)) & (p.member_sells_exit_reason == "member_sale")
bad["ms_exit_before_entry"] = pd.to_datetime(p.member_sells_exit_date) < pd.to_datetime(p.entry_date)
# exit date strictly after sale filing for every member_sale (incl. at-entry: entry is after buy filing)
bad["ms_atentry_sale_filed_after_entry"] = (p.member_sells_exit_reason == "member_sale_at_entry") & (pd.to_datetime(p.first_sale_filing_date) >= pd.to_datetime(p.entry_date))
# independent first-sale filing date
sales = raw[raw.include_v1 & raw.type.isin(["sell", "partial_sale"]) & ~raw.short_related]
sk = {k: g for k, g in sales.groupby(["member", "ticker"])}
exp_sf = []
for r in p.itertuples():
    g = sk.get((r.member, r.ticker))
    q = g[g.transaction_date >= r.transaction_date] if g is not None else None
    exp_sf.append(q.filing_date.min() if q is not None and len(q) else "")
p["exp_sf"] = exp_sf
bad["first_sale_filing"] = p.exp_sf != p.first_sale_filing_date.fillna("")
for k, v in bad.items():
    print(f"{k:38s} mismatches: {int(np.asarray(v).sum())}")
print("priced rows checked:", len(p))
