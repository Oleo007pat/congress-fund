"""Recompute 10 random trades from the raw CSVs (independent of src/backtest).
Seed 20261117: 8 random priced signals + 1 random hold_12m 'delisted' exit +
1 random member_sells 'member_sale' exit."""
import numpy as np
import pandas as pd

from review.common import COST, ROOT, cal, load_bt, load_trades, px, spy

pd.set_option("display.width", 250)
SEED = 20261117
rng = np.random.default_rng(SEED)
bt = load_bt()
priced = bt[bt.entry_idx.notna()]
pick = list(rng.choice(priced.row_id.to_numpy(), 8, replace=False))
pick.append(int(rng.choice(priced.loc[priced.hold_12m_exit_reason == "delisted", "row_id"].to_numpy())))
pick.append(int(rng.choice(priced.loc[priced.member_sells_exit_reason == "member_sale", "row_id"].to_numpy())))

raw = load_trades()
tmap = pd.read_csv(ROOT / "data/prices/ticker_map.csv", keep_default_na=False).set_index("filing_ticker")
C = cal()
S = spy()


def valid(sym):
    d = px(sym)
    return d[(d.volume > 0) & (d.open > 0) & (d.close > 0)]


def seg_end_after(v, start_date):
    """Last valid bar of the run that contains start_date (run broken by >10 SPY days w/o valid bar)."""
    idx = [C.get_loc(x) for x in v.index]
    k = idx.index(C.get_loc(start_date))
    j = k
    while j + 1 < len(idx) and idx[j + 1] - idx[j] - 1 <= 10:
        j += 1
    return v.index[j]


out = []
for rid in pick:
    r = raw.loc[rid]
    f = bt.set_index("row_id").loc[rid]
    sym = tmap.loc[r.ticker, "price_symbol"]
    v = valid(sym)
    ed = C[C > pd.Timestamp(r.filing_date)][0]
    eo = v.loc[ed, "open"]
    t_day = C[C <= pd.Timestamp(r.transaction_date)][-1] if (C <= pd.Timestamp(r.transaction_date)).any() else None
    ref = v[v.index <= pd.Timestamp(r.transaction_date)]
    refpx = ref.close.iloc[-1] if len(ref) and t_day is not None else np.nan
    refd = ref.index[-1].date() if len(ref) and t_day is not None else None
    rise = 100 * (eo / refpx - 1)
    send = seg_end_after(v, ed)
    # hold_12m
    tgt = ed + pd.DateOffset(months=12)
    p = C[C >= tgt]
    if len(p) and p[0] <= send:
        xd = v.index[v.index >= p[0]][0]; reason = "horizon"
    else:
        xd = send; reason = "delisted/open_at_end"
    xp = v.loc[xd, "close"]
    g = xp / eo - 1; n = (1 - COST) ** 2 * (1 + g) - 1
    sret = S.loc[xd, "close"] / S.loc[ed, "open"] - 1
    # member_sells
    sales = raw[raw.include_v1 & raw.type.isin(["sell", "partial_sale"]) & ~raw.short_related &
                (raw.member == r.member) & (raw.ticker == r.ticker) & (raw.transaction_date >= r.transaction_date)]
    if len(sales):
        sf = sales.filing_date.min()
        sd = max(C[C > pd.Timestamp(sf)][0], ed)
        if sd <= send:
            mxd = v.index[v.index >= sd][0]; mxp = v.loc[mxd, "open"]; mk = "open"
            mreason = "member_sale_at_entry" if mxd == ed else "member_sale"
        else:
            mxd, mxp, mk, mreason = send, v.loc[send, "close"], "close", "terminal"
    else:
        sf = ""; mxd, mxp, mk, mreason = send, v.loc[send, "close"], "close", "terminal(no sale)"
    mg = mxp / eo - 1; mn = (1 - COST) ** 2 * (1 + mg) - 1
    ms = (S.loc[mxd, "open"] if mk == "open" else S.loc[mxd, "close"]) / S.loc[ed, "open"] - 1
    exp = dict(entry_date=str(ed.date()), entry_open=eo, ref_date=str(refd), ref_price=refpx, rise_pct=rise,
               h12_exit_date=str(xd.date()), h12_reason=reason, h12_gross=g, h12_net=n, h12_spy=sret,
               ms_sale_filing=sf, ms_exit_date=str(mxd.date()), ms_exit_px=mxp, ms_reason=mreason, ms_net=mn, ms_spy=ms)
    fil = dict(entry_date=f.entry_date, entry_open=f.entry_open, ref_date=str(f.ref_date), ref_price=f.ref_price,
               rise_pct=f.rise_pct, h12_exit_date=f.hold_12m_exit_date, h12_reason=f.hold_12m_exit_reason,
               h12_gross=f.hold_12m_gross_ret, h12_net=f.hold_12m_net_ret, h12_spy=f.hold_12m_spy_ret,
               ms_sale_filing=f.first_sale_filing_date if isinstance(f.first_sale_filing_date, str) else "",
               ms_exit_date=f.member_sells_exit_date, ms_exit_px=f.member_sells_exit_price, ms_reason=f.member_sells_exit_reason,
               ms_net=f.member_sells_net_ret, ms_spy=f.member_sells_spy_ret)
    mism = []
    for k in exp:
        a, b = exp[k], fil[k]
        if isinstance(a, float) or isinstance(b, float):
            if not (np.isclose(a, b, rtol=1e-9, atol=1e-9) or (np.isnan(a) and np.isnan(b))):
                mism.append(k)
        elif k.endswith("reason"):
            ok = (a == b) or (a.startswith("delisted/open") and b in ("delisted", "open_at_end")) or (a.startswith("terminal") and b in ("delisted", "open_at_end"))
            if not ok:
                mism.append(k)
        elif str(a) != str(b):
            mism.append(k)
    out.append(dict(row_id=rid, member=r.member, ticker=r.ticker, sym=sym, txn=r.transaction_date, filing=r.filing_date,
                    **{f"exp_{k}": v_ for k, v_ in exp.items()}, **{f"file_{k}": v_ for k, v_ in fil.items()},
                    mismatches=";".join(mism) or "none"))
o = pd.DataFrame(out)
o.to_csv("review/_hand_check.csv", index=False)
print("picked row_ids:", pick)
for r in out:
    print(f"\n# row {r['row_id']} {r['member']} {r['ticker']}->{r['sym']} txn {r['txn']} filed {r['filing']}  MISMATCH: {r['mismatches']}")
    for k in ("entry_date", "entry_open", "ref_date", "ref_price", "rise_pct", "h12_exit_date", "h12_reason", "h12_gross",
              "h12_net", "h12_spy", "ms_sale_filing", "ms_exit_date", "ms_exit_px", "ms_reason", "ms_net", "ms_spy"):
        print(f"   {k:15s} exp={r['exp_' + k]!s:28s} file={r['file_' + k]!s}")
