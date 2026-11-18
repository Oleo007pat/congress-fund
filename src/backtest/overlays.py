"""Risk-overlay follow-up test:   python3 -m src.backtest.overlays

Question: do simple, pre-specified risk overlays on the LOCKED v1 strategy
(hold_6m, amount weighting, X = 10 %) raise its Sharpe ratio, or lower its risk
without hurting its return much?

Pre-registration: src/backtest/overlay_config.py (written before any result; its
SHA-256 is stored in recommendation.json).  The v1 signal is reused unchanged
(run.build_signal_table + run.Evaluator.trades).  Only the portfolio
construction is overlaid:

  weighting   equal | amount (v1) | sqrt_amount        (starting notional w_i)
  cap         none | position 5 % | member 15 %       (daily weight caps, water-filling)
  vol target  off | on (15 %, 63 days, exposure <= 1, cash at 0 %)

Book (same as portfolio.py, rewritten per position so weights can be capped):
  S_i,t = w_i on the entry day, h_i * C_i,t-1 later;  E_i,t = h_i * C_i,t, or
  h_i * P_exit * (1 - c) on the exit day;  rho_i,t = E_i,t / S_i,t - 1.
  natural weight omega_i,t = S_i,t / sum_j S_j,t  (v1: r_t = sum omega * rho).
  overlay: r_t = sum omega'_i,t rho_i,t - cap cost_t, then vol targeting on top.

Writes results/overlays/: grid_summary.csv, recommendation.json, oos_continuous.csv,
yearly.csv, significance.csv, concentration.csv, equity_curves.csv
(ASSUMPTIONS.md in that folder is maintained by hand.)
"""
from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

from . import config
from . import overlay_config as oc
from .analysis import significance, yearly_table
from .engine import add_canonical_member, load_trades
from .member_selection import deflated_sr, json_clean, probabilistic_sr
from .portfolio import bench_daily_returns, series_metrics
from .prices import load_pricebook
from .run import PRIMARY, SEL, Evaluator, build_signal_table

CONT = "all_continuous"
V1_KEY = ("amount", "none", "off")


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def combo_key(weighting, cap, vt) -> str:
    return f"weighting={weighting};cap={cap};vol_target={vt}"


def grid_order():
    return [(w, c, v) for w in oc.WEIGHTINGS for c in oc.CAPS for v in oc.VOL_TARGETS]


# ----------------------------------------------------------------------------
# 1. Weighting
# ----------------------------------------------------------------------------
def notional(amount_mid: np.ndarray, weighting: str) -> np.ndarray:
    """Starting notional w_i.  amount_mid = engine.amount_weight (range midpoint)."""
    a = np.asarray(amount_mid, float)
    if weighting == "equal":
        return np.ones_like(a)
    if weighting == "amount":
        return a.copy()
    if weighting == "sqrt_amount":
        if (a < 0).any():
            raise ValueError("negative amount")
        return np.sqrt(a)
    raise ValueError(weighting)


# ----------------------------------------------------------------------------
# 2. Caps
# ----------------------------------------------------------------------------
def water_fill(w: np.ndarray, cap: float, eps: float = 1e-12) -> np.ndarray:
    """Cap every weight at `cap`; redistribute the excess to the uncapped weights in
    proportion to their weights; repeat until no weight exceeds the cap.  If every
    weight is capped (fewer than 1/cap names), the rest is not placed (cash).
    Input weights >= 0; the output sum is <= the input sum (== if 1/cap names fit)."""
    w = np.asarray(w, float).copy()
    if (w < -eps).any():
        raise ValueError("negative weight")
    capped = np.zeros(len(w), bool)
    for _ in range(len(w) + 1):
        over = w > cap + eps
        if not over.any():
            break
        excess = float((w[over] - cap).sum())
        w[over] = cap
        capped |= over
        free = ~capped & (w > 0)
        tot = float(w[free].sum())
        if tot <= 0:
            break  # nothing left to receive the excess -> cash
        w[free] += excess * w[free] / tot
    return np.minimum(w, cap) if capped.any() else w


def member_cap_weights(w: np.ndarray, member: np.ndarray, cap: float) -> np.ndarray:
    """Cap each member's summed weight at `cap` (water-filling across members);
    positions inside a member are scaled pro rata."""
    w = np.asarray(w, float)
    u, inv = np.unique(np.asarray(member), return_inverse=True)
    W = np.bincount(inv, weights=w, minlength=len(u))
    Wc = water_fill(W, cap)
    with np.errstate(invalid="ignore", divide="ignore"):
        f = np.where(W > 0, Wc / np.where(W > 0, W, 1.0), 0.0)
    return w * f[inv]


def apply_cap(wn: np.ndarray, member: np.ndarray, cap: str) -> np.ndarray:
    if cap == "none":
        return wn
    if cap == "position":
        return water_fill(wn, oc.POSITION_CAP)
    if cap == "member":
        return member_cap_weights(wn, member, oc.MEMBER_CAP)
    raise ValueError(cap)


# ----------------------------------------------------------------------------
# Position panel (one element per position-day, trade-major)
# ----------------------------------------------------------------------------
def position_panel(Cz, col, e, x, entry_px, exit_px, w, cost=config.COST) -> dict:
    """Per position-day start value S and end value E exactly as portfolio.py."""
    col, e, x = (np.asarray(a, int) for a in (col, e, x))
    entry_px, exit_px, w = (np.asarray(a, float) for a in (entry_px, exit_px, w))
    assert np.all(x >= e)
    h = w * (1.0 - cost) / entry_px
    L = x - e + 1
    tid = np.repeat(np.arange(len(e)), L)
    off = np.arange(int(L.sum())) - np.repeat(np.cumsum(L) - L, L)
    day = e[tid] + off
    c = col[tid]
    S = np.where(off == 0, w[tid], h[tid] * Cz[np.maximum(day - 1, 0), c])
    E = np.where(day == x[tid], h[tid] * exit_px[tid] * (1.0 - cost), h[tid] * Cz[day, c])
    if not (S > 0).all():
        raise ValueError("non-positive start value inside a holding window")
    return {"tid": tid, "off": off, "day": day, "S": S, "E": E, "first": int(e.min()), "last": int(x.max())}


def run_book(panel: dict, member_code: np.ndarray, cap: str, cost: float = config.COST,
             with_conc: bool = True, keep_weights: bool = False) -> dict:
    """Daily net returns of the (capped) calendar-time book, before vol targeting.

    member_code: integer member code per TRADE.  Returns r (net of cap cost),
    r_gross, cap_cost, and daily concentration statistics."""
    first, last = panel["first"], panel["last"]
    n = last - first + 1
    day = panel["day"] - first
    S, E, tid, off = panel["S"], panel["E"], panel["tid"], panel["off"]
    mem = np.asarray(member_code, int)[tid]
    rho = E / S - 1.0
    ss = np.bincount(day, weights=S, minlength=n)
    es = np.bincount(day, weights=E, minlength=n)
    wnat = S / ss[day]
    order = np.argsort(day, kind="stable")
    bounds = np.searchsorted(day[order], np.arange(n + 1))
    if cap == "none":
        wcap = wnat
        m = ss > 1e-12 * max(1.0, float(np.abs(ss).max()))
        r_gross = np.zeros(n)
        r_gross[m] = es[m] / ss[m] - 1.0          # identical to portfolio.portfolio_daily_returns
    else:
        wcap = np.empty_like(wnat)
        for k in range(n):
            idx = order[bounds[k]:bounds[k + 1]]
            if len(idx):
                wcap[idx] = apply_cap(wnat[idx], mem[idx], cap)
        r_gross = np.bincount(day, weights=wcap * rho, minlength=n)
    cap_cost = np.zeros(n)
    if cap != "none":
        cont = np.flatnonzero(off > 0)            # positions held on t-1 and t
        prev = cont - 1                            # same trade, previous day (trade-major layout)
        pi = wcap[prev] * (1.0 + rho[prev]) / (1.0 + r_gross[day[prev]])
        d = wcap[cont] - pi
        gross = np.bincount(day[cont], weights=np.abs(d), minlength=n)
        net = np.bincount(day[cont], weights=d, minlength=n)
        cap_cost = cost * np.maximum(0.0, gross - np.abs(net))
    out = {"first": first, "last": last, "r": r_gross - cap_cost, "r_gross": r_gross, "cap_cost": cap_cost,
           "invested": np.bincount(day, weights=wcap, minlength=n)}
    if keep_weights:
        out.update(w_cap=wcap, w_nat=wnat, rho=rho, day=day, member=mem)
    if with_conc:
        top_m = np.zeros(n)
        top_m_code = np.full(n, -1)
        top_p = np.zeros(n)
        npos = np.diff(bounds)
        for k in range(n):
            idx = order[bounds[k]:bounds[k + 1]]
            if len(idx):
                u, inv = np.unique(mem[idx], return_inverse=True)
                W = np.bincount(inv, weights=wcap[idx])
                j = int(np.argmax(W))
                top_m[k], top_m_code[k] = W[j], u[j]
                top_p[k] = float(wcap[idx].max())
        out.update(top_member_share=top_m, top_member_code=top_m_code, top_position_share=top_p, n_positions=npos)
    return out


# ----------------------------------------------------------------------------
# 3. Vol targeting
# ----------------------------------------------------------------------------
def vol_target(r_book: np.ndarray, target: float = oc.VT_TARGET, window: int = oc.VT_WINDOW,
               cost: float = oc.VT_COST, ann: int = oc.VT_ANNUALIZATION) -> dict:
    """exposure_t = min(1, target / sigma_t), sigma_t from r_book[t-window .. t-1] only
    (exposure 1 during warm-up or if sigma = 0).  r_t = e_t r_book_t - cost |e_t - e~_t-1|,
    e~_t-1 = e_t-1 (1 + r_book_t-1) / (1 + e_t-1 r_book_t-1) (drifted), e~ before day 0 = 1."""
    r = np.asarray(r_book, float)
    n = len(r)
    sig = pd.Series(r).rolling(window, min_periods=window).std(ddof=1).shift(1).to_numpy() * np.sqrt(ann)
    with np.errstate(divide="ignore", invalid="ignore"):
        e = np.where(np.isfinite(sig) & (sig > 0), np.minimum(1.0, target / sig), 1.0)
    drift = np.ones(n)
    if n > 1:
        drift[1:] = e[:-1] * (1.0 + r[:-1]) / (1.0 + e[:-1] * r[:-1])
    trade = np.abs(e - drift)
    tc = cost * trade
    return {"r": e * r - tc, "exposure": e, "sigma": sig, "turnover": trade, "cost": tc}


def apply_vt(r_book: np.ndarray, vt: str) -> dict:
    if vt == "off":
        n = len(r_book)
        return {"r": np.asarray(r_book, float).copy(), "exposure": np.ones(n), "cost": np.zeros(n),
                "turnover": np.zeros(n), "sigma": np.full(n, np.nan)}
    if vt == "on":
        return vol_target(r_book)
    raise ValueError(vt)


# ----------------------------------------------------------------------------
# Selection (pre-registered)
# ----------------------------------------------------------------------------
def _order_col(g):
    ow = {k: i for i, k in enumerate(oc.WEIGHTINGS)}
    occ = {k: i for i, k in enumerate(oc.CAPS)}
    ov = {k: i for i, k in enumerate(oc.VOL_TARGETS)}
    return g["weighting"].map(ow) * 100 + g["cap"].map(occ) * 10 + g["vol_target"].map(ov)


def select_overlay(grid_sel: pd.DataFrame) -> pd.Series:
    """Primary pick: highest in-sample Sharpe.  Receives ONLY selection-sample rows."""
    samples = set(grid_sel["sample"].unique())
    if samples != {oc.SELECTION_SAMPLE}:
        raise ValueError(f"selection must see only {oc.SELECTION_SAMPLE}, got {samples}")
    g = grid_sel[grid_sel["n_trades"] >= oc.SELECT_MIN_TRADES].copy()
    g = g[np.isfinite(g[oc.SELECTION_METRIC].astype(float))]
    if g.empty:
        raise ValueError("no eligible combination")
    g["_o"] = _order_col(g)
    best = g[oc.SELECTION_METRIC].max()
    return g[g[oc.SELECTION_METRIC] >= best - 1e-12].sort_values("_o").iloc[0].drop(labels="_o")


def select_risk_reduction(grid_sel: pd.DataFrame, cagr_v1: float, two_sided: bool = True) -> pd.Series:
    """Secondary pick: lowest in-sample volatility among |CAGR - CAGR_v1| <= tol
    (two_sided, pre-registered) or CAGR >= CAGR_v1 - tol (one-sided check)."""
    samples = set(grid_sel["sample"].unique())
    if samples != {oc.SELECTION_SAMPLE}:
        raise ValueError(f"selection must see only {oc.SELECTION_SAMPLE}, got {samples}")
    g = grid_sel[grid_sel["n_trades"] >= oc.SELECT_MIN_TRADES].copy()
    d = g["cagr"].astype(float) - cagr_v1
    ok = (d.abs() <= oc.RISK_PICK_CAGR_TOL + 1e-12) if two_sided else (d >= -oc.RISK_PICK_CAGR_TOL - 1e-12)
    g = g[ok & np.isfinite(g["volatility"].astype(float))]
    if g.empty:
        raise ValueError("no candidate for the risk-reduction pick")
    g["_o"] = _order_col(g)
    lo = g["volatility"].min()
    return g[g["volatility"] <= lo + 1e-12].sort_values("_o").iloc[0].drop(labels="_o")


def memmel_z(r1, r2):
    """Sharpe-difference z (analysis.significance), NaN if the two series are identical."""
    r1, r2 = np.asarray(r1, float), np.asarray(r2, float)
    if np.allclose(r1, r2, rtol=0, atol=1e-15):
        return np.nan
    return significance(r1, r2)["sharpe_diff_memmel_z"]


# ----------------------------------------------------------------------------
def main():
    t0 = time.time()
    OUT = oc.OUT_DIR
    OUT.mkdir(parents=True, exist_ok=True)
    cfg_sha = hashlib.sha256(Path(oc.__file__).read_bytes()).hexdigest()

    log("loading trades, prices, building v1 signal table (engine unchanged)")
    trades = add_canonical_member(load_trades())
    tmap = pd.read_csv(config.TICKER_MAP_CSV, keep_default_na=False, na_values=[])
    syms = set(tmap.loc[tmap["price_symbol"] != "", "price_symbol"])
    pb = load_pricebook(syms)
    sig = build_signal_table(trades, tmap, pb)
    ev = Evaluator(sig, pb)
    codes = {m: i for i, m in enumerate(sorted(sig["member_canon"].dropna().unique()))}
    inv_codes = {v: k for k, v in codes.items()}
    oos0 = ev.oos_start
    cut_idx = pb.last_day_on_or_before(config.IS_END)

    # ---- books: 3 weightings x 3 caps on two samples (vol targeting is applied on top)
    samples = {SEL: SEL, CONT: CONT}
    tks, panels, books = {}, {}, {}
    for s in samples:
        _, _, _, tk, prule = ev.trades(s, oc.V1_EXIT_RULE, oc.V1_X)
        tks[s] = (tk, prule)
        col = tk["col"].astype(int).map(ev.compact).to_numpy()
        e = tk["entry_idx"].astype(int).to_numpy()
        xi = tk[f"{prule}_exit_idx"].astype(int).to_numpy()
        mcode = tk["member_canon"].map(codes).to_numpy(int)
        for wt in oc.WEIGHTINGS:
            w = notional(tk["weight_amount"].to_numpy(float), wt)
            panels[(s, wt)] = position_panel(ev.Cz, col, e, xi, tk["entry_open"].to_numpy(float),
                                             tk[f"{prule}_exit_price"].to_numpy(float), w)
            for cap in oc.CAPS:
                log(f"book {s} weighting={wt} cap={cap}")
                books[(s, wt, cap)] = run_book(panels[(s, wt)], mcode, cap)
    first_sel, last_sel = books[(SEL, "amount", "none")]["first"], books[(SEL, "amount", "none")]["last"]
    first_c, last_c = books[(CONT, "amount", "none")]["first"], books[(CONT, "amount", "none")]["last"]
    assert last_sel == cut_idx, "selection book must end at the 2021 close"
    dates_sel = pb.cal[first_sel:last_sel + 1]
    dates_c = pb.cal[first_c:last_c + 1]
    spy_sel = bench_daily_returns(ev.b_open, ev.b_close, first_sel, last_sel)
    spy_c = bench_daily_returns(ev.b_open, ev.b_close, first_c, last_c)
    k0 = oos0 - first_c                                   # first OOS day inside the continuous series
    k23 = pb.first_day_on_or_after("2023-01-01") - first_c

    # ---- all 18 combinations: series ------------------------------------------------
    series = {}
    for (wt, cap, vt) in grid_order():
        for s in samples:
            b = books[(s, wt, cap)]
            v = apply_vt(b["r"], vt)
            series[(s, wt, cap, vt)] = {"r": v["r"], "exposure": v["exposure"], "vt_cost": v["cost"],
                                        "cap_cost": b["cap_cost"], "invested": b["invested"],
                                        "vt_turnover": v["turnover"]}
    # SPY + vol targeting baseline (same rule, same days)
    spy_vt_sel = vol_target(spy_sel)
    spy_vt_c = vol_target(spy_c)

    def cut(s, arr, start_k=None):
        if s == SEL:
            return arr
        return arr[(k0 if start_k is None else start_k):]

    def sel_series(key):
        return series[(SEL,) + key]["r"]

    def oos_series(key, start_k=None):
        return cut(CONT, series[(CONT,) + key]["r"], start_k)

    # ---- self-check: amount / none / off reproduces v1 exactly ----------------------
    v1grid = pd.read_csv(config.RESULTS / "grid_summary.csv")
    v1row = lambda smp: v1grid[(v1grid["sample"] == smp) & (v1grid["exit_rule"] == oc.V1_EXIT_RULE)
                               & (v1grid["weighting"] == oc.V1_WEIGHTING) & (v1grid["X"] == "10")].iloc[0]
    m_sel = series_metrics(dates_sel, sel_series(V1_KEY))
    m_oos = series_metrics(dates_c[k0:], oos_series(V1_KEY))
    check = {}
    for nm, m, smp in (("selection", m_sel, SEL), ("primary_oos", m_oos, PRIMARY)):
        ref = v1row(smp)
        for k in ("total_return", "cagr", "volatility", "sharpe", "max_drawdown"):
            diff = abs(float(m[k]) - float(ref[k]))
            check[f"{nm}_{k}_abs_diff"] = diff
            if diff > 1e-9:
                raise AssertionError(f"amount/none/off does not reproduce v1 {nm} {k}: {m[k]} vs {ref[k]}")
        if int(ref["n_trades"]) != len(tks[CONT if smp == PRIMARY else SEL][0]):
            raise AssertionError("trade count differs from v1")
    log("self-check passed: amount / none / off reproduces v1 (selection and primary OOS) to 1e-9")

    # ---- grid summary -----------------------------------------------------------------
    r_v1_sel, r_v1_oos = sel_series(V1_KEY), oos_series(V1_KEY)
    spy_oos = spy_c[k0:]
    rows = []
    n_years_sel = len(dates_sel) / config.ANNUALIZATION
    n_years_oos = len(dates_c[k0:]) / config.ANNUALIZATION
    for (wt, cap, vt) in grid_order():
        key = (wt, cap, vt)
        for smp, dts, r, br, rv1, src, nyr in (
                (SEL, dates_sel, sel_series(key), spy_sel, r_v1_sel, series[(SEL,) + key], n_years_sel),
                (PRIMARY, dates_c[k0:], oos_series(key), spy_oos, r_v1_oos,
                 {k_: cut(CONT, v_) for k_, v_ in series[(CONT,) + key].items()}, n_years_oos)):
            sg = significance(r, br)
            row = {"sample": smp, "config": combo_key(*key), "weighting": wt, "cap": cap, "vol_target": vt,
                   "is_v1": key == V1_KEY,
                   "n_trades": len(tks[SEL if smp == SEL else CONT][0]),
                   **series_metrics(dts, r), "period_start": str(dts[0]), "period_end": str(dts[-1]),
                   "alpha_annual": sg["alpha_annual"], "alpha_t_nw": sg["alpha_t_nw"], "beta": sg["beta"],
                   "information_ratio": sg["information_ratio"], "tracking_error": sg["tracking_error"],
                   "sharpe_diff_z_vs_spy": sg["sharpe_diff_memmel_z"],
                   "sharpe_diff_z_vs_v1": memmel_z(r, rv1),
                   "sharpe_minus_v1": series_metrics(dts, r)["sharpe"] - series_metrics(dts, rv1)["sharpe"],
                   "mean_exposure": float(src["exposure"].mean()), "min_exposure": float(src["exposure"].min()),
                   "mean_invested_share_of_book": float(src["invested"].mean()),
                   "cap_cost_per_year": float(src["cap_cost"].sum() / nyr),
                   "vt_cost_per_year": float(src["vt_cost"].sum() / nyr),
                   "vt_turnover_per_year": float(src["vt_turnover"].sum() / nyr),
                   **series_metrics(dts, br, prefix="spy_")}
            rows.append(row)
    grid = pd.DataFrame(rows)
    # Start-up check (robustness only, never used for the picks): during the first days of 2016
    # too few positions/members are open for a cap to place all weight, so capped books hold cash.
    # Re-measure in-sample Sharpe from the first day after the last such cash day.
    cash_days = np.flatnonzero(np.any([books[(SEL, wt, cap)]["invested"] < 1 - 1e-9
                                       for wt in oc.WEIGHTINGS for cap in ("position", "member")], axis=0)
                               & (books[(SEL, "equal", "none")]["invested"] > 0))
    k_full = int(cash_days.max()) + 1 if len(cash_days) else 0
    full_start = str(dates_sel[k_full])
    grid["sharpe_from_full_cap_placement"] = np.nan
    for (wt, cap, vt) in grid_order():
        m = (grid["sample"] == SEL) & (grid["config"] == combo_key(wt, cap, vt))
        grid.loc[m, "sharpe_from_full_cap_placement"] = series_metrics(dates_sel[k_full:],
                                                                       sel_series((wt, cap, vt))[k_full:])["sharpe"]
    grid.loc[grid["sample"] == SEL, "spy_sharpe_from_full_cap_placement"] = series_metrics(
        dates_sel[k_full:], spy_sel[k_full:])["sharpe"]
    grid.to_csv(OUT / "grid_summary.csv", index=False)
    assert int((grid["sample"] == SEL).sum()) == oc.N_COMBINATIONS

    # ---- pre-registered selection (selection sample ONLY) ---------------------------
    sel_rows = grid[grid["sample"] == SEL].copy()
    pick = select_overlay(sel_rows)
    cagr_v1 = float(sel_rows.loc[sel_rows["is_v1"], "cagr"].iloc[0])
    risk = select_risk_reduction(sel_rows, cagr_v1, two_sided=True)
    risk_1s = select_risk_reduction(sel_rows, cagr_v1, two_sided=False)
    pk = (pick["weighting"], pick["cap"], pick["vol_target"])
    rk = (risk["weighting"], risk["cap"], risk["vol_target"])
    rk1 = (risk_1s["weighting"], risk_1s["cap"], risk_1s["vol_target"])
    log(f"primary pick (max IS Sharpe): {combo_key(*pk)}  IS Sharpe {pick['sharpe']:.4f}")
    log(f"risk-reduction pick: {combo_key(*rk)}  IS vol {risk['volatility']:.4f} CAGR {risk['cagr']:.4f}")
    names = {"primary_pick_max_IS_sharpe": pk, "risk_reduction_pick": rk, "v1": V1_KEY}
    # descriptive only (NOT picks): each single overlay applied to v1
    extra = {"not_picked_v1+position_cap": ("amount", "position", "off"),
             "not_picked_v1+member_cap": ("amount", "member", "off"),
             "not_picked_v1+vol_target": ("amount", "none", "on")}
    shown = {**names, **extra}

    def g(sample, key):
        return grid[(grid["sample"] == sample) & (grid["config"] == combo_key(*key))].iloc[0].to_dict()

    # ---- detailed OOS table, significance, yearly ------------------------------------
    log("OOS detail, significance, yearly, concentration, equity curves")
    oos_rows, sig_rows, yr_rows = [], [], []
    measures = {"PRIMARY_continuous_book_2022-01-03_to_end": (CONT, k0),
                "continuous_excl_2022_window_2023-01-03_to_end": (CONT, k23),
                "selection_sample_in_sample_cut2021": (SEL, None)}
    keep = ("total_return", "cagr", "volatility", "sharpe", "max_drawdown")

    def add_measure(name, cfg, mname, dts, r, br, rv1):
        m = series_metrics(dts, r)
        mb = series_metrics(dts, br)
        sg = significance(r, br)
        oos_rows.append({"config_name": name, "config": cfg, "measure": mname, "period_start": str(dts[0]),
                         "period_end": str(dts[-1]), "n_days": len(r), **{k: m[k] for k in keep},
                         **{f"spy_{k}": mb[k] for k in keep},
                         "alpha_annual": sg["alpha_annual"], "alpha_t_nw": sg["alpha_t_nw"], "beta": sg["beta"],
                         "information_ratio": sg["information_ratio"], "sharpe_diff_z_vs_spy": sg["sharpe_diff_memmel_z"],
                         "sharpe_v1_same_days": series_metrics(dts, rv1)["sharpe"] if rv1 is not None else np.nan,
                         "sharpe_diff_z_vs_v1": memmel_z(r, rv1) if rv1 is not None else np.nan})
        sig_rows.append({"config_name": name, "config": cfg, "measure": mname, "benchmark": "SPY",
                         "period_start": str(dts[0]), "period_end": str(dts[-1]), **sg})
        if rv1 is not None and not np.allclose(r, rv1, rtol=0, atol=1e-15):
            sig_rows.append({"config_name": name, "config": cfg, "measure": mname, "benchmark": "v1",
                             "period_start": str(dts[0]), "period_end": str(dts[-1]), **significance(r, rv1)})

    for name, key in shown.items():
        for mname, (smp, k) in measures.items():
            if smp == SEL:
                add_measure(name, combo_key(*key), mname, dates_sel, sel_series(key), spy_sel, r_v1_sel)
            else:
                add_measure(name, combo_key(*key), mname, dates_c[k:], oos_series(key, k), spy_c[k:],
                            oos_series(V1_KEY, k))
        full = series[(CONT,) + key]["r"]
        y = yearly_table(dates_c, full, spy_c)
        y["v1_return"] = pd.DataFrame({"y": pd.DatetimeIndex(dates_c).year, "r": series[(CONT,) + V1_KEY]["r"]}) \
            .groupby("y")["r"].apply(lambda s_: float(np.prod(1 + s_) - 1)).to_numpy()
        y["minus_v1"] = y["strategy_return"] - y["v1_return"]
        y.insert(0, "config", combo_key(*key))
        y.insert(0, "config_name", name)
        yr_rows.append(y)
    for mname, (smp, k) in measures.items():
        if smp == SEL:
            add_measure("SPY_plus_vol_targeting", "SPY;vol_target=on", mname, dates_sel, spy_vt_sel["r"], spy_sel, None)
        else:
            add_measure("SPY_plus_vol_targeting", "SPY;vol_target=on", mname, dates_c[k:], spy_vt_c["r"][k:], spy_c[k:], None)
    y = yearly_table(dates_c, spy_vt_c["r"], spy_c)
    y["v1_return"] = yr_rows[0]["v1_return"].to_numpy()
    y["minus_v1"] = y["strategy_return"] - y["v1_return"]
    y.insert(0, "config", "SPY;vol_target=on")
    y.insert(0, "config_name", "SPY_plus_vol_targeting")
    yr_rows.append(y)
    oos_df = pd.DataFrame(oos_rows)
    oos_df.to_csv(OUT / "oos_continuous.csv", index=False)
    sig_df = pd.DataFrame(sig_rows)
    sig_df.to_csv(OUT / "significance.csv", index=False)
    yearly = pd.concat(yr_rows, ignore_index=True)
    yearly.to_csv(OUT / "yearly.csv", index=False)

    # ---- multiple testing ------------------------------------------------------------
    oos_g = grid[grid["sample"] == PRIMARY]
    r_pick_sel = sel_series(pk)
    trial = {key: sel_series(key) for key in grid_order()}
    sr = lambda a: float(np.mean(a) / np.std(a, ddof=1))
    trial_raw = np.array([sr(r) for r in trial.values()])
    trial_act_spy = np.array([sr(r - spy_sel) for r in trial.values()])
    trial_act_v1 = np.array([sr(r - r_v1_sel) for key, r in trial.items() if key != V1_KEY])
    N = oc.N_COMBINATIONS
    dsr = {"raw_return_N=18": deflated_sr(r_pick_sel, trial_raw, N),
           "active_return_vs_SPY_N=18": deflated_sr(r_pick_sel - spy_sel, trial_act_spy, N)}
    if pk != V1_KEY:
        dsr["active_return_vs_v1_N=18"] = deflated_sr(r_pick_sel - r_v1_sel, trial_act_v1, N)
    else:
        dsr["active_return_vs_v1_N=18"] = "pick is v1 itself: not defined"
    dsr["note"] = ("Bailey & Lopez de Prado (2014): SR0 = sqrt(V)*((1-g)*Z^-1(1-1/N) + g*Z^-1(1-1/(N e))), V = variance "
                   "of the trials' daily in-sample Sharpe ratios (active vs v1: the 17 non-v1 trials); DSR = PSR(SR0) "
                   "with skew/kurtosis correction. The 18 trials are highly correlated (same trades), so V is small and "
                   "SR0 low: the raw-return DSR mostly says the book has a positive Sharpe (it is ~the market). The "
                   "active-vs-v1 DSR is the relevant one for 'does the overlay add anything'.")
    # ex post (NOT a pre-registered test): the best OOS combination's active return vs v1, deflated for
    # having looked at 18 OOS results
    best_oos_cfg = oos_g.sort_values("sharpe", ascending=False).iloc[0]
    bk = (best_oos_cfg["weighting"], best_oos_cfg["cap"], best_oos_cfg["vol_target"])
    oos_trials_v1 = np.array([sr(oos_series(key) - r_v1_oos) for key in grid_order() if key != V1_KEY])
    ex_post = {"config": combo_key(*bk), "oos_sharpe": float(best_oos_cfg["sharpe"]),
               "oos_sharpe_v1": float(oos_g.loc[oos_g["is_v1"], "sharpe"].iloc[0]),
               "oos_sharpe_spy": float(best_oos_cfg["spy_sharpe"]),
               "in_sample_sharpe": float(sel_rows.loc[sel_rows["config"] == combo_key(*bk), "sharpe"].iloc[0]),
               "memmel_z_vs_v1": float(best_oos_cfg["sharpe_diff_z_vs_v1"]),
               "memmel_z_vs_spy": float(best_oos_cfg["sharpe_diff_z_vs_spy"])}
    if bk != V1_KEY:
        ex_post["psr_active_vs_v1_sr0_0"] = probabilistic_sr(oos_series(bk) - r_v1_oos, 0.0)
        ex_post["dsr_active_vs_v1_N=18"] = deflated_sr(oos_series(bk) - r_v1_oos, oos_trials_v1, N)
    ex_post["note"] = ("Chosen AFTER seeing the OOS results (cherry-pick of 18). Not a valid test; shown only to say "
                       "whether even the best ex-post overlay is distinguishable from v1.")
    sel_full = grid[grid["sample"] == SEL].copy()
    sel_full["sharpe"] = sel_full["sharpe_from_full_cap_placement"]
    pick_full = select_overlay(sel_full)
    startup = {"first_day_all_caps_place_full_weight": full_start,
               "n_initial_days_with_cap_cash": k_full,
               "pick_with_sharpe_from_that_day": combo_key(pick_full["weighting"], pick_full["cap"], pick_full["vol_target"]),
               "same_as_pre_registered_pick": (pick_full["weighting"], pick_full["cap"], pick_full["vol_target"]) == pk,
               "note": "robustness only; capped books hold cash while < 20 positions / < 7 members are open "
                       "(January 2016 sell-off), which flatters their full-period in-sample Sharpe"}
    mt = {
        "n_combinations": N,
        "n_in_sample_sharpe_above_v1": int((sel_rows["sharpe"] > float(sel_rows.loc[sel_rows["is_v1"], "sharpe"].iloc[0]) + 1e-12).sum()),
        "n_in_sample_sharpe_above_spy": int((sel_rows["sharpe"] > sel_rows["spy_sharpe"]).sum()),
        "n_oos_sharpe_above_v1": int((oos_g["sharpe"] > float(oos_g.loc[oos_g["is_v1"], "sharpe"].iloc[0]) + 1e-12).sum()),
        "n_oos_sharpe_above_spy": int((oos_g["sharpe"] > oos_g["spy_sharpe"]).sum()),
        "n_oos_total_return_above_v1": int((oos_g["total_return"] > float(oos_g.loc[oos_g["is_v1"], "total_return"].iloc[0]) + 1e-12).sum()),
        "n_oos_total_return_above_spy": int((oos_g["total_return"] > oos_g["spy_total_return"]).sum()),
        "n_oos_volatility_below_v1": int((oos_g["volatility"] < float(oos_g.loc[oos_g["is_v1"], "volatility"].iloc[0]) - 1e-12).sum()),
        "n_oos_volatility_below_spy": int((oos_g["volatility"] < oos_g["spy_volatility"]).sum()),
        "rank_correlation_is_vs_oos_sharpe_spearman": float(pd.Series(sel_rows["sharpe"].to_numpy()).rank().corr(
            pd.Series(oos_g["sharpe"].to_numpy()).rank())),
        "best_oos_sharpe_of_18_(ex_post)": {"config": oos_g.sort_values("sharpe", ascending=False).iloc[0]["config"],
                                             "sharpe": float(oos_g["sharpe"].max())},
        "deflated_sharpe_in_sample": dsr,
        "spy_in_sample_psr_vs_0": probabilistic_sr(spy_sel, 0.0),
        "ex_post_best_oos_vs_v1": ex_post,
        "startup_check": startup,
    }

    # ---- concentration: top member's share of the book over time --------------------
    conc_rows = []
    for wt in oc.WEIGHTINGS:
        for cap in oc.CAPS:
            b = books[(CONT, wt, cap)]
            for per, sl in (("in_sample_2016_2021", slice(0, k0)), ("oos_2022_2026", slice(k0, None)),
                            ("all", slice(None))):
                tm = b["top_member_share"][sl]
                tp = b["top_position_share"][sl]
                cc = b["top_member_code"][sl]
                act = b["n_positions"][sl] > 0
                vc = pd.Series(cc[act]).value_counts()
                conc_rows.append({"section": "summary", "weighting": wt, "cap": cap, "period": per,
                                  "n_days_with_positions": int(act.sum()),
                                  "top_member_share_mean": float(tm[act].mean()),
                                  "top_member_share_median": float(np.median(tm[act])),
                                  "top_member_share_p95": float(np.quantile(tm[act], 0.95)),
                                  "top_member_share_max": float(tm[act].max()),
                                  "share_of_days_top_member_above_15pct": float(np.mean(tm[act] > oc.MEMBER_CAP + 1e-9)),
                                  "most_frequent_top_member": inv_codes[int(vc.index[0])],
                                  "most_frequent_top_member_share_of_days": float(vc.iloc[0] / act.sum()),
                                  "top_position_share_mean": float(tp[act].mean()),
                                  "top_position_share_max": float(tp[act].max()),
                                  "share_of_days_top_position_above_5pct": float(np.mean(tp[act] > oc.POSITION_CAP + 1e-9)),
                                  "n_positions_mean": float(b["n_positions"][sl][act].mean()),
                                  "cash_share_mean_from_caps": float(1 - b["invested"][sl][act].mean()),
                                  "note": "continuous book, vol targeting off (vol targeting scales all weights equally)"})
            me = pd.DataFrame({"date": pd.DatetimeIndex(dates_c), "s": b["top_member_share"],
                               "m": [inv_codes.get(int(c), "") for c in b["top_member_code"]],
                               "p": b["top_position_share"]}).set_index("date").resample("ME").last()
            for d_, rr in me.iterrows():
                conc_rows.append({"section": "month_end", "weighting": wt, "cap": cap, "date": str(d_.date()),
                                  "top_member_share": rr["s"], "top_member": rr["m"], "top_position_share": rr["p"]})
    conc_df = pd.DataFrame(conc_rows)
    conc_df.to_csv(OUT / "concentration.csv", index=False)

    # ---- equity curves --------------------------------------------------------------
    eq = []
    for key in grid_order():
        eq.append(pd.DataFrame({"sample": SEL, "series": combo_key(*key), "date": dates_sel.astype(str),
                                "equity": np.cumprod(1 + sel_series(key))}))
        eq.append(pd.DataFrame({"sample": PRIMARY, "series": combo_key(*key), "date": dates_c[k0:].astype(str),
                                "equity": np.cumprod(1 + oos_series(key))}))
    for smp, dts, a, b_ in ((SEL, dates_sel, spy_sel, spy_vt_sel["r"]), (PRIMARY, dates_c[k0:], spy_oos, spy_vt_c["r"][k0:])):
        eq.append(pd.DataFrame({"sample": smp, "series": "SPY", "date": dts.astype(str), "equity": np.cumprod(1 + a)}))
        eq.append(pd.DataFrame({"sample": smp, "series": "SPY;vol_target=on", "date": dts.astype(str),
                                "equity": np.cumprod(1 + b_)}))
    for name, key in shown.items():
        eq.append(pd.DataFrame({"sample": CONT, "series": f"{name}[{combo_key(*key)}]", "date": dates_c.astype(str),
                                "equity": np.cumprod(1 + series[(CONT,) + key]["r"]),
                                "exposure": series[(CONT,) + key]["exposure"]}))
    eq.append(pd.DataFrame({"sample": CONT, "series": "SPY", "date": dates_c.astype(str), "equity": np.cumprod(1 + spy_c)}))
    eq.append(pd.DataFrame({"sample": CONT, "series": "SPY;vol_target=on", "date": dates_c.astype(str),
                            "equity": np.cumprod(1 + spy_vt_c["r"]), "exposure": spy_vt_c["exposure"]}))
    pd.concat(eq, ignore_index=True).to_csv(OUT / "equity_curves.csv", index=False)

    # ---- recommendation.json --------------------------------------------------------
    rkeys = ["n_trades", "period_start", "period_end", "total_return", "cagr", "volatility", "sharpe", "max_drawdown",
             "alpha_annual", "alpha_t_nw", "beta", "information_ratio", "tracking_error", "sharpe_diff_z_vs_spy",
             "sharpe_diff_z_vs_v1", "sharpe_minus_v1", "mean_exposure", "min_exposure", "cap_cost_per_year",
             "vt_cost_per_year", "spy_total_return", "spy_cagr", "spy_volatility", "spy_sharpe", "spy_max_drawdown"]
    clean = lambda dct: {k: dct.get(k) for k in rkeys}
    spy_vt_m = {smp: {**series_metrics(dts, r_), **{k: v for k, v in significance(r_, br_).items()
                                                    if k in ("alpha_annual", "alpha_t_nw", "beta", "information_ratio",
                                                             "sharpe_diff_memmel_z")},
                      "mean_exposure": float(ex_.mean())}
                for smp, dts, r_, br_, ex_ in ((SEL, dates_sel, spy_vt_sel["r"], spy_sel, spy_vt_sel["exposure"]),
                                               (PRIMARY, dates_c[k0:], spy_vt_c["r"][k0:], spy_oos, spy_vt_c["exposure"][k0:]))}
    vt_effect = {}
    for smp in (SEL, PRIMARY):
        a = grid[(grid["sample"] == smp)]
        on = a[a["vol_target"] == "on"].set_index(["weighting", "cap"])
        off = a[a["vol_target"] == "off"].set_index(["weighting", "cap"])
        vt_effect[smp] = {"mean_sharpe_change_on_minus_off_over_9": float((on["sharpe"] - off["sharpe"]).mean()),
                          "mean_vol_change_on_minus_off_over_9": float((on["volatility"] - off["volatility"]).mean()),
                          "mean_cagr_change_on_minus_off_over_9": float((on["cagr"] - off["cagr"]).mean()),
                          "spy_sharpe_change_with_vt": float(spy_vt_m[smp]["sharpe"] - a["spy_sharpe"].iloc[0]),
                          "spy_vol_change_with_vt": float(spy_vt_m[smp]["volatility"] - a["spy_volatility"].iloc[0]),
                          "spy_cagr_change_with_vt": float(spy_vt_m[smp]["cagr"] - a["spy_cagr"].iloc[0])}
    rec = {
        "question": "Do simple pre-specified risk overlays on the locked v1 strategy raise its Sharpe ratio, or lower "
                    "its risk without hurting return much?",
        "pre_registration": {"file": "src/backtest/overlay_config.py", "sha256": cfg_sha,
                             "selection_criterion": oc.SELECTION_TEXT,
                             "note": "No version control; the hash only detects later edits. The OOS period had been "
                                     "seen in v1, in the member-selection test and in earlier exploratory work: "
                                     "pseudo-OOS. The only clean test is the live paper fund going forward."},
        "locked_v1_signal": {"exit_rule": oc.V1_EXIT_RULE, "X_pct": oc.V1_X, "cost_per_side": config.COST},
        "n_combinations_tested": N,
        "primary_pick": {"weighting": pk[0], "cap": pk[1], "vol_target": pk[2], "config": combo_key(*pk),
                         "is_v1": pk == V1_KEY},
        "risk_reduction_pick": {"weighting": rk[0], "cap": rk[1], "vol_target": rk[2], "config": combo_key(*rk),
                                "is_v1": rk == V1_KEY, "v1_in_sample_cagr": cagr_v1,
                                "rule": "lowest in-sample volatility among |CAGR - CAGR_v1| <= 1 pp",
                                "check_one_sided_rule_CAGR_ge_v1_minus_1pp": combo_key(*rk1),
                                "one_sided_gives_same_pick": rk1 == rk,
                                "candidates": sel_rows.loc[(sel_rows["cagr"] - cagr_v1).abs() <= oc.RISK_PICK_CAGR_TOL + 1e-12,
                                                           ["config", "cagr", "volatility", "sharpe"]].to_dict("records")},
        "in_sample_selection": {n: clean(g(SEL, k)) for n, k in names.items()},
        "PRIMARY_oos_continuous": {n: clean(g(PRIMARY, k)) for n, k in names.items()},
        "descriptive_single_overlays_on_v1_not_picked": {n: {"in_sample": clean(g(SEL, k)),
                                                            "PRIMARY_oos": clean(g(PRIMARY, k))}
                                                        for n, k in extra.items()},
        "SPY_plus_vol_targeting": spy_vt_m,
        "vol_targeting_effect_strategy_vs_spy": vt_effect,
        "multiple_testing": mt,
        "self_check_v1_reproduction": check,
        "concentration_summary": conc_df[conc_df["section"] == "summary"].drop(columns=["section", "date", "top_member_share",
                                                                                         "top_member", "top_position_share"],
                                                                                errors="ignore").to_dict("records"),
        "notes": [
            "v1 signal unchanged: entry at the open after filing, skip on a rise > 10 %, hold 6 months, 0.1 % per side, "
            "all v1 exclusions; only the weights of the calendar-time book are overlaid.",
            "Caps are applied every day by water-filling; cap-induced reshuffling is charged 0.1 % (gross minus net "
            "turnover of continuing positions); v1's pro-rata funding of entries stays free as in v1 (F9).",
            "Vol targeting: exposure min(1, 15 %/sigma_63(t-1)), daily, no leverage, cash at 0 % (penalises the overlay: "
            "no T-bill series), 0.1 % on the change in exposure vs the drifted exposure; exposure 1 in the warm-up.",
            "rf = 0; SPY adjustment=all treated as total return; pseudo-OOS.",
        ],
        "runtime_seconds": round(time.time() - t0, 1),
    }
    with open(OUT / "recommendation.json", "w") as f:
        json.dump(json_clean(rec), f, indent=2)
    log(f"done in {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
