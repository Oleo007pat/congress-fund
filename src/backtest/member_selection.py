"""Member-selection follow-up test:  python3 -m src.backtest.member_selection

Question: does copying ONLY members with a strong past track record beat SPY?

Pre-registration: src/backtest/ms_config.py (written before any result; its
SHA-256 is stored in recommendation.json).  The v1 engine is reused unchanged
(signals, entry, skip rule, exits, costs, portfolio, samples); the only new
element is a per-signal member filter that uses information known BEFORE the
signal's entry day t:

  track record of member m at t = m's own v1-eligible signals whose hold_6m
  result was known before t (exit_idx < t; delisted exits only after
  DELISTED_KNOWN_LAG more trading days), with exit date in [t - W, t);
  value = hold_6m net return - SPY over the same window;
  score = mean * n / (n + k_shrink);  eligible iff n >= K;
  selection rule: score > 0, or top 20 % of eligible members by score at t.

Writes results/member_selection/:
  grid_summary.csv, recommendation.json, oos_continuous.csv, yearly.csv,
  significance.csv, selected_members.csv, robustness.csv, equity_curves.csv,
  concentration.csv, eligibility_by_year.csv, multiple_testing.csv,
  persistence.csv, decisions_chosen.csv
(ASSUMPTIONS.md in that folder is maintained by hand.)
"""
from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path
from statistics import NormalDist

import numpy as np
import pandas as pd

from . import config
from . import ms_config as mc
from .analysis import significance, yearly_table
from .engine import add_canonical_member, load_trades
from .portfolio import x_label
from .prices import load_pricebook
from .run import PRIMARY, SECONDARY, SEL, Evaluator, build_signal_table, top_member

EULER_GAMMA = 0.5772156649015329
ND = NormalDist()
TOP_PCT = int(round(mc.TOP_FRACTION * 100))


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def fnum(v):
    if isinstance(v, (np.floating, np.integer)):
        v = v.item()
    return None if isinstance(v, float) and not np.isfinite(v) else v


def json_clean(o):
    """Recursively convert numpy scalars and replace NaN/inf by None (valid JSON)."""
    if isinstance(o, dict):
        return {str(k): json_clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [json_clean(v) for v in o]
    if isinstance(o, (np.bool_,)):
        return bool(o)
    if isinstance(o, (np.floating, np.integer, float, int)) and not isinstance(o, bool):
        return fnum(o)
    if o is None or isinstance(o, (str, bool)):
        return o
    if hasattr(o, "item"):
        return json_clean(o.item())
    return str(o)


def w_label(W) -> str:
    return "all" if W is None else f"{W}m"


def cfg_key(W, K, rule, exit_rule, weighting, x) -> str:
    return f"W={w_label(W)};K={K};sel={rule};exit={exit_rule};weighting={weighting};X={x_label(x)}"


# ----------------------------------------------------------------------------
# Track record (the look-ahead-critical part)
# ----------------------------------------------------------------------------
def track_record_table(sig: pd.DataFrame, rule: str = mc.TRACK_EXIT_RULE,
                       delisted_lag: int = mc.DELISTED_KNOWN_LAG) -> pd.DataFrame:
    """One row per v1-eligible signal with a completed reference (hold_6m) result.

    known_idx = first day index on which the result is NOT yet usable; a track-record
    trade counts for a signal entering on day t iff known_idx < t:
      * horizon / member exits: known_idx = exit_idx (exit close is known after the
        close of the exit day; our entry is at the open of t > exit_idx);
      * delisted (piece ended): known_idx = exit_idx + delisted_lag, because the piece
        break is only certain after > PIECE_MAX_GAP trading days without a bar, or at a
        discontinuous resumption, which is earlier (conservative);
      * open_at_end: never completed -> not in the table.
    excess = net return after 0.1 % per side - SPY over the identical window."""
    e = sig[(sig["drop_reason"] == "") & sig[f"{rule}_exit_idx"].notna()]
    reason = e[f"{rule}_exit_reason"].to_numpy()
    e = e[reason != "open_at_end"]
    reason = e[f"{rule}_exit_reason"].to_numpy()
    exit_idx = e[f"{rule}_exit_idx"].astype(int).to_numpy()
    known = exit_idx + np.where(reason == "delisted", delisted_lag, 0)
    out = pd.DataFrame({"row_id": e["row_id"].to_numpy(), "member_canon": e["member_canon"].to_numpy(),
                        "entry_idx": e["entry_idx"].astype(int).to_numpy(), "exit_idx": exit_idx,
                        "known_idx": known, "exit_reason": reason,
                        "excess": (e[f"{rule}_net_ret"] - e[f"{rule}_spy_ret"]).to_numpy(float)})
    assert (out["exit_idx"] >= out["entry_idx"]).all()
    return out


def window_start_idx(cal: np.ndarray, t_idx: np.ndarray, W) -> np.ndarray:
    """Index of the first trading day on/after (date(t) - W calendar months); 0 for W=None."""
    t_idx = np.asarray(t_idx, int)
    if W is None:
        return np.zeros(len(t_idx), int)
    starts = [np.datetime64((pd.Timestamp(cal[t]) - pd.DateOffset(months=W)).date(), "D") for t in t_idx]
    return np.searchsorted(cal, np.array(starts, dtype="datetime64[D]"), side="left")


def score_panel(tr: pd.DataFrame, t_idx, cal, W, member_codes: dict) -> tuple[np.ndarray, np.ndarray]:
    """(n, s): (len(t_idx), M) counts and sums of track-record excess per member at each
    day t, using ONLY trades with known_idx < t and exit_idx >= window start."""
    t_idx = np.asarray(t_idx, int)
    M = len(member_codes)
    code = tr["member_canon"].map(member_codes).to_numpy()
    if np.isnan(code.astype(float)).any():
        raise ValueError("track-record member without code")
    code = code.astype(int)
    known = tr["known_idx"].to_numpy(int)
    ex_i = tr["exit_idx"].to_numpy(int)
    x = tr["excess"].to_numpy(float)
    ws = window_start_idx(cal, t_idx, W)
    n = np.zeros((len(t_idx), M))
    s = np.zeros((len(t_idx), M))
    for k, (t, w0) in enumerate(zip(t_idx, ws)):
        m = (known < t) & (ex_i >= w0)
        if m.any():
            n[k] = np.bincount(code[m], minlength=M)
            s[k] = np.bincount(code[m], weights=x[m], minlength=M)
    return n, s


def shrunk_score(s: np.ndarray, n: np.ndarray, k_shrink: float) -> np.ndarray:
    """score = mean * n / (n + k) = s / (n + k); NaN if n = 0."""
    s = np.asarray(s, float)
    n = np.asarray(n, float)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(n > 0, (s / np.where(n > 0, n, 1.0)) * n / (n + k_shrink), np.nan)


def top_cutoff(n_eligible) -> np.ndarray:
    """ceil(TOP_FRACTION * N) in integer arithmetic (avoids 0.2*15 = 3.0000000000000004)."""
    N = np.asarray(n_eligible, int)
    return (N * TOP_PCT + 99) // 100


def decide(n: np.ndarray, s: np.ndarray, K: int, rule: str, k_shrink: float) -> dict:
    """Selection at each (t, member).  Ranking universe for top20: all members with
    n >= K at t.  rank = 1 + number of eligible members with a strictly higher score."""
    score = shrunk_score(s, n, k_shrink)
    elig = n >= K
    n_elig = elig.sum(axis=1)
    rank = np.full(n.shape, np.nan)
    for k in range(n.shape[0]):
        if n_elig[k] == 0:
            continue
        sc = np.sort(score[k, elig[k]])
        cand = score[k, elig[k]]
        rank[k, elig[k]] = 1 + (len(sc) - np.searchsorted(sc, cand, side="right"))
    cutoff = top_cutoff(n_elig)
    if rule == "score_pos":
        sel = elig & (np.nan_to_num(score, nan=0.0) > 0)
    elif rule == "top20":
        sel = elig & (np.nan_to_num(rank, nan=np.inf) <= cutoff[:, None])
    else:
        raise ValueError(rule)
    with np.errstate(invalid="ignore", divide="ignore"):
        mean = np.where(n > 0, s / np.where(n > 0, n, 1.0), np.nan)
    return {"n": n, "mean": mean, "score": score, "eligible": elig, "n_eligible": n_elig, "rank": rank,
            "cutoff": cutoff, "selected": sel}


def eligible_signals(sig: pd.DataFrame) -> pd.DataFrame:
    return sig[(sig["drop_reason"] == "") & sig["entry_idx"].notna()]


def signal_decisions(sig, tr, cal, W, K, rule, k_shrink, member_codes, panel=None) -> pd.DataFrame:
    """Decision for every v1-eligible signal (row_id), computed at its entry day."""
    el = eligible_signals(sig)
    t_all = el["entry_idx"].astype(int).to_numpy()
    t_u, inv = np.unique(t_all, return_inverse=True)
    if panel is None:
        panel = score_panel(tr, t_u, cal, W, member_codes)
    n, s = panel
    d = decide(n, s, K, rule, k_shrink)
    j = el["member_canon"].map(member_codes).to_numpy(int)
    return pd.DataFrame({"row_id": el["row_id"].to_numpy(), "member_canon": el["member_canon"].to_numpy(),
                         "sample": el["sample"].to_numpy(), "entry_idx": t_all,
                         "n_track": d["n"][inv, j].astype(int), "mean_excess_track": d["mean"][inv, j],
                         "score": d["score"][inv, j], "eligible": d["eligible"][inv, j],
                         "n_eligible_members": d["n_eligible"][inv], "rank": d["rank"][inv, j],
                         "cutoff_rank": d["cutoff"][inv], "selected": d["selected"][inv, j]})


def member_codes_of(sig) -> dict:
    return {m: i for i, m in enumerate(sorted(sig["member_canon"].dropna().unique()))}


def select_ms_configuration(grid_sel: pd.DataFrame) -> pd.Series:
    """Pre-registered criterion (ms_config.SELECTION_TEXT).  Receives ONLY rows of the
    selection sample; refuses anything else."""
    samples = set(grid_sel["sample"].unique())
    if samples != {mc.SELECTION_SAMPLE}:
        raise ValueError(f"selection must see only {mc.SELECTION_SAMPLE}, got {samples}")
    g = grid_sel[grid_sel["n_trades"] >= mc.SELECT_MIN_TRADES].copy()
    g = g[np.isfinite(g[mc.SELECTION_METRIC].astype(float))]
    if g.empty:
        raise ValueError("no combination satisfies n_trades >= SELECT_MIN_TRADES")
    order =(g["W"].map({w_label(k): i for i, k in enumerate(mc.W_GRID)}) * 10 ** 8
             + g["K"].map({k: i for i, k in enumerate(mc.K_GRID)}) * 10 ** 6
             + g["selection"].map({k: i for i, k in enumerate(mc.SELECTION_RULES)}) * 10 ** 4
             + g["exit_rule"].map({k: i for i, k in enumerate(mc.EXIT_RULES)}) * 10 ** 2
             + g["weighting"].map({k: i for i, k in enumerate(mc.WEIGHTINGS)}) * 10
             + g["X"].map({x_label(k): i for i, k in enumerate(mc.X_GRID)}))
    g["_o"] = order
    best = g[mc.SELECTION_METRIC].max()
    top = g[g[mc.SELECTION_METRIC] >= best - 1e-12].sort_values("_o")
    return top.iloc[0].drop(labels="_o")


# ----------------------------------------------------------------------------
# Multiple testing
# ----------------------------------------------------------------------------
def expected_max_sr(var_sr: float, n_trials: int) -> float:
    """Bailey & Lopez de Prado (2014): E[max SR] under H0 for N trials with variance V."""
    if n_trials <= 1:
        return 0.0
    return float(np.sqrt(var_sr) * ((1 - EULER_GAMMA) * ND.inv_cdf(1 - 1.0 / n_trials)
                                    + EULER_GAMMA * ND.inv_cdf(1 - 1.0 / (n_trials * np.e))))


def probabilistic_sr(r: np.ndarray, sr0: float) -> dict:
    """PSR(SR0) with skewness/kurtosis correction (per-period SR, non-annualised)."""
    r = np.asarray(r, float)
    T = len(r)
    mu, sd = r.mean(), r.std(ddof=1)
    sr = mu / sd
    z = (r - mu) / r.std(ddof=0)
    g3 = float(np.mean(z ** 3))
    g4 = float(np.mean(z ** 4))
    den = np.sqrt(max(1e-12, 1 - g3 * sr + (g4 - 1) / 4.0 * sr ** 2))
    stat = (sr - sr0) * np.sqrt(T - 1) / den
    return {"sr_daily": sr, "sr0_daily": sr0, "skew": g3, "kurtosis": g4, "T": T, "z": float(stat),
            "psr": ND.cdf(float(stat))}


def deflated_sr(r_chosen: np.ndarray, trial_srs: np.ndarray, n_trials: int) -> dict:
    v = float(np.var(np.asarray(trial_srs, float), ddof=1))
    sr0 = expected_max_sr(v, n_trials)
    out = probabilistic_sr(r_chosen, sr0)
    out.update({"n_trials": n_trials, "var_trial_sr_daily": v, "dsr": out.pop("psr")})
    return out


# ----------------------------------------------------------------------------
def main():
    t0 = time.time()
    OUT = mc.OUT_DIR
    OUT.mkdir(parents=True, exist_ok=True)
    cfg_path = Path(mc.__file__)
    cfg_sha = hashlib.sha256(cfg_path.read_bytes()).hexdigest()

    log("loading trades, prices, building v1 signal table (engine unchanged)")
    trades = add_canonical_member(load_trades())
    tmap = pd.read_csv(config.TICKER_MAP_CSV, keep_default_na=False, na_values=[])
    syms = set(tmap.loc[tmap["price_symbol"] != "", "price_symbol"])
    pb = load_pricebook(syms)
    sig = build_signal_table(trades, tmap, pb)
    cal = pb.cal
    ev = Evaluator(sig, pb)
    cut_idx = pb.last_day_on_or_before(config.IS_END)
    codes = member_codes_of(sig)
    tr = track_record_table(sig)
    log(f"eligible signals {len(eligible_signals(sig))}, completed hold_6m track-record trades {len(tr)}, "
        f"members {len(codes)}")

    # ---- decisions for every member filter (W, K, rule) ----------------------------
    el = eligible_signals(sig)
    t_u = np.unique(el["entry_idx"].astype(int).to_numpy())
    panels = {w_label(W): score_panel(tr, t_u, cal, W, codes) for W in mc.W_GRID}
    filters = [(W, K, rule) for W in mc.W_GRID for K in mc.K_GRID for rule in mc.SELECTION_RULES]
    dec = {f: signal_decisions(sig, tr, cal, *f, mc.K_SHRINK, codes, panel=panels[w_label(f[0])]) for f in filters}

    # ---- self-check: no post-2021 information in the in-sample decisions ----------
    tr_trunc = tr[tr["known_idx"] <= cut_idx]
    sig_is = sig[sig["sample"] == "in_sample"]
    oos_check = {}
    for f in filters:
        d_trunc = signal_decisions(sig_is, tr_trunc, cal, *f, mc.K_SHRINK, codes)
        a = dec[f].set_index("row_id")
        b = d_trunc.set_index("row_id")
        ids = b.index[b["entry_idx"] <= cut_idx]
        same = bool((a.loc[ids, "selected"].to_numpy() == b.loc[ids, "selected"].to_numpy()).all()
                    and np.allclose(a.loc[ids, "score"].to_numpy(float), b.loc[ids, "score"].to_numpy(float),
                                    equal_nan=True))
        oos_check["/".join(map(str, (w_label(f[0]), f[1], f[2])))] = same
        if not same:
            raise AssertionError(f"post-2021 information changes in-sample decisions for {f}")
    log("self-check passed: in-sample decisions identical with all post-2021 track-record info removed")

    # ---- eligibility per year -----------------------------------------------------
    ey = []
    for f in filters:
        d = dec[f].merge(sig[["row_id", "year", "filing_date"]], on="row_id")
        d["filing_year"] = d["filing_date"].str[:4].astype(int)
        for y, g in d.groupby("filing_year"):
            ey.append({"W": w_label(f[0]), "K": f[1], "selection": f[2], "filing_year": y,
                       "n_v1_eligible_signals": len(g), "n_member_track_eligible": int(g["eligible"].sum()),
                       "n_selected": int(g["selected"].sum()),
                       "n_members_with_signals": int(g["member_canon"].nunique()),
                       "n_members_selected": int(g.loc[g["selected"], "member_canon"].nunique()),
                       "median_eligible_members_at_signal": float(g["n_eligible_members"].median())})
    pd.DataFrame(ey).to_csv(OUT / "eligibility_by_year.csv", index=False)

    # ---- grid: 128 combinations x (selection sample, primary OOS) ---------------
    def mask_of(ids):
        return lambda tk, pr, ids=ids: tk["row_id"].isin(ids)

    log("running grid: 128 combinations x 2 samples")
    rows, sel_series, oos_series = [], {}, {}
    for f in filters:
        ids = set(dec[f].loc[dec[f]["selected"], "row_id"])
        for rule in mc.EXIT_RULES:
            for wt in mc.WEIGHTINGS:
                for x in mc.X_GRID:
                    key = cfg_key(f[0], f[1], f[2], rule, wt, x)
                    for sample in (SEL, PRIMARY):
                        res = ev.evaluate(sample, rule, wt, x, extra_mask=mask_of(ids))
                        row = {"config": key, "W": w_label(f[0]), "K": f[1], "selection": f[2],
                               "k_shrink": mc.K_SHRINK, "n_selected_signals_any_sample": len(ids), **res["row"]}
                        row["X"] = x_label(x)
                        if "r" in res and len(res["r"]) > 10:
                            sg = significance(res["r"], res["br"])
                            row.update({k: sg[k] for k in ("alpha_annual", "alpha_t_nw", "beta", "information_ratio",
                                                           "tracking_error", "sharpe_diff_memmel_z")})
                            (sel_series if sample == SEL else oos_series)[key] = (res["r"], res["br"])
                        rows.append(row)
    grid = pd.DataFrame(rows)
    front = ["sample", "config", "W", "K", "selection", "exit_rule", "weighting", "X", "k_shrink"]
    grid = grid[front + [c for c in grid.columns if c not in front]]
    grid.to_csv(OUT / "grid_summary.csv", index=False)
    n_tested = int((grid["sample"] == SEL).sum())
    assert n_tested == mc.N_COMBINATIONS, n_tested

    # ---- pre-registered selection (selection sample ONLY) ---------------------
    sel_rows = grid[grid["sample"] == SEL].copy()
    chosen = select_ms_configuration(sel_rows)
    cW = None if chosen["W"] == "all" else int(chosen["W"][:-1])
    cK, crule = int(chosen["K"]), chosen["selection"]
    cexit, cwt = chosen["exit_rule"], chosen["weighting"]
    cx = None if chosen["X"] == "none" else float(chosen["X"])
    cfilter = (cW, cK, crule)
    ckey = chosen["config"]
    log(f"selected: {ckey}  (in-sample Sharpe {chosen['sharpe']:.4f} vs SPY {chosen['spy_sharpe']:.4f})")
    chosen_ids = set(dec[cfilter].loc[dec[cfilter]["selected"], "row_id"])

    def g(sample, key=ckey):
        return grid[(grid["sample"] == sample) & (grid["config"] == key)].iloc[0].to_dict()

    # ---- comparisons: chosen vs v1 chosen vs all members (same exit/weight/X) ----
    configs = {"chosen_member_selection": (chosen_ids, cexit, cwt, cx),
               "v1_chosen_all_members(hold_6m/amount/X=10)": (None, "hold_6m", "amount", 10.0)}
    if (cexit, cwt, cx) != ("hold_6m", "amount", 10.0):
        configs[f"all_members_same_exit_weight_X({cexit}/{cwt}/X={x_label(cx)})"] = (None, cexit, cwt, cx)
    log("continuous books, significance, yearly, equity curves")
    oos_rows, sig_rows, yr_rows, eq_parts, results = [], [], [], [], {}
    for name, (ids, rule, wt, x) in configs.items():
        kw = {} if ids is None else {"extra_mask": mask_of(ids)}
        cfg = (ckey if ids is not None else f"all_members;exit={rule};weighting={wt};X={x_label(x)}")
        full = ev.evaluate("all_continuous", rule, wt, x, **kw)
        prim = ev.evaluate(PRIMARY, rule, wt, x, **kw)
        variants = {"PRIMARY_continuous_book_2022-01-03_to_end": prim,
                    "SECONDARY_oos_signals_only_book": ev.evaluate(SECONDARY, rule, wt, x, **kw),
                    "continuous_excl_2022_window_2023-01-03_to_end": ev.evaluate(
                        PRIMARY, rule, wt, x, window_start=pb.first_day_on_or_after("2023-01-01"), **kw),
                    "selection_sample_in_sample_cut2021": ev.evaluate(SEL, rule, wt, x, **kw)}
        results[name] = {"full": full, **variants}
        for vname, res in variants.items():
            rw = res["row"]
            oos_rows.append({"config_name": name, "config": cfg, "measure": vname,
                             **{k: rw.get(k) for k in ("period_start", "period_end", "n_trades",
                                                       "n_trades_active_in_window", "total_return", "cagr",
                                                       "volatility", "sharpe", "max_drawdown", "spy_total_return",
                                                       "spy_cagr", "spy_volatility", "spy_sharpe", "spy_max_drawdown",
                                                       "mean_excess_vs_spy", "mean_excess_vs_spy_completed",
                                                       "share_beat_spy", "n_delisted_exit", "n_open_at_end")}})
            if "r" in res:
                sig_rows.append({"config_name": name, "config": cfg, "measure": vname,
                                 "period_start": rw["period_start"], "period_end": rw["period_end"],
                                 **significance(res["r"], res["br"])})
        y = yearly_table(full["dates"], full["r"], full["br"])
        y.insert(0, "config", cfg)
        y.insert(0, "config_name", name)
        yr_rows.append(y)
        for s_name, res in (("all_continuous", full), (PRIMARY, prim), (SEL, variants["selection_sample_in_sample_cut2021"])):
            eq_parts.append(pd.DataFrame({"sample": s_name, "series": f"strategy[{name}]", "config": cfg,
                                          "date": res["dates"].astype(str), "equity": np.cumprod(1 + res["r"])}))
            eq_parts.append(pd.DataFrame({"sample": s_name, "series": f"SPY_same_days_as[{name}]", "config": cfg,
                                          "date": res["dates"].astype(str), "equity": np.cumprod(1 + res["br"])}))
    # common-start series for the figure: from the chosen book's first day
    ch_full = results["chosen_member_selection"]["full"]
    v1_full = results["v1_chosen_all_members(hold_6m/amount/X=10)"]["full"]
    d0 = ch_full["dates"][0]
    v1_mask = v1_full["dates"] >= d0
    v1_r = pd.Series(v1_full["r"][v1_mask], index=v1_full["dates"][v1_mask]).reindex(ch_full["dates"]).fillna(0.0)
    for nm, rr in (("strategy[chosen_member_selection]", ch_full["r"]), ("SPY_same_days", ch_full["br"]),
                   ("v1_chosen_all_members", v1_r.to_numpy())):
        eq_parts.append(pd.DataFrame({"sample": "common_start_chosen_book", "series": nm, "config": "",
                                      "date": ch_full["dates"].astype(str), "equity": np.cumprod(1 + rr)}))
    pd.DataFrame(oos_rows).to_csv(OUT / "oos_continuous.csv", index=False)
    pd.DataFrame(sig_rows).to_csv(OUT / "significance.csv", index=False)
    pd.concat(yr_rows).to_csv(OUT / "yearly.csv", index=False)
    pd.concat(eq_parts, ignore_index=True).to_csv(OUT / "equity_curves.csv", index=False)

    # ---- multiple testing --------------------------------------------------------
    log("multiple testing")
    oos_rows_g = grid[grid["sample"] == PRIMARY]
    n_is_beat = int((sel_rows["sharpe"] > sel_rows["spy_sharpe"]).sum())
    n_is_beat_elig = int(((sel_rows["sharpe"] > sel_rows["spy_sharpe"]) & (sel_rows["n_trades"] >= mc.SELECT_MIN_TRADES)).sum())
    n_oos_beat = int((oos_rows_g["sharpe"] > oos_rows_g["spy_sharpe"]).sum())
    n_oos_ret_beat = int((oos_rows_g["total_return"] > oos_rows_g["spy_total_return"]).sum())
    n_is_elig = int((sel_rows["n_trades"] >= mc.SELECT_MIN_TRADES).sum())
    is_t = float(chosen["alpha_t_nw"])
    p_is = 1 - ND.cdf(is_t)
    t_crit_1s = ND.inv_cdf(1 - mc.BONFERRONI_ALPHA / mc.N_COMBINATIONS)
    prim_sig = [s for s in sig_rows if s["config_name"] == "chosen_member_selection"
                and s["measure"].startswith("PRIMARY")][0]
    p_oos = 1 - ND.cdf(prim_sig["alpha_t_nw"])
    best_oos = oos_rows_g.sort_values("alpha_t_nw", ascending=False).iloc[0]
    best_is = sel_rows.sort_values("alpha_t_nw", ascending=False).iloc[0]
    r_ch, br_ch = sel_series[ckey]
    trial_sr_act = np.array([np.mean(r - b) / np.std(r - b, ddof=1) for r, b in sel_series.values()])
    trial_sr_raw = np.array([np.mean(r) / np.std(r, ddof=1) for r, b in sel_series.values()])
    dsr = {f"{lab}_N={N}": deflated_sr(rc, srs, N)
           for lab, rc, srs in (("active_return_vs_SPY", r_ch - br_ch, trial_sr_act), ("raw_return", r_ch, trial_sr_raw))
           for N in (mc.N_COMBINATIONS, len(filters))}
    psr_spy = probabilistic_sr(br_ch, 0.0)
    mt = {
        "n_combinations_tested": n_tested,
        "n_combinations_with_n_trades_ge_200_in_sample": n_is_elig,
        "n_in_sample_sharpe_above_spy": n_is_beat,
        "n_in_sample_sharpe_above_spy_and_n_trades_ge_200": n_is_beat_elig,
        "n_oos_continuous_sharpe_above_spy": n_oos_beat,
        "n_oos_continuous_total_return_above_spy": n_oos_ret_beat,
        "chosen_in_sample_alpha_t_nw": is_t,
        "chosen_in_sample_alpha_p_one_sided": p_is,
        "chosen_in_sample_alpha_p_bonferroni_128": min(1.0, p_is * mc.N_COMBINATIONS),
        "bonferroni_critical_t_one_sided_5pct_128": t_crit_1s,
        "max_in_sample_alpha_t_over_128": {"config": best_is["config"], "alpha_t_nw": float(best_is["alpha_t_nw"]),
                                           "p_bonferroni_128": min(1.0, (1 - ND.cdf(float(best_is["alpha_t_nw"]))) * mc.N_COMBINATIONS)},
        "chosen_oos_alpha_t_nw": prim_sig["alpha_t_nw"],
        "chosen_oos_alpha_p_one_sided_single_pre_registered_test": p_oos,
        "max_oos_alpha_t_over_128_(ex_post_cherry_pick)": {"config": best_oos["config"], "alpha_t_nw": float(best_oos["alpha_t_nw"]),
                                                           "p_bonferroni_128": min(1.0, (1 - ND.cdf(float(best_oos["alpha_t_nw"]))) * mc.N_COMBINATIONS)},
        "deflated_sharpe_in_sample": dsr,
        "spy_in_sample_psr_vs_0_same_days": psr_spy,
        "note": ("DSR per Bailey & Lopez de Prado (2014): SR0 = sqrt(V)*((1-g)*Z^-1(1-1/N) + g*Z^-1(1-1/(N e))), "
                 "V = variance of the 128 trials' daily in-sample Sharpe ratios; DSR = PSR(SR0) with skew/kurtosis "
                 "correction. N=8 (distinct member filters) is shown as a lenient lower bound on the trial count. "
                 "The active-return DSR is the probability that the chosen config's true in-sample information "
                 "ratio vs SPY exceeds the expected maximum of 128 zero-skill trials."),
    }
    mt_rows = [{"item": k, "value": json.dumps(v, default=fnum) if isinstance(v, dict) else v} for k, v in mt.items()]
    pd.DataFrame(mt_rows).to_csv(OUT / "multiple_testing.csv", index=False)

    # ---- concentration ----------------------------------------------------------
    log("concentration, members, robustness")
    conc = []
    tk_is = results["chosen_member_selection"]["selection_sample_in_sample_cut2021"]
    tk_oos = results["chosen_member_selection"]["PRIMARY_continuous_book_2022-01-03_to_end"]
    for uname, res, sample in (("in_sample_cut2021", tk_is, SEL), ("oos_continuous_active_in_window", tk_oos, PRIMARY)):
        tk = res["tk"]
        if sample == PRIMARY:
            tk = tk[tk[f"{res['prule']}_exit_idx"] >= ev.oos_start]
        mem, share = top_member(tk, cwt)
        ids_wo = chosen_ids - set(sig.loc[sig["member_canon"] == mem, "row_id"])
        no = ev.evaluate(sample, cexit, cwt, cx, extra_mask=mask_of(ids_wo))
        wcol = f"weight_{cwt}"
        wsum = tk.groupby("member_canon")[wcol].sum().sort_values(ascending=False)
        ids_wo2 = chosen_ids - set(sig.loc[sig["member_canon"].isin(wsum.index[:2]), "row_id"])
        no2 = ev.evaluate(sample, cexit, cwt, cx, extra_mask=mask_of(ids_wo2))
        conc.append({"config": ckey, "universe": uname, "n_trades": len(tk), "n_members": int(tk["member_canon"].nunique()),
                     "top_member": mem, "top_member_weight_share": share,
                     "top_member_n_trades": int((tk["member_canon"] == mem).sum()),
                     "top3_members_weight_share": float(wsum.iloc[:3].sum() / wsum.sum()),
                     "top1pct_trades_weight_share": float(tk[wcol].nlargest(max(1, int(round(0.01 * len(tk))))).sum()
                                                          / tk[wcol].sum()),
                     "base_total_return": res["row"]["total_return"], "base_sharpe": res["row"]["sharpe"],
                     "excl_top_member_total_return": no["row"].get("total_return"),
                     "excl_top_member_sharpe": no["row"].get("sharpe"),
                     "excl_top_member_alpha_t_nw": significance(no["r"], no["br"])["alpha_t_nw"] if "r" in no else np.nan,
                     "top2_members": "; ".join(wsum.index[:2]),
                     "excl_top2_members_total_return": no2["row"].get("total_return"),
                     "excl_top2_members_sharpe": no2["row"].get("sharpe"),
                     "spy_total_return": res["row"]["spy_total_return"], "spy_sharpe": res["row"]["spy_sharpe"],
                     "excl_top_member_spy_sharpe": no["row"].get("spy_sharpe"),
                     "note": "top member(s) trades removed; selection decisions of other members unchanged"})
    conc_df = pd.DataFrame(conc)
    conc_df.to_csv(OUT / "concentration.csv", index=False)

    # ---- selected members: top 15 by weight + year-to-year stability ------------
    sm = []
    for uname, res, sample in (("top15_in_sample_cut2021", tk_is, SEL), ("top15_oos_continuous_active_in_window", tk_oos, PRIMARY)):
        tk = res["tk"]
        if sample == PRIMARY:
            tk = tk[tk[f"{res['prule']}_exit_idx"] >= ev.oos_start]
        pr = res["prule"]
        wcol = f"weight_{cwt}"
        gm = tk.assign(_ex=tk[f"{pr}_net_ret"] - tk[f"{pr}_spy_ret"]).groupby("member_canon")
        t15 = pd.DataFrame({"n_trades": gm.size(), "weight": gm[wcol].sum(), "mean_trade_excess_vs_spy": gm["_ex"].mean(),
                            "first_entry": gm["entry_date"].min(), "last_entry": gm["entry_date"].max()})
        t15["weight_share"] = t15["weight"] / t15["weight"].sum()
        t15 = t15.sort_values("weight", ascending=False).head(15).reset_index()
        for i, r_ in t15.iterrows():
            sm.append({"section": uname, "rank": i + 1, "member": r_["member_canon"], "n_trades": int(r_["n_trades"]),
                       "weight": r_["weight"], "weight_share": r_["weight_share"],
                       "mean_trade_excess_vs_spy": r_["mean_trade_excess_vs_spy"],
                       "first_entry": r_["first_entry"], "last_entry": r_["last_entry"]})
    # stability: (a) members qualified (eligible & passing) on the first trading day of each year;
    #            (b) members with >= 1 executed selected trade entered in that year
    years = list(range(2016, int(str(cal[-1])[:4]) + 1))
    y_idx = np.array([pb.first_day_on_or_after(f"{y}-01-01") for y in years])
    pn = score_panel(tr, y_idx, cal, cW, codes)
    dd = decide(*pn, cK, crule, mc.K_SHRINK)
    inv_codes = {v: k for k, v in codes.items()}
    full_tk = results["chosen_member_selection"]["full"]["tk"]
    traded = {y: set(full_tk.loc[full_tk["entry_date"].str[:4] == str(y), "member_canon"]) for y in years}
    prev_q, prev_t = None, None
    for k, y in enumerate(years):
        q = {inv_codes[j] for j in np.flatnonzero(dd["selected"][k])}
        tset = traded[y]
        jac = lambda a, b: (len(a & b) / len(a | b)) if (a is not None and (a | b)) else np.nan
        sm.append({"section": "yearly_stability", "year": y, "as_of": str(cal[y_idx[k]]),
                   "n_members_track_eligible": int(dd["n_eligible"][k]), "n_members_qualified": len(q),
                   "qualified_overlap_with_prev_year": len(q & prev_q) if prev_q is not None else np.nan,
                   "qualified_jaccard_vs_prev_year": jac(q, prev_q) if prev_q is not None else np.nan,
                   "n_members_traded_selected": len(tset),
                   "traded_overlap_with_prev_year": len(tset & prev_t) if prev_t is not None else np.nan,
                   "traded_jaccard_vs_prev_year": jac(tset, prev_t) if prev_t is not None else np.nan,
                   "qualified_members": "; ".join(sorted(q))})
        prev_q, prev_t = q, tset
    sm_df = pd.DataFrame(sm)
    sm_df.to_csv(OUT / "selected_members.csv", index=False)

    # ---- robustness: neighbours (same exit/weight/X) and k_shrink sensitivity -----
    rob = []
    keep = ("n_trades", "total_return", "cagr", "sharpe", "max_drawdown", "spy_total_return", "spy_sharpe",
            "alpha_annual", "alpha_t_nw", "information_ratio")
    for f in filters:
        key = cfg_key(*f, cexit, cwt, cx)
        a, b = g(SEL, key), g(PRIMARY, key)
        rob.append({"variant": "neighbour" if f != cfilter else "chosen", "config": key, "k_shrink": mc.K_SHRINK,
                    **{f"is_{k}": a.get(k) for k in keep}, **{f"oos_{k}": b.get(k) for k in keep}})
    other_wt = [w for w in mc.WEIGHTINGS if w != cwt][0]
    key = cfg_key(*cfilter, cexit, other_wt, cx)
    a, b = g(SEL, key), g(PRIMARY, key)
    rob.append({"variant": f"same filter/exit/X, {other_wt} weighting", "config": key, "k_shrink": mc.K_SHRINK,
                **{f"is_{k}": a.get(k) for k in keep}, **{f"oos_{k}": b.get(k) for k in keep}})
    for k_s in mc.K_SHRINK_SENSITIVITY:
        d_k = signal_decisions(sig, tr, cal, *cfilter, k_s, codes, panel=panels[w_label(cW)])
        ids_k = set(d_k.loc[d_k["selected"], "row_id"])
        a = ev.evaluate(SEL, cexit, cwt, cx, extra_mask=mask_of(ids_k))
        b = ev.evaluate(PRIMARY, cexit, cwt, cx, extra_mask=mask_of(ids_k))
        sa, sb = significance(a["r"], a["br"]), significance(b["r"], b["br"])
        rob.append({"variant": f"k_shrink={k_s} (sensitivity only)", "config": ckey, "k_shrink": k_s,
                    "n_decisions_changed_vs_k10": int((d_k["selected"].to_numpy() != dec[cfilter]["selected"].to_numpy()).sum()),
                    **{f"is_{k}": (a["row"] | sa).get(k) for k in keep}, **{f"oos_{k}": (b["row"] | sb).get(k) for k in keep}})
    # warm-up: the track records start empty; re-run the selection with the Sharpe measured from 2018
    log("robustness: selection re-run with Sharpe from 2018-01-02 (warm-up)")
    ws18 = pb.first_day_on_or_after("2018-01-01")
    wrows = []
    for f in filters:
        ids = set(dec[f].loc[dec[f]["selected"], "row_id"])
        for rule in mc.EXIT_RULES:
            for wt in mc.WEIGHTINGS:
                for x in mc.X_GRID:
                    rr = ev.evaluate(SEL, rule, wt, x, extra_mask=mask_of(ids), window_start=ws18)["row"]
                    rr.update({"config": cfg_key(*f, rule, wt, x), "W": w_label(f[0]), "K": f[1], "selection": f[2],
                               "X": x_label(x)})
                    wrows.append(rr)
    wdf = pd.DataFrame(wrows)
    wch = select_ms_configuration(wdf)
    wx = None if wch["X"] == "none" else float(wch["X"])
    wf = (None if wch["W"] == "all" else int(wch["W"][:-1]), int(wch["K"]), wch["selection"])
    wo = g(PRIMARY, wch["config"])
    rob.append({"variant": "selection re-run, Sharpe from 2018-01-02 (warm-up)", "config": wch["config"],
                "k_shrink": mc.K_SHRINK, "same_as_chosen": wch["config"] == ckey,
                "is_sharpe": wch["sharpe"], "is_spy_sharpe": wch["spy_sharpe"], "is_n_trades": wch["n_trades"],
                "is_total_return": wch["total_return"],
                **{f"oos_{k}": wo.get(k) for k in keep},
                "n_in_sample_sharpe_above_spy_from_2018": int((wdf["sharpe"] > wdf["spy_sharpe"]).sum())})
    rob_df = pd.DataFrame(rob)
    rob_df.to_csv(OUT / "robustness.csv", index=False)

    # ---- persistence: per-trade hold_6m excess of selected vs not selected signals --
    log("persistence (per-trade)")
    pers = []
    trx = tr.set_index("row_id")["excess"]
    wamt = sig.set_index("row_id")["weight_amount"]
    for f in filters:
        d = dec[f].copy()
        d["excess"] = d["row_id"].map(trx)          # completed hold_6m result (NaN if open at end)
        d["w"] = d["row_id"].map(wamt)
        d = d[d["excess"].notna()]
        for smp, gs in d.groupby("sample"):
            a_ = gs[gs["selected"]]
            b_ = gs[gs["eligible"] & ~gs["selected"]]
            c_ = gs[~gs["selected"]]
            se = np.sqrt(a_["excess"].var(ddof=1) / len(a_) + b_["excess"].var(ddof=1) / len(b_)) if len(a_) > 1 and len(b_) > 1 else np.nan
            pers.append({"W": w_label(f[0]), "K": f[1], "selection": f[2], "sample": smp,
                         "n_selected": len(a_), "n_eligible_not_selected": len(b_), "n_not_selected_all": len(c_),
                         "mean_excess_selected": a_["excess"].mean(),
                         "mean_excess_eligible_not_selected": b_["excess"].mean(),
                         "mean_excess_not_selected_all": c_["excess"].mean(),
                         "diff_selected_minus_eligible_not_selected": a_["excess"].mean() - b_["excess"].mean(),
                         "welch_t_naive": (a_["excess"].mean() - b_["excess"].mean()) / se if se and se > 0 else np.nan,
                         "amount_weighted_mean_excess_selected": float(np.average(a_["excess"], weights=a_["w"])) if len(a_) else np.nan,
                         "amount_weighted_mean_excess_eligible_not_selected": float(np.average(b_["excess"], weights=b_["w"])) if len(b_) else np.nan,
                         "median_excess_selected": a_["excess"].median(),
                         "median_excess_eligible_not_selected": b_["excess"].median()})
    pers_df = pd.DataFrame(pers)
    pers_df["note"] = ("completed hold_6m trades only (open_at_end excluded); in-sample rows use uncut exits and are "
                       "descriptive, never used for selection; naive Welch t ignores overlap/correlation between trades "
                       "and overstates significance")
    pers_df.to_csv(OUT / "persistence.csv", index=False)

    # ---- decisions of the chosen filter (audit trail) ----------------------------
    dch = dec[cfilter].merge(sig[["row_id", "ticker", "transaction_date", "filing_date", "entry_date"]], on="row_id")
    dch.to_csv(OUT / "decisions_chosen.csv", index=False)

    # ---- recommendation.json -----------------------------------------------------
    keys = ["n_trades", "n_trades_active_in_window", "n_skips", "period_start", "period_end", "total_return", "cagr",
            "volatility", "sharpe", "max_drawdown", "hit_rate", "share_beat_spy", "mean_excess_vs_spy",
            "mean_excess_vs_spy_completed", "spy_total_return", "spy_cagr", "spy_volatility", "spy_sharpe",
            "spy_max_drawdown", "alpha_annual", "alpha_t_nw", "beta", "information_ratio", "tracking_error",
            "sharpe_diff_memmel_z"]
    clean = lambda dct: {k: fnum(dct.get(k)) for k in keys if k in dct}

    def sigrow(name, prefix):
        s = [s for s in sig_rows if s["config_name"] == name and s["measure"].startswith(prefix)][0]
        return {k: fnum(v) for k, v in s.items()}

    comp_names = list(configs)
    rec = {
        "question": "Does copying ONLY House members with a strong past track record beat SPY?",
        "pre_registration": {"file": "src/backtest/ms_config.py", "sha256": cfg_sha,
                             "selection_criterion": mc.SELECTION_TEXT,
                             "note": "No version control; the hash only detects later edits. The OOS period had been "
                                     "seen in v1 and in earlier exploratory work (pseudo-OOS, v1 F8)."},
        "n_combinations_tested": n_tested,
        "chosen": {"W": chosen["W"], "K": cK, "selection": crule, "exit_rule": cexit, "weighting": cwt,
                   "X_pct": chosen["X"], "k_shrink": mc.K_SHRINK, "config": ckey},
        "selection_sample_metrics": clean(chosen.to_dict()),
        "selection_sharpe_minus_spy_sharpe": fnum(chosen["sharpe"] - chosen["spy_sharpe"]),
        "PRIMARY_oos_continuous_book": clean(g(PRIMARY)),
        "PRIMARY_oos_significance": sigrow("chosen_member_selection", "PRIMARY"),
        "SECONDARY_oos_signals_only_book": clean(results["chosen_member_selection"]["SECONDARY_oos_signals_only_book"]["row"]),
        "comparisons_PRIMARY_oos": {n: {**clean(results[n]["PRIMARY_continuous_book_2022-01-03_to_end"]["row"]),
                                        **{k: fnum(v) for k, v in sigrow(n, "PRIMARY").items()
                                           if k in ("alpha_annual", "alpha_t_nw", "beta", "information_ratio",
                                                    "sharpe_diff_memmel_z")}}
                                    for n in comp_names},
        "comparisons_selection_sample": {n: clean(results[n]["selection_sample_in_sample_cut2021"]["row"]) for n in comp_names},
        "multiple_testing": mt,
        "concentration": conc_df.to_dict("records"),
        "robustness": rob_df.to_dict("records"),
        "persistence_per_trade": pers_df.drop(columns="note").to_dict("records"),
        "self_check_in_sample_decisions_unchanged_without_post2021_info": oos_check,
        "data_counts": {
            "v1_eligible_signals": int(len(el)),
            "v1_eligible_in_sample": int((el["sample"] == "in_sample").sum()),
            "v1_eligible_oos": int((el["sample"] == "out_of_sample").sum()),
            "completed_track_record_trades_hold_6m": int(len(tr)),
            "track_record_delisted_exits_delayed": int((tr["exit_reason"] == "delisted").sum()),
            "members": len(codes),
            "selected_signals_chosen_filter": len(chosen_ids),
            "selected_signals_chosen_filter_in_sample": int(dec[cfilter].query("selected and sample == 'in_sample'").shape[0]),
            "selected_signals_chosen_filter_oos": int(dec[cfilter].query("selected and sample == 'out_of_sample'").shape[0]),
        },
        "notes": [
            "Same v1 rules: entry at the open of the first trading day after filing; skip only on a rise > X; "
            "0.1 % per side; non-discretionary, duplicate and missing-reference buys excluded; F6 gaps as v1.",
            "Track record = all v1-eligible signals of the member (regardless of X and of whether they were selected), "
            "hold_6m reference exit, result known strictly before the entry day; delisted exits count only "
            f"{mc.DELISTED_KNOWN_LAG} trading days after the last close.",
            "rf = 0; SPY adjustment=all treated as total return; pseudo-OOS (see v1 F8).",
        ],
        "runtime_seconds": round(time.time() - t0, 1),
    }
    with open(OUT / "recommendation.json", "w") as f:
        json.dump(json_clean(rec), f, indent=2)
    log(f"done in {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
