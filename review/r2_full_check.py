"""Round 2: independent re-derivation (no src/ imports) of entry, reference, rise,
pieces (new F6 rule), hold_6m exits and returns for all eligible rows; then the
continuous book for hold_6m / amount / X=10, its metrics and a Newey-West alpha."""
import numpy as np
import pandas as pd

from review.common import COST, ROOT, cal, load_bt, metrics, px, spy

pd.set_option("display.width", 250)
bt = load_bt()
C = cal(); S = spy(); n = len(C)
pos = {d: i for i, d in enumerate(C)}
el = bt[bt.drop_reason.isna()].copy()
print("eligible rows:", len(el))

pieces = {}


def piece_info(sym):
    """valid-bar index array, piece end (cal index) for each valid bar, close series on cal."""
    if sym in pieces:
        return pieces[sym]
    d = px(sym)
    v = d[(d.volume > 0) & (d.open > 0) & (d.close > 0)]
    idx = np.array([pos[t] for t in v.index])
    o = v.open.to_numpy(); c = v.close.to_numpy()
    gaps = np.diff(idx) - 1
    jump = o[1:] / c[:-1]
    brk = (gaps > 10) & ((jump > 2) | (jump < 0.5) | (gaps > 63))
    end_of = np.empty(len(idx), int)
    pid = np.zeros(len(idx), int)
    cur = 0
    for k in range(1, len(idx)):
        if brk[k - 1]:
            cur += 1
        pid[k] = cur
    for p_ in np.unique(pid):
        m = pid == p_
        end_of[m] = idx[m][-1]
    closes = np.full(n, np.nan); closes[idx] = c
    opens = np.full(n, np.nan); opens[idx] = o
    pieces[sym] = (idx, end_of, pid, closes, opens)
    return pieces[sym]


bad = {k: 0 for k in ("entry_date", "entry_open", "ref", "rise", "h6_exit_date", "h6_exit_px", "h6_net", "h6_spy", "h6_reason")}
rows = []
for r in el.itertuples(index=False):
    idx, end_of, pid, closes, opens = piece_info(r.price_symbol)
    e = int(np.searchsorted(C.values, np.datetime64(r.filing_date), side="right"))
    if str(C[e].date()) != r.entry_date: bad["entry_date"] += 1
    if not np.isclose(opens[e], r.entry_open): bad["entry_open"] += 1
    t = int(np.searchsorted(C.values, np.datetime64(r.transaction_date), side="right")) - 1
    rv = idx[idx <= t]
    refpx = closes[rv[-1]]
    if not np.isclose(refpx, r.ref_price): bad["ref"] += 1
    if not np.isclose(100 * (r.entry_open / refpx - 1), r.rise_pct): bad["rise"] += 1
    k = int(np.searchsorted(idx, e))
    pend = end_of[k]
    tgt = C[e] + pd.DateOffset(months=6)
    p = int(np.searchsorted(C.values, np.datetime64(tgt), side="left"))
    if p <= pend:
        x = idx[np.searchsorted(idx, p)]; reason = "horizon"
    else:
        x = pend; reason = "terminal"
    xp = closes[x]
    if str(C[x].date()) != r.hold_6m_exit_date: bad["h6_exit_date"] += 1
    if not np.isclose(xp, r.hold_6m_exit_price): bad["h6_exit_px"] += 1
    net = (1 - COST) ** 2 * xp / r.entry_open - 1
    if not np.isclose(net, r.hold_6m_net_ret): bad["h6_net"] += 1
    sp = S.close.iloc[x] / S.open.iloc[e] - 1
    if not np.isclose(sp, r.hold_6m_spy_ret): bad["h6_spy"] += 1
    if (reason == "horizon") != (r.hold_6m_exit_reason == "horizon"): bad["h6_reason"] += 1
    rows.append((r.row_id, r.price_symbol, e, x, r.entry_open, xp, r.weight_amount, r.rise_pct, r.sample))
print("mismatches:", bad)
T = pd.DataFrame(rows, columns=["row_id", "sym", "e", "x", "eo", "xp", "w", "rise", "sample"])

# continuous book hold_6m / amount / X = 10 (take iff rise <= 10 %)
tk = T[T.rise <= 10 + 1e-7]
print("trades taken (X=10):", len(tk))
start = np.zeros(n); end = np.zeros(n)
for r in tk.itertuples(index=False):
    closes = piece_info(r.sym)[3]
    h = r.w * (1 - COST) / r.eo
    last = np.nan; prev = np.nan
    for t_ in range(r.e, r.x + 1):
        if not np.isnan(closes[t_]): last = closes[t_]
        start[t_] += r.w if t_ == r.e else h * prev
        end[t_] += h * r.xp * (1 - COST) if t_ == r.x else h * last
        prev = last
first = int(np.flatnonzero(start)[0]); lastd = int(np.flatnonzero(start)[-1])
rr = np.where(start > 0, end / np.where(start > 0, start, 1) - 1, 0.0)
br = np.r_[np.nan, S.close.to_numpy()[1:] / S.close.to_numpy()[:-1] - 1]
br[first] = S.close.iloc[first] / S.open.iloc[first] - 1
w0 = int(np.searchsorted(C.values, np.datetime64("2022-01-01")))
dates = C[w0:lastd + 1]; r_ = rr[w0:lastd + 1]; b_ = br[w0:lastd + 1]
m, mb = metrics(dates, r_), metrics(dates, b_)
print("PRIMARY continuous (mine):", {k: round(v, 6) for k, v in m.items()})
print("SPY same days   (mine):", {k: round(v, 6) for k, v in mb.items()})


def nw(y, X, L=5):
    b = np.linalg.lstsq(X, y, rcond=None)[0]
    u = y - X @ b
    Q = np.linalg.inv(X.T @ X)
    S_ = sum(np.outer(X[i] * u[i], X[i] * u[i]) for i in range(len(y)))
    for l in range(1, L + 1):
        G = sum(np.outer(X[i] * u[i], X[i - l] * u[i - l]) for i in range(l, len(y)))
        S_ += (1 - l / (L + 1)) * (G + G.T)
    V = Q @ S_ @ Q
    return b, np.sqrt(np.diag(V))


X = np.c_[np.ones(len(b_)), b_]
b, se = nw(r_, X, 5)
b0, se0 = nw(r_, X, 0)
print(f"alpha {b[0]*252:.6f}/yr  t_NW5 {b[0]/se[0]:.4f}  (t_white {b0[0]/se0[0]:.4f}); beta {b[1]:.4f} t(beta-1) {(b[1]-1)/se[1]:.3f}")
for L in (10, 20):
    bl, sl = nw(r_, X, L); print(f"  NW lags {L}: alpha t {bl[0]/sl[0]:.4f}")
ex = r_ - b_
be, see = nw(ex, np.ones((len(ex), 1)), 5)
print(f"mean excess {be[0]*252:.6f}/yr t_NW {be[0]/see[0]:.4f}; TE {ex.std(ddof=1)*np.sqrt(252):.6f}")
# yearly table of the continuous book (all days, from first)
yy = pd.DataFrame({"y": C[first:lastd + 1].year, "r": rr[first:lastd + 1], "b": br[first:lastd + 1]}).groupby("y").agg(
    strat=("r", lambda s: np.prod(1 + s) - 1), spy=("b", lambda s: np.prod(1 + s) - 1))
print(yy.round(6).to_string())
np.save("review/_r2_primary.npy", np.c_[dates.values.astype("datetime64[D]").astype(int), r_, b_])
