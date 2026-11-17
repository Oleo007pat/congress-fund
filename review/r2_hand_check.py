"""Round 2 hand check: 10 NEW random trades (seed 20261118) from the eligible rows of
results/trades_backtest.csv: 8 random + 1 hold_6m 'delisted' + 1 member_sells 'member_sale'.
Independent of src/: own piece rule, own name normalisation."""
import re
import numpy as np, pandas as pd
from review.common import COST, ROOT, cal, load_bt, load_trades, px, spy
SEED = 20261118
rng = np.random.default_rng(SEED)
bt = load_bt(); el = bt[bt.drop_reason.isna()]
pick = [int(x) for x in rng.choice(el.row_id.to_numpy(), 8, replace=False)]
pick.append(int(rng.choice(el.loc[el.hold_6m_exit_reason == "delisted", "row_id"].to_numpy())))
pick.append(int(rng.choice(el.loc[el.member_sells_exit_reason == "member_sale", "row_id"].to_numpy())))
raw = load_trades()
H = {"hon", "honorable", "dr", "mr", "mrs", "ms", "jr", "sr", "ii", "iii", "md", "facs"}
def key(name, st):
    t = [x for x in re.sub(r"[^a-z ]", " ", str(name).lower()).split() if x not in H and len(x) > 1]
    return f"{t[0]} {t[-1]}|{str(st)[:2]}"
raw["mk"] = [key(a, b) for a, b in zip(raw.member, raw.state_dst)]
tmap = pd.read_csv(ROOT / "data/prices/ticker_map.csv", keep_default_na=False).set_index("filing_ticker")
C = cal(); S = spy(); pos = {d: i for i, d in enumerate(C)}

def pieces(sym):
    d = px(sym); v = d[(d.volume > 0) & (d.open > 0) & (d.close > 0)]
    idx = np.array([pos[t] for t in v.index]); o = v.open.to_numpy(); c = v.close.to_numpy()
    g = np.diff(idx) - 1; j = o[1:] / c[:-1]
    brk = (g > 10) & ((j > 2) | (j < .5) | (g > 63))
    return v, idx, np.r_[0, np.cumsum(brk)]

out = []
for rid in pick:
    r = raw.loc[rid]; f = bt.set_index("row_id").loc[rid]
    sym = tmap.loc[r.ticker, "price_symbol"]
    v, idx, pid = pieces(sym)
    e = int(np.searchsorted(C.values, np.datetime64(r.filing_date), side="right")); ed = C[e]
    eo = v.loc[ed, "open"]
    ref = v[v.index <= pd.Timestamp(r.transaction_date)]; refd, refpx = ref.index[-1], ref.close.iloc[-1]
    rise = 100 * (eo / refpx - 1)
    k = int(np.searchsorted(idx, e)); pend = idx[pid == pid[k]][-1]
    tgt = ed + pd.DateOffset(months=6); p = int(np.searchsorted(C.values, np.datetime64(tgt)))
    if p <= pend:
        x = idx[np.searchsorted(idx, p)]; reason = "horizon"
    else:
        x = pend; reason = "terminal"
    xp = v.loc[C[x], "close"]; g6 = xp / eo - 1; n6 = (1 - COST) ** 2 * (1 + g6) - 1
    s6 = S.close.iloc[x] / S.open.iloc[e] - 1
    sales = raw[raw.include_v1.astype(bool) & raw.type.isin(["sell", "partial_sale"]) & ~raw.short_related.astype(bool)
                & (raw.mk == r.mk) & (raw.ticker == r.ticker) & (raw.transaction_date >= r.transaction_date)]
    if len(sales):
        sf = sales.filing_date.min(); sd = max(int(np.searchsorted(C.values, np.datetime64(sf), side="right")), e)
        if sd <= pend:
            mx = idx[np.searchsorted(idx, sd)]; mpx = v.loc[C[mx], "open"]; mk = "open"
            mr = "member_sale_at_entry" if mx == e else "member_sale"
        else:
            mx, mpx, mk, mr = pend, v.loc[C[pend], "close"], "close", "terminal"
    else:
        sf = ""; mx, mpx, mk, mr = pend, v.loc[C[pend], "close"], "close", "terminal"
    mn = (1 - COST) ** 2 * mpx / eo - 1
    ms = (S.open.iloc[mx] if mk == "open" else S.close.iloc[mx]) / S.open.iloc[e] - 1
    exp = dict(entry=str(ed.date()), eo=eo, refd=str(refd.date()), ref=refpx, rise=rise, taken_X10=rise <= 10 + 1e-7,
               h6d=str(C[x].date()), h6r=reason, h6g=g6, h6n=n6, h6s=s6, sf=sf, msd=str(C[mx].date()), mpx=mpx, mr=mr, mn=mn, ms=ms)
    fil = dict(entry=f.entry_date, eo=f.entry_open, refd=f.ref_date, ref=f.ref_price, rise=f.rise_pct,
               taken_X10=f.rise_pct <= 10 + 1e-7, h6d=f.hold_6m_exit_date, h6r=f.hold_6m_exit_reason, h6g=f.hold_6m_gross_ret,
               h6n=f.hold_6m_net_ret, h6s=f.hold_6m_spy_ret, sf=f.first_sale_filing_date if isinstance(f.first_sale_filing_date, str) else "",
               msd=f.member_sells_exit_date, mpx=f.member_sells_exit_price, mr=f.member_sells_exit_reason,
               mn=f.member_sells_net_ret, ms=f.member_sells_spy_ret)
    mism = []
    for kk in exp:
        a, b = exp[kk], fil[kk]
        if kk in ("h6r", "mr"):
            if not (a == b or (a == "terminal" and b in ("delisted", "open_at_end"))): mism.append(kk)
        elif isinstance(a, (float, np.floating)) and not isinstance(a, bool):
            if not np.isclose(a, b, rtol=1e-9, atol=1e-12): mism.append(kk)
        elif str(a) != str(b): mism.append(kk)
    out.append(dict(row_id=rid, member=r.member, ticker=r.ticker, txn=r.transaction_date, filing=r.filing_date,
                    **{"e_" + a: b for a, b in exp.items()}, **{"f_" + a: b for a, b in fil.items()}, mism=";".join(mism) or "none"))
o = pd.DataFrame(out); o.to_csv("review/_r2_hand_check.csv", index=False)
print("seed", SEED, "picked", pick)
for r in o.itertuples():
    print(f"| {r.row_id} | {r.member} / {r.ticker} | {r.txn} / {r.filing} | {r.e_entry} @ {r.e_eo:g} | {r.e_refd} @ {r.e_ref:g} | {r.e_rise:+.2f} ({'take' if r.e_taken_X10 else 'skip'}) | {r.e_h6d} ({r.f_h6r}) | {r.e_h6g:+.4f} / {r.e_h6n:+.4f} | {r.e_h6s:+.4f} | {r.e_sf if isinstance(r.e_sf,str) and r.e_sf else 'none'} -> {r.e_msd} {r.f_mr} @ {r.e_mpx:g} | {r.e_mn:+.4f} / {r.e_ms:+.4f} | {'yes' if r.mism=='none' else 'NO: '+r.mism} |")
