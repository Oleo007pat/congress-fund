"""Regenerate every backtest output:   python3 -m src.backtest.run

Round 2 (after review_round1.md).  Writes to results/:
  trades_backtest.csv, grid_summary.csv, equity_curves.csv, recommendation.json,
  oos_continuous.csv, yearly.csv, significance.csv, concentration.csv,
  selection_robustness.csv, sensitivities.csv, skip_dividend_flip.csv,
  excluded_nondiscretionary.csv, member_name_map.csv, ticker_change_candidates.csv,
  gap_exits.csv
(results/ASSUMPTIONS.md is maintained by hand.)

Samples:
  in_sample_cut2021    in-sample signals, every position force-closed at the 2021 close
                       -> the ONLY sample the selection sees (pre-registered criterion)
  in_sample_full_hold  in-sample signals held to their real exits (contains post-2021
                       prices; NOT used for selection)
  oos_continuous       PRIMARY OOS: one continuous book of ALL signals (2016->), the
                       daily returns evaluated from the first trading day of 2022 to the end
  oos_signals_only     SECONDARY OOS: book of 2022+ signals only, starting empty
"""
from __future__ import annotations

import json
import sys
import time

import numpy as np
import pandas as pd

from . import config
from .analysis import significance, yearly_table
from .engine import (add_canonical_member, amount_weight, bench_window_return, build_member_map, cut_exit,
                     exit_fixed_hold, exit_member_sells, find_entry, find_reference, first_sale_exit_day,
                     load_trades, sample_of, select_buy_signals, select_sales, skip_mask, trade_returns)
from .portfolio import (bench_daily_returns, portfolio_daily_returns, select_configuration, series_metrics,
                        x_label)
from .prices import load_pricebook

SEL = config.SELECTION_SAMPLE
PRIMARY = "oos_continuous"
SECONDARY = "oos_signals_only"
FULL_IS = "in_sample_full_hold"
GRID_SAMPLES = [SEL, FULL_IS, PRIMARY, SECONDARY]
NO_PRICE_REASONS = ("no_symbol_mapping", "no_price_data", "data_starts_after_entry", "data_ended_before_entry",
                    "entry_in_data_gap", "no_trade_on_entry_day", "entry_after_end_of_data")
DUP_REASONS = ("exact_duplicate_row", "cross_doc_duplicate")


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def fnum(v):
    return None if isinstance(v, float) and not np.isfinite(v) else v


# ----------------------------------------------------------------------------
def build_signal_table(trades: pd.DataFrame, tmap: pd.DataFrame, pb) -> pd.DataFrame:
    sig = select_buy_signals(trades)          # R1 + duplicates + non-discretionary (round 2)
    sales = select_sales(trades)              # canonical member names (round 2)
    sales_by_key = {k: g for k, g in sales.groupby(["member_canon", "ticker"], sort=False)}
    sym_of = dict(zip(tmap["filing_ticker"], tmap["price_symbol"]))
    bcol = pb.col[config.BENCH_SYMBOL]
    cut_idx = pb.last_day_on_or_before(config.IS_END)
    jump15 = pb.jump_cum(config.JUMP15_UP, config.JUMP15_DOWN)
    artefacts = {}
    for sym, dt in config.KNOWN_ARTEFACTS:
        if sym in pb.col:
            artefacts.setdefault(pb.col[sym], []).append(pb.first_day_on_or_after(dt))

    rows = []
    for r in sig.itertuples(index=False):
        flag = r.amount_flag if isinstance(r.amount_flag, str) else ""
        d = {"row_id": r.row_id, "year": r.year, "doc_id": r.doc_id, "member": r.member,
             "member_canon": r.member_canon, "owner": r.owner, "ticker": r.ticker, "asset_type": r.asset_type,
             "filing_status": r.filing_status, "transaction_date": r.transaction_date, "filing_date": r.filing_date,
             "sample": sample_of(r.filing_date), "amount_low": r.amount_low, "amount_high": r.amount_high,
             "amount_flag": flag, "weight_equal": 1.0, "weight_amount": amount_weight(r.amount_low, r.amount_high, flag),
             "nondiscretionary_category": r.nondiscretionary_category, "drop_reason": r.drop_reason}
        sym = sym_of.get(r.ticker, "")
        sym = sym if isinstance(sym, str) else ""
        d["price_symbol"] = sym
        if d["drop_reason"]:  # short / duplicate / non-discretionary
            rows.append(d)
            continue
        col = pb.col.get(sym) if sym else None
        ent = find_entry(pb, col, r.filing_date)                       # R2 / R6
        if ent.drop_reason:
            d["drop_reason"] = ent.drop_reason
            rows.append(d)
            continue
        e = ent.idx
        d.update(entry_idx=e, entry_date=pb.date(e), entry_open=ent.price, col=col,
                 entry_piece=int(pb.seg[e, col]), symbol_n_pieces=int(pb.n_segments[col]))
        ref = find_reference(pb, col, r.transaction_date, e)          # R3
        d["ref_status"] = ref.status
        if ref.status != "ok":
            # F2: no usable reference -> removed from EVERY configuration (incl. X=none)
            d["drop_reason"] = "ref_" + ref.status
            d["rise_pct"] = np.nan
            rows.append(d)
            continue
        d.update(ref_date=pb.date(ref.idx), ref_price=ref.price, rise_pct=100.0 * (ent.price / ref.price - 1.0))
        sale_day, sale_filing = first_sale_exit_day(pb, sales_by_key.get((r.member_canon, r.ticker)),
                                                    r.transaction_date)
        d["first_sale_filing_date"] = sale_filing
        exits = {"member_sells": exit_member_sells(pb, col, e, sale_day, sale_filing)}
        for rule, m in config.HOLD_MONTHS.items():
            exits[rule] = exit_fixed_hold(pb, col, e, m)
        for rule, ex in exits.items():
            _store_exit(d, rule, ex, pb, col, bcol, e, jump15, artefacts)
            if d["sample"] == "in_sample" and e <= cut_idx:
                _store_exit(d, "cut_" + rule, cut_exit(pb, col, ex, cut_idx), pb, col, bcol, e, jump15, artefacts)
        rows.append(d)
    return pd.DataFrame(rows)


def _store_exit(d, rule, ex, pb, col, bcol, e, jump15, artefacts):
    g, n = trade_returns(d["entry_open"], ex.price)                    # R9
    d[f"{rule}_exit_idx"] = ex.idx
    d[f"{rule}_exit_date"] = pb.date(ex.idx)
    d[f"{rule}_exit_price_type"] = ex.kind
    d[f"{rule}_exit_price"] = ex.price
    d[f"{rule}_exit_reason"] = ex.reason
    d[f"{rule}_planned_exit"] = ex.planned
    # F6 audit: trading days the exit came after the planned day because of a gap
    d[f"{rule}_exit_delay_tdays"] = (ex.idx - ex.planned_idx
                                     if ex.planned_idx >= 0 and ex.reason in ("horizon", "member_sale") else 0)
    d[f"{rule}_gross_ret"] = g
    d[f"{rule}_net_ret"] = n
    d[f"{rule}_spy_ret"] = bench_window_return(pb, bcol, e, ex)        # R10
    d[f"{rule}_extreme_move_flag"] = bool(pb.extreme_cum[ex.idx, col] - pb.extreme_cum[e, col] > 0)
    d[f"{rule}_jump15_flag"] = bool(jump15[ex.idx, col] - jump15[e, col] > 0)
    d[f"{rule}_known_artefact_flag"] = any(e < a <= ex.idx for a in artefacts.get(col, []))


# ----------------------------------------------------------------------------
class Evaluator:
    def __init__(self, sig: pd.DataFrame, pb):
        self.pb = pb
        self.sig = sig
        used = sorted(sig.loc[sig["entry_idx"].notna(), "col"].astype(int).unique())
        self.compact = {c: k for k, c in enumerate(used)}
        self.Cz = np.nan_to_num(pb.close_ff[:, used], nan=0.0)
        b = pb.col[config.BENCH_SYMBOL]
        self.b_open, self.b_close = pb.open[:, b], pb.close[:, b]
        self.oos_start = pb.first_day_on_or_after(config.OOS_START)

    def population(self, sample):
        if sample in (SEL, FULL_IS):
            return self.sig[self.sig["sample"] == "in_sample"]
        if sample == SECONDARY:
            return self.sig[self.sig["sample"] == "out_of_sample"]
        return self.sig  # oos_continuous / all_continuous

    def trades(self, sample, rule, x, extra_mask=None):
        """Executed trades of a configuration (after the skip rule) and the column prefix."""
        pop = self.population(sample)
        elig = pop[pop["drop_reason"] == ""]
        prule = rule
        if sample == SEL:
            elig = elig[elig[f"cut_{rule}_exit_idx"].notna()]
            prule = f"cut_{rule}"
        skip = skip_mask(elig["rise_pct"].to_numpy(float) / 100.0, x)   # R3/R4 skip rule
        tk = elig[~skip]
        if extra_mask is not None:
            tk = tk[extra_mask(tk, prule)]
        return pop, elig, skip, tk, prule

    def evaluate(self, sample, rule, weighting, x, extra_mask=None, window_start=None, modify=None):
        pop, elig, skip, tk, prule = self.trades(sample, rule, x, extra_mask)
        dr = pop["drop_reason"]
        row = {"sample": sample, "exit_rule": rule, "weighting": weighting, "X": x_label(x),
               "n_buy_rows": len(pop),
               "n_excluded_short_related": int((dr == "short_related_excluded").sum()),
               "n_excluded_duplicates": int(dr.isin(DUP_REASONS).sum()),
               "n_excluded_nondiscretionary": int(dr.str.startswith("nondiscretionary_").sum()),
               "n_signals": int((~dr.isin(("short_related_excluded",) + DUP_REASONS)
                                 & ~dr.str.startswith("nondiscretionary_")).sum()),
               "n_dropped_no_price": int(dr.isin(NO_PRICE_REASONS).sum()),
               "n_dropped_ref_missing": int(dr.str.startswith("ref_").sum()),
               "n_entry_after_2021_cut": int(((dr == "") & pop[f"cut_{rule}_exit_idx"].isna()).sum()) if sample == SEL else 0,
               "n_skips": int(skip.sum()), "n_trades": len(tk)}
        if len(tk) == 0:
            return dict(row=row)
        if modify is not None:
            tk = modify(tk.copy(), prule)
        col = tk["col"].astype(int).map(self.compact).to_numpy()
        e = tk["entry_idx"].astype(int).to_numpy()
        xi = tk[f"{prule}_exit_idx"].astype(int).to_numpy()
        w = tk[f"weight_{weighting}"].to_numpy(float)
        first, last, r = portfolio_daily_returns(self.Cz, col, e, xi, tk["entry_open"].to_numpy(float),
                                                 tk[f"{prule}_exit_price"].to_numpy(float), w)
        dates = self.pb.cal[first:last + 1]
        br = bench_daily_returns(self.b_open, self.b_close, first, last)
        ws = window_start
        if sample == PRIMARY and ws is None:
            ws = self.oos_start
        if ws is not None:  # evaluate the continuous book only on days >= ws
            k = max(ws - first, 0)
            dates, r, br = dates[k:], r[k:], br[k:]
            active = tk[xi >= ws]
            row["n_trades_active_in_window"] = len(active)
        # per-trade statistics: in the continuous OOS book over OOS-filed trades only
        pt = tk[tk["sample"] == "out_of_sample"] if sample == PRIMARY else tk
        row["per_trade_universe"] = "oos_filed_trades" if sample == PRIMARY else "all_trades_of_sample"
        net = pt[f"{prule}_net_ret"].to_numpy(float)
        spy = pt[f"{prule}_spy_ret"].to_numpy(float)
        reason = pt[f"{prule}_exit_reason"]
        done = (reason != "open_at_end").to_numpy()
        row.update({
            "period_start": str(dates[0]), "period_end": str(dates[-1]),
            **series_metrics(dates, r),
            "hit_rate": float(np.mean(net > 0)), "share_beat_spy": float(np.mean(net > spy)),
            "mean_trade_return": float(net.mean()), "median_trade_return": float(np.median(net)),
            "mean_excess_vs_spy": float((net - spy).mean()), "median_excess_vs_spy": float(np.median(net - spy)),
            "n_completed_trades": int(done.sum()),
            "mean_excess_vs_spy_completed": float((net - spy)[done].mean()) if done.any() else np.nan,
            "hit_rate_completed": float(np.mean(net[done] > 0)) if done.any() else np.nan,
            "share_beat_spy_completed": float(np.mean(net[done] > spy[done])) if done.any() else np.nan,
            "mean_spy_window_return": float(spy.mean()),
            "n_delisted_exit": int((tk[f"{prule}_exit_reason"] == "delisted").sum()),
            "n_open_at_end": int((tk[f"{prule}_exit_reason"] == "open_at_end").sum()),
            "n_cut_2021_marked": int((tk[f"{prule}_exit_reason"] == "cut_2021").sum()),
            "n_exit_after_gap": int((tk[f"{prule}_exit_delay_tdays"] > 0).sum()),
            "n_member_sale_exit": int(tk[f"{prule}_exit_reason"].isin(["member_sale", "member_sale_at_entry"]).sum()),
            "n_member_sale_at_entry": int((tk[f"{prule}_exit_reason"] == "member_sale_at_entry").sum()),
            "n_extreme_move_flag": int(tk[f"{prule}_extreme_move_flag"].astype(bool).sum()),
            **series_metrics(dates, br, prefix="spy_"),
        })
        return dict(row=row, dates=dates, r=r, br=br, tk=tk, prule=prule)


# ----------------------------------------------------------------------------
def top_member(tk, weighting):
    w = tk.groupby("member_canon")[f"weight_{weighting}"].sum().sort_values(ascending=False)
    return w.index[0], float(w.iloc[0] / w.sum())


def top1pct_ids(tk, weighting="amount"):
    k = max(1, int(round(0.01 * len(tk))))
    return set(tk.nlargest(k, f"weight_{weighting}")["row_id"])


def grid_rows(ev, sample, **kw):
    return [ev.evaluate(sample, r, w, x, **kw)["row"]
            for r in config.EXIT_RULES for w in config.WEIGHTINGS for x in config.X_GRID]


def ticker_change_candidates(pb, sig):
    """F6: pieces that truly ended (delisted exits) whose last 20 daily returns are
    reproduced by another loaded symbol that keeps trading -> probable ticker change
    (e.g. FB -> META).  Listing only; positions are still exited at the last close."""
    rules = config.EXIT_RULES
    dl = []
    for rule in rules:
        x = sig[sig[f"{rule}_exit_reason"] == "delisted"]
        dl.append(pd.DataFrame({"col": x["col"].astype(int), "end_idx": x[f"{rule}_exit_idx"].astype(int), "rule": rule}))
    dl = pd.concat(dl)
    counts = dl.groupby(["col", "end_idx", "rule"]).size().unstack(fill_value=0)
    with np.errstate(invalid="ignore", divide="ignore"):
        R = pb.close[1:] / pb.close[:-1] - 1  # NaN where a bar is invalid
    out = []
    for (col, end), cnt in counts.iterrows():
        lo = max(0, end - 21)
        rj = R[lo:end, col]
        ok = np.isfinite(rj)
        # flat (pinned, e.g. pending cash deal) series would "match" cash-like ETFs:
        # require real price movement before calling it the same security
        if ok.sum() < 10 or np.nanstd(rj[ok]) < 0.005:
            succ = ""
        else:
            win = R[lo:end][ok]
            full_cols = np.isfinite(win).all(axis=0)
            diff = np.abs(np.where(np.isfinite(win), win, 0.0) - rj[ok][:, None])
            cand = np.flatnonzero(full_cols & (diff.max(axis=0) < 0.002))
            cand = [c for c in cand if c != col and pb.valid[min(end + 1, len(pb.cal) - 1):min(end + 6, len(pb.cal)), c].any()]
            succ = ";".join(pb.symbols[c] for c in cand)
        out.append({"symbol": pb.symbols[col], "piece_end_date": pb.date(end), "last_close": pb.close[end, col],
                    "data_resumes_later_as_new_piece": bool(pb.next_valid[min(end + 1, len(pb.cal) - 1), col] < len(pb.cal)
                                                            and end + 1 < len(pb.cal)),
                    "probable_successor_same_returns": succ,
                    **{f"n_delisted_{k}": int(cnt.get(k, 0)) for k in rules}})
    return pd.DataFrame(out).sort_values(["probable_successor_same_returns", "symbol"], ascending=[False, True])


# ----------------------------------------------------------------------------
def main():
    t0 = time.time()
    R = config.RESULTS
    R.mkdir(exist_ok=True)
    log("loading trades and ticker map")
    trades = add_canonical_member(load_trades())
    build_member_map(trades).query("n_spellings_for_key > 1").to_csv(R / "member_name_map.csv", index=False)
    tmap = pd.read_csv(config.TICKER_MAP_CSV, keep_default_na=False, na_values=[])
    syms = set(tmap.loc[tmap["price_symbol"] != "", "price_symbol"])
    log(f"loading prices for {len(syms)} symbols")
    pb = load_pricebook(syms)
    bcol = pb.col[config.BENCH_SYMBOL]
    if not pb.valid[:, bcol].all():
        raise RuntimeError("benchmark has invalid bars - essential input broken")
    log("building per-signal paths")
    sig = build_signal_table(trades, tmap, pb)
    assert (sig["sample"] == "before_sample").sum() == 0
    sig.sort_values("row_id").drop(columns=["col"]).to_csv(R / "trades_backtest.csv", index=False)
    nd = sig[sig["drop_reason"].str.startswith("nondiscretionary_")]
    tr_idx = trades.set_index("row_id")
    nd_out = nd[["row_id", "sample", "member", "ticker", "transaction_date", "filing_date", "owner", "amount_low",
                 "amount_high", "nondiscretionary_category"]].copy()
    nd_out["description"] = nd_out["row_id"].map(tr_idx["description"])
    nd_out["comments"] = nd_out["row_id"].map(tr_idx["comments"])
    nd_out.to_csv(R / "excluded_nondiscretionary.csv", index=False)
    log(f"signals: {len(sig)}  eligible+priced: {int((sig['drop_reason'] == '').sum())}")

    ev = Evaluator(sig, pb)
    log("running grid (4 samples x 136)")
    grid = pd.DataFrame([row for s in GRID_SAMPLES for row in grid_rows(ev, s)])
    grid.to_csv(R / "grid_summary.csv", index=False)

    # ---- R11: pre-registered selection on in_sample_cut2021 ONLY -------------------
    sel_rows = grid[grid["sample"] == SEL].copy()
    chosen = select_configuration(sel_rows)
    rule, wt, xl = chosen["exit_rule"], chosen["weighting"], chosen["X"]
    x_star = None if xl == "none" else float(xl)
    log(f"selected: exit={rule} weighting={wt} X={xl}")

    def g(sample, xlab, r=rule, w=wt):
        q = grid[(grid["sample"] == sample) & (grid["exit_rule"] == r) & (grid["weighting"] == w) & (grid["X"] == xlab)]
        return q.iloc[0].to_dict()

    # configurations reported in detail
    configs = {"chosen": (rule, wt, x_star), "equal_X=none": (rule, "equal", None)}
    if (rule, wt, x_star) != (rule, wt, None):
        configs["chosen_exit_weight_X=none"] = (rule, wt, None)

    # ---- F1/F4: continuous OOS, yearly, significance, concentration ---------------
    log("continuous book, yearly table, significance, concentration")
    oos_rows, yr_rows, sig_rows, conc_rows, eq_parts = [], [], [], [], []
    for name, (cr, cw, cx) in configs.items():
        cfg = f"exit={cr};weighting={cw};X={x_label(cx)}"
        full = ev.evaluate("all_continuous", cr, cw, cx)
        y = yearly_table(full["dates"], full["r"], full["br"])
        y.insert(0, "config", cfg)
        y.insert(0, "config_name", name)
        yr_rows.append(y)
        variants = {}
        prim = ev.evaluate(PRIMARY, cr, cw, cx)
        tm_oos, tm_share = top_member(prim["tk"][prim["tk"][f"{prim['prule']}_exit_idx"] >= ev.oos_start], cw)
        variants["PRIMARY_continuous_book_2022-01-03_to_end"] = prim
        variants["SECONDARY_oos_signals_only_book"] = ev.evaluate(SECONDARY, cr, cw, cx)
        variants["continuous_excl_2022_window_2023-01-03_to_end"] = ev.evaluate(
            PRIMARY, cr, cw, cx, window_start=pb.first_day_on_or_after("2023-01-01"))
        variants[f"continuous_excl_top_member({tm_oos})"] = ev.evaluate(
            PRIMARY, cr, cw, cx, extra_mask=lambda tk, pr, m=tm_oos: tk["member_canon"] != m)
        variants["selection_sample_in_sample_cut2021"] = ev.evaluate(SEL, cr, cw, cx)
        for vname, res in variants.items():
            rw = res["row"]
            oos_rows.append({"config_name": name, "config": cfg, "measure": vname,
                             **{k: rw.get(k) for k in ("period_start", "period_end", "n_trades", "n_trades_active_in_window",
                                                       "total_return", "cagr", "volatility", "sharpe", "max_drawdown",
                                                       "spy_total_return", "spy_cagr", "spy_volatility", "spy_sharpe",
                                                       "spy_max_drawdown", "mean_excess_vs_spy", "share_beat_spy",
                                                       "n_delisted_exit", "n_open_at_end")}})
            if vname.startswith(("PRIMARY", "SECONDARY", "selection")):
                sig_rows.append({"config_name": name, "config": cfg, "measure": vname,
                                 "period_start": rw["period_start"], "period_end": rw["period_end"],
                                 **significance(res["r"], res["br"])})
        # concentration (F1/F3)
        for uname, res in (("oos_continuous_active_in_window", prim), ("in_sample_cut2021", variants["selection_sample_in_sample_cut2021"])):
            tk = res["tk"]
            if uname.startswith("oos"):
                tk = tk[tk[f"{res['prule']}_exit_idx"] >= ev.oos_start]
            wcol = f"weight_{cw}"
            k = max(1, int(round(0.01 * len(tk))))
            mem, share = top_member(tk, cw)
            sample = PRIMARY if uname.startswith("oos") else SEL
            ids1 = set(tk.nlargest(k, "weight_amount")["row_id"])
            no_mem = ev.evaluate(sample, cr, cw, cx, extra_mask=lambda t, pr, m=mem: t["member_canon"] != m)["row"]
            no_top = ev.evaluate(sample, cr, cw, cx, extra_mask=lambda t, pr, s=ids1: ~t["row_id"].isin(s))["row"]
            base = res["row"]
            conc_rows.append({"config_name": name, "config": cfg, "universe": uname, "n_trades": len(tk),
                              "top1pct_n_trades": k,
                              "top1pct_weight_share": float(tk[wcol].nlargest(k).sum() / tk[wcol].sum()),
                              "top_member": mem, "top_member_weight_share": share,
                              "top_member_n_trades": int((tk["member_canon"] == mem).sum()),
                              "base_total_return": base["total_return"], "base_sharpe": base["sharpe"],
                              "excl_top_member_total_return": no_mem["total_return"], "excl_top_member_sharpe": no_mem["sharpe"],
                              "excl_top1pct_by_amount_total_return": no_top["total_return"],
                              "excl_top1pct_by_amount_sharpe": no_top["sharpe"],
                              "spy_total_return": base["spy_total_return"], "spy_sharpe": base["spy_sharpe"],
                              "note": ("equal weights: top 1% by weight is not meaningful; the excl_top1pct columns "
                                       "remove the top 1% by AMOUNT") if cw == "equal" else ""})
        # equity curves
        for s_name in ("all_continuous", PRIMARY, SECONDARY, SEL, FULL_IS):
            res = full if s_name == "all_continuous" else (prim if s_name == PRIMARY else ev.evaluate(s_name, cr, cw, cx))
            eq_parts.append(pd.DataFrame({"sample": s_name, "series": f"strategy[{name}]", "config": cfg,
                                          "date": res["dates"].astype(str), "equity": np.cumprod(1 + res["r"])}))
            eq_parts.append(pd.DataFrame({"sample": s_name, "series": f"SPY_same_days_as[{name}]", "config": cfg,
                                          "date": res["dates"].astype(str), "equity": np.cumprod(1 + res["br"])}))
    pd.DataFrame(oos_rows).to_csv(R / "oos_continuous.csv", index=False)
    pd.concat(yr_rows).to_csv(R / "yearly.csv", index=False)
    pd.DataFrame(sig_rows).to_csv(R / "significance.csv", index=False)
    pd.DataFrame(conc_rows).to_csv(R / "concentration.csv", index=False)
    pd.concat(eq_parts, ignore_index=True).to_csv(R / "equity_curves.csv", index=False)

    # ---- F3: selection robustness (same criterion, in-sample only) ----------------
    log("selection robustness")
    sel_tk = ev.trades(SEL, "hold_12m", None)[3]          # all eligible in-sample cut trades
    is_top_member, is_top_share = top_member(sel_tk, "amount")
    is_top1 = top1pct_ids(sel_tk, "amount")
    rob_specs = {
        "pre-registered (baseline)": {},
        f"excl in-sample top member by amount ({is_top_member})": dict(
            extra_mask=lambda tk, pr: tk["member_canon"] != is_top_member),
        "excl in-sample top 1% trades by amount": dict(extra_mask=lambda tk, pr: ~tk["row_id"].isin(is_top1)),
        "12-month warm-up (selection Sharpe from 2017-01-03)": dict(window_start=pb.first_day_on_or_after("2017-01-01")),
    }
    rob_rows = []
    for vname, kw in rob_specs.items():
        rows = sel_rows if not kw else pd.DataFrame(grid_rows(ev, SEL, **kw))
        c = select_configuration(rows)
        cx = None if c["X"] == "none" else float(c["X"])
        o_kw = {"extra_mask": kw["extra_mask"]} if "extra_mask" in kw else {}
        o = ev.evaluate(PRIMARY, c["exit_rule"], c["weighting"], cx, **o_kw)["row"]
        rows_sorted = rows[rows["n_trades"] >= config.SELECT_MIN_TRADES].sort_values("sharpe", ascending=False)
        rob_rows.append({"variant": vname, "chosen_exit": c["exit_rule"], "chosen_weighting": c["weighting"],
                         "chosen_X": c["X"], "selection_sharpe": c["sharpe"], "selection_spy_sharpe": c["spy_sharpe"],
                         "runner_up": "/".join(map(str, rows_sorted.iloc[1][["exit_rule", "weighting", "X"]])),
                         "runner_up_sharpe": rows_sorted.iloc[1]["sharpe"],
                         "same_as_baseline": (c["exit_rule"], c["weighting"], c["X"]) == (rule, wt, xl),
                         "oos_continuous_total_return": o["total_return"], "oos_continuous_sharpe": o["sharpe"],
                         "oos_spy_total_return": o["spy_total_return"], "oos_spy_sharpe": o["spy_sharpe"],
                         "note": "same exclusion applied to the OOS book" if o_kw else ""})
    rob = pd.DataFrame(rob_rows)
    rob.to_csv(R / "selection_robustness.csv", index=False)

    # ---- F5/F6 and other sensitivities on the chosen + equal/none configs ---------
    log("sensitivities")
    def zero_delisted(tk, pr):
        m = tk[f"{pr}_exit_reason"] == "delisted"
        tk.loc[m, f"{pr}_exit_price"] = 1e-12
        return tk
    sens_specs = {
        "baseline": {},
        "excl trades with jump >+100%/<-50% in window": dict(extra_mask=lambda tk, pr: ~tk[f"{pr}_extreme_move_flag"].astype(bool)),
        "excl trades with jump >+15%/<-13% in window (catches DD 2019-06-03)": dict(
            extra_mask=lambda tk, pr: ~tk[f"{pr}_jump15_flag"].astype(bool)),
        "excl trades across known artefact dates (DOW,AZN,NVS,DLPH,DDR,SRC,RTX,DD)": dict(
            extra_mask=lambda tk, pr: ~tk[f"{pr}_known_artefact_flag"].astype(bool)),
        "excl entries in a later price piece (possible reused ticker)": dict(extra_mask=lambda tk, pr: tk["entry_piece"] == 0),
        "stress: every delisted exit valued at -100%": dict(modify=zero_delisted),
    }
    sens_rows = []
    for name, (cr, cw, cx) in configs.items():
        for s in (SEL, PRIMARY):
            for vname, kw in sens_specs.items():
                rw = ev.evaluate(s, cr, cw, cx, **kw)["row"]
                sens_rows.append({"config_name": name, "config": f"exit={cr};weighting={cw};X={x_label(cx)}", "sample": s,
                                  "variant": vname, **{k: rw.get(k) for k in ("n_trades", "total_return", "cagr", "sharpe",
                                                                              "max_drawdown", "spy_total_return", "spy_sharpe",
                                                                              "mean_excess_vs_spy")}})
    pd.DataFrame(sens_rows).to_csv(R / "sensitivities.csv", index=False)

    # ---- F7: skip decisions that a dividend <= 2% between ref and entry could flip --
    flip = []
    elig = sig[sig["drop_reason"] == ""]
    for s, gg in elig.groupby("sample"):
        rise = gg["rise_pct"].to_numpy(float)
        for x in config.X_GRID:
            if x is None:
                continue
            hi = ((1 + x / 100) * (1 + config.DIV_FLIP_MAX) - 1) * 100
            hi05 = ((1 + x / 100) * 1.005 - 1) * 100
            sk = rise > x + 1e-7
            flip.append({"sample": s, "X": x_label(x), "n_signals": len(gg), "n_skipped": int(sk.sum()),
                         "n_skips_that_could_flip_to_take_div_2pct": int((sk & (rise <= hi)).sum()),
                         "n_skips_that_could_flip_to_take_div_0.5pct": int((sk & (rise <= hi05)).sum()),
                         "rise_band_pct_div_2pct": f"({x:g}, {hi:.3f}]",
                         "is_chosen_X": x_label(x) == xl})
    pd.DataFrame(flip).to_csv(R / "skip_dividend_flip.csv", index=False)

    # ---- F6: gap exits and probable ticker changes ---------------------------------
    gx = []
    for rr in config.EXIT_RULES:
        m = (sig[f"{rr}_exit_delay_tdays"].fillna(0) > 0)
        x = sig[m]
        gx.append(pd.DataFrame({"rule": rr, "row_id": x["row_id"], "sample": x["sample"], "price_symbol": x["price_symbol"],
                                "planned_exit": x[f"{rr}_planned_exit"], "exit_date": x[f"{rr}_exit_date"],
                                "delay_tdays": x[f"{rr}_exit_delay_tdays"].astype(int)}))
    gap_exits = pd.concat(gx)
    gap_exits.to_csv(R / "gap_exits.csv", index=False)
    tcc = ticker_change_candidates(pb, sig)
    tcc.to_csv(R / "ticker_change_candidates.csv", index=False)
    known_check = {f"{s} {d}": (None if s not in pb.col else
                                float(pb.close_ff[pb.first_day_on_or_after(d), pb.col[s]] /
                                      pb.close_ff[pb.first_day_on_or_after(d) - 1, pb.col[s]] - 1))
                   for s, d in config.KNOWN_ARTEFACTS}

    # ---- recommendation.json -------------------------------------------------------
    keys = ["n_signals", "n_dropped_no_price", "n_dropped_ref_missing", "n_skips", "n_trades", "n_trades_active_in_window",
            "n_delisted_exit", "n_open_at_end", "n_cut_2021_marked", "n_exit_after_gap", "period_start", "period_end",
            "total_return", "cagr", "volatility", "sharpe", "max_drawdown", "hit_rate", "share_beat_spy",
            "mean_trade_return", "median_trade_return", "mean_excess_vs_spy", "mean_excess_vs_spy_completed",
            "per_trade_universe", "spy_total_return", "spy_cagr", "spy_volatility", "spy_sharpe", "spy_max_drawdown"]
    clean = lambda dct: {k: fnum(dct.get(k)) for k in keys if k in dct}
    xi = [x_label(x) for x in config.X_GRID]
    k = xi.index(xl)
    neigh = sorted(set([xi[j] for j in (k - 1, k, k + 1) if 0 <= j < len(xi) - 1] + ["none"]), key=xi.index)
    sigdf = pd.DataFrame(sig_rows)
    rec = {
        "selection_criterion": config.SELECTION_TEXT,
        "selection_sample": SEL,
        "chosen": {"exit_rule": rule, "weighting": wt, "X_pct": xl},
        "selection_sample_metrics": clean(g(SEL, xl)),
        "selection_sharpe_minus_spy_sharpe": fnum(g(SEL, xl)["sharpe"] - g(SEL, xl)["spy_sharpe"]),
        "in_sample_edge_statement": ("In-sample shows NO meaningful edge over SPY: the chosen configuration's Sharpe "
                                     "equals SPY's to within %.4f and its alpha is not significant (see significance.csv)."
                                     % abs(g(SEL, xl)["sharpe"] - g(SEL, xl)["spy_sharpe"])),
        "n_selection_configs_with_sharpe_above_spy": int((sel_rows["sharpe"] > sel_rows["spy_sharpe"]).sum()),
        "in_sample_full_hold_metrics__contains_post2021_prices_not_used_for_selection": clean(g(FULL_IS, xl)),
        "PRIMARY_oos_continuous_book": clean(g(PRIMARY, xl)),
        "PRIMARY_oos_significance": {k_: fnum(v) for k_, v in sigdf[(sigdf.config_name == "chosen") & sigdf.measure.str.startswith("PRIMARY")].iloc[0].items()},
        "SECONDARY_oos_signals_only_book": clean(g(SECONDARY, xl)),
        "equal_weight_X_none": {"selection": clean(g(SEL, "none", w="equal")), "PRIMARY_oos_continuous": clean(g(PRIMARY, "none", w="equal")),
                                "SECONDARY_oos_signals_only": clean(g(SECONDARY, "none", w="equal"))},
        "robustness_neighbouring_X": {s: {n_: {m: fnum(g(s, n_)[m]) for m in ("sharpe", "cagr", "n_trades")} for n_ in neigh}
                                      for s in (SEL, PRIMARY)},
        "best_X_per_exit_and_weighting_in_selection_sample": (
            sel_rows[sel_rows["n_trades"] >= config.SELECT_MIN_TRADES].sort_values("sharpe", ascending=False)
            .groupby(["exit_rule", "weighting"]).head(1)[["exit_rule", "weighting", "X", "sharpe", "spy_sharpe", "n_trades"]]
            .to_dict("records")),
        "selection_robustness": rob.to_dict("records"),
        "notes": [
            "PRIMARY OOS = one continuous book of all signals since 2016, configuration fixed in-sample, returns evaluated "
            "2022-01-03..2026-11-16 (no information leaks: the configuration is fixed before 2022).",
            "SECONDARY OOS = book of 2022+ signals only, starting empty on 2022-01-03 (warm-up artefact, see review F1).",
            "pseudo-OOS: the 2021-2026 period had been examined in earlier exploratory backtests (README, backtest/) before this "
            "pre-registration; the pre-registration cannot be externally verified (see ASSUMPTIONS F8).",
            "rf = 0 for all Sharpe ratios (no T-bill series in the inputs).",
            "SPY adjustment=all treated as total return (approximation).",
            "Skip-rule rise is a total-return rise on dividend-adjusted prices (no unadjusted prices available).",
            "Delisted exits at the last close are optimistic for bankruptcies (e.g. SIVB 2023-03-09).",
        ],
        "data_counts": {
            "buy_rows_include_v1": int(len(sig)),
            "drop_reasons": {k_: int(v) for k_, v in sig["drop_reason"].replace("", "kept").value_counts().items()},
            "drop_reasons_by_sample": {s: {k_: int(v) for k_, v in gg["drop_reason"].replace("", "kept").value_counts().items()}
                                       for s, gg in sig.groupby("sample")},
            "nondiscretionary_by_category": {k_: int(v) for k_, v in nd["nondiscretionary_category"].value_counts().items()},
            "member_spellings_merged": int(build_member_map(trades).query("n_spellings_for_key > 1")["member"].nunique()),
            "members_with_several_spellings": int(build_member_map(trades).query("n_spellings_for_key > 1")["key"].nunique()),
            "entries_in_later_price_piece": int((sig["entry_piece"].fillna(0) > 0).sum()),
            "symbols_split_into_pieces": int((pb.n_segments > 1).sum()),
            "long_gaps_kept_within_piece": int(pb.n_long_gaps_within_piece.sum()),
            "exits_after_gap_by_rule": {rr: int((sig[f"{rr}_exit_delay_tdays"].fillna(0) > 0).sum()) for rr in config.EXIT_RULES},
            "delisted_exits_by_rule": {rr: int((sig[f"{rr}_exit_reason"] == "delisted").sum()) for rr in config.EXIT_RULES},
            "delisted_exits_below_25pct_of_entry_by_rule": {rr: int(((sig[f"{rr}_exit_reason"] == "delisted")
                                                                     & (sig[f"{rr}_exit_price"] < 0.25 * sig["entry_open"])).sum())
                                                            for rr in config.EXIT_RULES},
            "probable_ticker_changes_treated_as_delisting": tcc.loc[tcc["probable_successor_same_returns"] != "",
                                                                     ["symbol", "piece_end_date", "probable_successor_same_returns"]].to_dict("records"),
            "known_artefact_one_day_moves_in_data": known_check,
            "amount_weight_share_of_top_1pct_eligible_signals": {
                smp: float(gg["weight_amount"].nlargest(max(1, len(gg) // 100)).sum() / gg["weight_amount"].sum())
                for smp, gg in elig.groupby("sample")},
            "in_sample_top_member_by_amount": {"member": is_top_member, "weight_share": is_top_share},
        },
    }
    with open(R / "recommendation.json", "w") as f:
        json.dump(rec, f, indent=2, default=lambda o: o.item() if hasattr(o, "item") else str(o))
    log(f"done in {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
