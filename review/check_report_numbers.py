"""Kontrollerer, at nøgletallene i reports/backtest_v1.md, RESUME.md og reports/site_data.json
stemmer med result-filerne.

Kør fra projektmappen:  python3 review/check_report_numbers.py

For hvert tal beregnes den formaterede værdi (dansk decimalkomma) direkte fra results/ og
det kontrolleres, at netop den tekst står i rapporten. Tal, der kun findes i bias-reviewet eller
datakvalitetsrapporten, kontrolleres mod disse filer.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
RES = ROOT / "results"
REPORT = (ROOT / "reports" / "backtest_v1.md").read_text()
RESUME = (ROOT / "RESUME.md").read_text()
REVIEW2 = (ROOT / "review" / "review_round2.md").read_text()
DQ = (ROOT / "reports" / "data_quality_v1.md").read_text()

rec = json.loads((RES / "recommendation.json").read_text())
grid = pd.read_csv(RES / "grid_summary.csv", dtype={"X": str})
sig = pd.read_csv(RES / "significance.csv")
oos = pd.read_csv(RES / "oos_continuous.csv")
yearly = pd.read_csv(RES / "yearly.csv")
conc = pd.read_csv(RES / "concentration.csv")
selrob = pd.read_csv(RES / "selection_robustness.csv", dtype={"chosen_X": str})
sens = pd.read_csv(RES / "sensitivities.csv")
flip = pd.read_csv(RES / "skip_dividend_flip.csv")
gap = pd.read_csv(RES / "gap_exits.csv")
tick = pd.read_csv(RES / "ticker_change_candidates.csv")
site = json.loads((ROOT / "reports" / "site_data.json").read_text())

CH = "exit=hold_6m;weighting=amount;X=10"
CHN = "exit=hold_6m;weighting=amount;X=none"
EQN = "exit=hold_6m;weighting=equal;X=none"
PRIM = "PRIMARY_continuous_book_2022-01-03_to_end"
SEC = "SECONDARY_oos_signals_only_book"
SELM = "selection_sample_in_sample_cut2021"

fails: list[str] = []
n_ok = 0


def pct(v, d=1, sign=True):
    s = f"{v * 100:+.{d}f}" if sign else f"{v * 100:.{d}f}"
    return s.replace(".", ",").replace("-", "−") + " %"


def num(v, d=2, sign=False):
    s = f"{v:+.{d}f}" if sign else f"{v:.{d}f}"
    return s.replace(".", ",").replace("-", "−")


def thou(n):
    return f"{int(n):,}".replace(",", ".")


def check(text, where="rapport", doc=None):
    global n_ok
    doc = REPORT if doc is None else doc
    if text in doc:
        n_ok += 1
    else:
        fails.append(f"[{where}] mangler: {text!r}")


def eq(a, b, label, tol=1e-12):
    global n_ok
    if abs(float(a) - float(b)) <= tol:
        n_ok += 1
    else:
        fails.append(f"[site_data] {label}: {a} != {b}")


def g(sample, exit_rule, weighting, X):
    r = grid[(grid["sample"] == sample) & (grid.exit_rule == exit_rule) & (grid.weighting == weighting) & (grid.X == X)]
    assert len(r) == 1, (sample, exit_rule, weighting, X)
    return r.iloc[0]


def s(cfg, measure):
    return sig[(sig.config == cfg) & (sig.measure == measure)].iloc[0]


def o(cfg, measure):
    return oos[(oos.config == cfg) & (oos.measure == measure)].iloc[0]


# ---- Valg og kriterium -------------------------------------------------------------------
assert rec["chosen"] == {"exit_rule": "hold_6m", "weighting": "amount", "X_pct": "10"}
assert rec["selection_sample"] == "in_sample_cut2021"
assert rec["n_selection_configs_with_sharpe_above_spy"] == 2
assert len(grid[grid["sample"] == "in_sample_cut2021"]) == 136
cut = grid[grid["sample"] == "in_sample_cut2021"]
assert (cut.sharpe > cut.spy_sharpe).sum() == 2
assert cut.loc[cut.sharpe.idxmax(), ["exit_rule", "weighting", "X"]].tolist() == ["hold_6m", "amount", "10"]
check("kun 2 af 136 kombinationer lå over SPY")
check("Kun 2 af 136 kombinationer har en in-sample-Sharpe over SPY's")

# ---- Udvælgelsesperioden -----------------------------------------------------------------
sm = rec["selection_sample_metrics"]
check(f"Sharpe var {num(sm['sharpe'], 4)} mod SPY's {num(sm['spy_sharpe'], 4)}")
check(f"| 8.716 | **{pct(sm['total_return'])}** | {pct(sm['cagr'], sign=False)} | **{num(sm['sharpe'], 4)}** | {pct(sm['max_drawdown'], sign=False)} |")
check(f"| **{pct(sm['spy_total_return'])}** | {pct(sm['spy_cagr'], sign=False)} | **{num(sm['spy_sharpe'], 4)}** | {pct(sm['spy_max_drawdown'], sign=False)} |")
ss = s(CH, SELM)
check(f"+{num(ss.alpha_annual * 100, 1)} % om året, t = {num(ss.alpha_t_nw, 2)}")
d8 = rec["robustness_neighbouring_X"]["in_sample_cut2021"]["8"]["sharpe"]
diff = sm["sharpe"] - d8
assert round(diff, 4) == 0.0004, diff
check("X = 10 slår X = 8 med 0,0004")

# Tabel 1: alle rækker
rows1 = [("hold_6m", "amount", "10"), ("hold_6m", "amount", "8"), ("hold_6m", "amount", "6"),
         ("hold_6m", "amount", "none"), ("hold_3m", "amount", "30"), ("hold_12m", "amount", "6"),
         ("hold_12m", "equal", "10"), ("member_sells", "equal", "10"), ("hold_6m", "equal", "10"),
         ("hold_6m", "equal", "none"), ("hold_3m", "equal", "none"), ("member_sells", "amount", "10")]
for e, w, x in rows1:
    a, b = g("in_sample_cut2021", e, w, x), g("oos_continuous", e, w, x)
    sd = 4 if (e, w) == ("hold_6m", "amount") and x in ("10", "8", "6", "none") else 2
    tr = pct(a.total_return)
    tr = f"**{tr}**" if (e, w, x) == ("hold_6m", "amount", "10") else tr
    sh = f"**{num(a.sharpe, sd)}**" if (e, w, x) == ("hold_6m", "amount", "10") else num(a.sharpe, sd)
    check(f"| {thou(a.n_trades)} | {tr} | {pct(a.cagr, sign=False)} | {sh} | {pct(a.max_drawdown, sign=False)} | "
          f"{pct(b.total_return)} | {num(b.sharpe)} |", f"tabel1 {e}/{w}/{x}")
# best X pr. par stemmer med recommendation.json
best = {(r["exit_rule"], r["weighting"]): r["X"] for r in rec["best_X_per_exit_and_weighting_in_selection_sample"]}
for (e, w), x in best.items():
    assert (e, w, x) in rows1, (e, w, x)
oc = grid[grid["sample"] == "oos_continuous"]
n_oos_above = int((oc.sharpe > oc.spy_sharpe).sum())
check(f"Out-of-sample har {n_oos_above} af 136 kombinationer en højere Sharpe end SPY")
assert g("oos_continuous", "hold_6m", "amount", "10").sharpe < g("oos_continuous", "hold_6m", "amount", "10").spy_sharpe

# ---- Primært OOS --------------------------------------------------------------------------
p = rec["PRIMARY_oos_continuous_book"]
ps = s(CH, PRIM)
check(f"strategien {pct(p['total_return'])} mod SPY's {pct(p['spy_total_return'])}")
check(f"Sharpe-ratio på {num(p['sharpe'])} mod {num(p['spy_sharpe'])}")
check(f"årlig alfa på {num(ps.alpha_annual * 100, 1)} % (Newey-West t = {num(ps.alpha_t_nw)})")
check(f"Betaen var {num(ps.beta)}")
check(f"({pct(p['volatility'], sign=False)} mod {pct(p['spy_volatility'], sign=False)})")
check(f"({num(ps.n_days, 0).replace('1223', '1.223')} handelsdage)")
check(f"(t = {num(ps.beta_minus_1_t_nw)})")
cols = [(CH, "Valgt"), (CHN, "X=none"), (EQN, "lige/none")]
P = {c: o(c, PRIM) for c, _ in cols}
S = {c: s(c, PRIM) for c, _ in cols}
check("| Afkast i alt | " + " | ".join(pct(P[c].total_return) for c, _ in cols) + f" | {pct(P[CH].spy_total_return)} |")
check("| CAGR | " + " | ".join(pct(P[c].cagr, sign=False) for c, _ in cols) + f" | {pct(P[CH].spy_cagr, sign=False)} |")
check("| Volatilitet | " + " | ".join(pct(P[c].volatility, sign=False) for c, _ in cols) + f" | {pct(P[CH].spy_volatility, sign=False)} |")
check("| Sharpe (rf = 0) | " + " | ".join(num(P[c].sharpe) for c, _ in cols) + f" | {num(P[CH].spy_sharpe)} |")
check("| Maks. fald | " + " | ".join(pct(P[c].max_drawdown, sign=False) for c, _ in cols) + f" | {pct(P[CH].spy_max_drawdown, sign=False)} |")
check("| Alfa pr. år (NW-t) | " + " | ".join(f"{pct(S[c].alpha_annual, sign=False)} ({num(S[c].alpha_t_nw)})" for c, _ in cols) + " | |")
check("| Beta | " + " | ".join(num(S[c].beta) for c, _ in cols) + " | 1 |")
check("| Tracking error | " + " | ".join(pct(S[c].tracking_error, sign=False) for c, _ in cols) + " | |")
check("| Information ratio | " + " | ".join(num(S[c].information_ratio) for c, _ in cols) + " | |")
check("| Sharpe-forskel, Memmel z | " + " | ".join(num(S[c].sharpe_diff_memmel_z) for c, _ in cols) + " | |")
check(f"merafkast mod SPY i samme periode {pct(p['mean_excess_vs_spy'], sign=False)} ({pct(p['mean_excess_vs_spy_completed'], sign=False)} for afsluttede handler)")
check(f"{pct(p['share_beat_spy'], sign=False)} af handlerne slog SPY, og {pct(p['hit_rate'], sign=False)} gav et positivt afkast")
x22 = o(CH, "continuous_excl_2022_window_2023-01-03_to_end")
check(f"giver den valgte {pct(x22.total_return)} (Sharpe {num(x22.sharpe)}) mod SPY's {pct(x22.spy_total_return)} ({num(x22.spy_sharpe)})")
# Skip-reglen: X=none mod X=10 OOS
rn = rec["robustness_neighbouring_X"]["oos_continuous"]
assert rn["none"]["sharpe"] > rn["10"]["sharpe"]
check(f"X = ingen (aldrig springe over) en Sharpe på {num(rn['none']['sharpe'], 3)} mod {num(rn['10']['sharpe'], 3)} for X = 10")

# ---- Sekundært OOS ------------------------------------------------------------------------
sc, se = o(CH, SEC), o(EQN, SEC)
check(f"| Afkast i alt | {pct(sc.total_return)} | {pct(se.total_return)} | {pct(sc.spy_total_return)} |")
check(f"| Sharpe | {num(sc.sharpe)} | {num(se.sharpe)} | {num(sc.spy_sharpe)} |")
a1, a2 = s(CH, SEC), s(EQN, SEC)
check(f"| Alfa pr. år (NW-t) | {pct(a1.alpha_annual)} ({num(a1.alpha_t_nw)}) | {pct(a2.alpha_annual, sign=False)} ({num(a2.alpha_t_nw)}) | |")

# ---- Årstabel -----------------------------------------------------------------------------
yc = yearly[yearly.config == CH].sort_values("year")
for r in yc.itertuples():
    per = "in-sample" if r.period == "in_sample_years" else "out-of-sample"
    yl = "2026 (til 16/11)" if r.year == 2026 else str(r.year)
    check(f"| {yl} | {per} | {pct(r.strategy_return)} | {pct(r.spy_return)} | {num(r.excess * 100, 1, sign=True).replace('+', '+')} |",
          f"år {r.year}")
n_pos = int((yc.excess > 0).sum())
check(f"Strategien slog SPY i {n_pos} af {len(yc)} år")
# Produktet af OOS-årene = primært totalafkast
prod = (1 + yc[yc.period == "out_of_sample_years"].strategy_return).prod() - 1
assert abs(prod - p["total_return"]) < 1e-9, prod

# ---- Robusthed ----------------------------------------------------------------------------
for r in selrob.itertuples():
    check(f"| {pct(r.oos_continuous_total_return)} / {num(r.oos_continuous_sharpe)} |", f"robusthed {r.variant}")
    check(f"| {num(r.selection_sharpe, 3)}", f"robusthed sharpe {r.variant}")
check(f"{num(selrob.iloc[3].selection_spy_sharpe, 3)}")
assert selrob.iloc[1].chosen_exit == "hold_3m" and selrob.iloc[1].chosen_X == "30"
assert selrob.iloc[2].chosen_exit == "member_sells" and selrob.iloc[2].chosen_weighting == "equal"
for t in ["hold_12m/amount/X=6 (Sharpe 0.948 < SPY)", "+63.75 %, Sharpe 0.586", "from 0.930 to 1.015",
          "from +63.9 % / 0.559 to +61.9 % / 0.549", "189 of them", "The 262 excluded in-sample rows",
          "Ingen alvorlige fejl", "this cuts 18 trades short"]:
    check(t, "review runde 2", REVIEW2)
check("| hold 12 mdr. / beløb / 6 | 0,948 | +63,75 % / 0,586 |")
cc = conc[(conc.config == CH)].set_index("universe")
check(f"{pct(cc.loc['in_sample_cut2021'].top1pct_weight_share, sign=False)} af vægten in-sample og {pct(cc.loc['oos_continuous_active_in_window'].top1pct_weight_share, sign=False)} out-of-sample")
check(f"Suzan DelBene, {pct(cc.loc['in_sample_cut2021'].top_member_weight_share, sign=False)} af vægten** ({int(cc.loc['in_sample_cut2021'].top_member_n_trades)} handler")
check(f"fra {num(cc.loc['in_sample_cut2021'].base_sharpe, 3)} til {num(cc.loc['in_sample_cut2021'].excl_top_member_sharpe, 3)}")
check(f"Cleo Fields, {pct(cc.loc['oos_continuous_active_in_window'].top_member_weight_share, sign=False)}")
check(f"giver den kontinuerlige portefølje {pct(cc.loc['oos_continuous_active_in_window'].excl_top_member_total_return)} (Sharpe {num(cc.loc['oos_continuous_active_in_window'].excl_top_member_sharpe)})")

# ---- Følsomhed ----------------------------------------------------------------------------
sv = sens[sens.config == CH]
for variant in sv.variant.unique():
    i = sv[(sv.variant == variant) & (sv["sample"] == "in_sample_cut2021")].iloc[0]
    oo = sv[(sv.variant == variant) & (sv["sample"] == "oos_continuous")].iloc[0]
    check(f"| {num(i.sharpe, 3)} | {pct(oo.total_return)} | {num(oo.sharpe)} |", f"følsomhed {variant}")
j15 = sv[(sv["sample"] == "oos_continuous") & sv.variant.str.contains(r"\+15%")].iloc[0]
check(f"fjerner {thou(p['n_trades'] - j15.n_trades)} af {thou(p['n_trades'])} handler")

# ---- Afkast som funktion af X -------------------------------------------------------------
for x in ["0", "2", "4", "6", "8", "10", "12", "16", "20", "24", "30", "none"]:
    a, b = g("in_sample_cut2021", "hold_6m", "amount", x), g("oos_continuous", "hold_6m", "amount", x)
    sd = 3 if x in ("8", "10") else 2
    line = f"{thou(a.n_trades)} | {pct(a.cagr, sign=False)} | {num(a.sharpe, sd)} | {pct(b.cagr, sign=False)} | {num(b.sharpe)} |"
    if x == "10":
        line = f"**{thou(a.n_trades)}** | **{pct(a.cagr, sign=False)}** | **{num(a.sharpe, sd)}** | **{pct(b.cagr, sign=False)}** | **{num(b.sharpe)}** |"
    check(line, f"X-tabel {x}")
a = g("in_sample_cut2021", "hold_6m", "amount", "10")
check(f"| SPY | | {pct(a.spy_cagr, sign=False)} | {num(a.spy_sharpe)} | {pct(p['spy_cagr'], sign=False)} | {num(p['spy_sharpe'])} |")

# ---- Optælling ----------------------------------------------------------------------------
dc = rec["data_counts"]
dr = dc["drop_reasons"]
assert dc["buy_rows_include_v1"] == 20567 and sum(dr.values()) == 20567
check(f"| {thou(dc['buy_rows_include_v1'])} |")
check(f"| − short-relaterede | {dr['short_related_excluded']} |")
check(f"| {dr['exact_duplicate_row']} / {dr['cross_doc_duplicate']} |")
nd = dc["nondiscretionary_by_category"]
check(f"udbyttegeninvestering {nd['dividend_reinvestment']}, forvaltet af tredjemand {nd['third_party_managed']}, "
      f"overførsel/selskabshandling {nd['transfer_distribution_corporate_action']}, vesting/løn {nd['vesting_compensation']}, "
      f"automatisk plan {nd['automatic_plan']}) | {sum(nd.values())} |")
nop = ["no_symbol_mapping", "data_starts_after_entry", "data_ended_before_entry", "entry_in_data_gap", "no_trade_on_entry_day"]
check(f"ingen symbolkobling {dr['no_symbol_mapping']}, data starter senere {dr['data_starts_after_entry']}, data slutter før "
      f"{dr['data_ended_before_entry']}, hul {dr['entry_in_data_gap']}, ingen handel den dag {dr['no_trade_on_entry_day']}) | "
      f"{thou(sum(dr[k] for k in nop))} |")
nref = sum(v for k, v in dr.items() if k.startswith("ref_missing"))
check(f"(fjernet for alle X) | {nref} |")
ks = dc["drop_reasons_by_sample"]
check(f"| **{thou(dr['kept'])}** ({thou(ks['in_sample']['kept'])} / {thou(ks['out_of_sample']['kept'])}) |")
se_ = rec["SECONDARY_oos_signals_only_book"]
check(f"| {thou(sm['n_skips'])} / {thou(sm['n_trades'])} |")
check(f"| {thou(se_['n_skips'])} / {thou(se_['n_trades'])} |")
check(f"| {thou(p['n_trades'])} / {thou(p['n_trades_active_in_window'])} |")
check(f"| {sm['n_delisted_exit']} / {p['n_delisted_exit']} |")
check(f"vurderet til sidste lukkekurs) | {p['n_open_at_end']} |")
check(f"| {sm['n_cut_2021_marked']} |")
check(f"(hold 6 mdr.) | {int((gap.rule == 'hold_6m').sum())} |")
check(f"{thou(sum(dr[k] for k in nop))} køb uden kurs ved indgang")

# ---- Begrænsninger ------------------------------------------------------------------------
f10 = flip[(flip.X == 10)].set_index("sample")
check(f"op til {f10.loc['in_sample', 'n_skips_that_could_flip_to_take_div_2pct']} in-sample- og "
      f"{f10.loc['out_of_sample', 'n_skips_that_could_flip_to_take_div_2pct']} out-of-sample-skip (ud af "
      f"{thou(f10.loc['in_sample', 'n_skipped'])} og {thou(f10.loc['out_of_sample', 'n_skipped'])})")
stress = sv[(sv["sample"] == "oos_continuous") & sv.variant.str.startswith("stress")].iloc[0]
check(f"sænker out-of-sample til {pct(stress.total_return)} (Sharpe {num(stress.sharpe)})")
check(f"out-of-sample {pct(stress.total_return)}", "RESUME", RESUME)
n_ren = len(dc["probable_ticker_changes_treated_as_delisting"])
check(f"{n_ren} sandsynlige navneskift")
check(f"{n_ren} tickerskift", "RESUME", RESUME)
for t in ["7.004", "5.312", "75,8 %", "1.692", "24,2 %", "2016: 315, 2017: 295, 2018: 297", "Harold Rogers (129)",
          "Michael McCaul (127)", "Rohit Khanna (121)", "ca. 561 handler (heraf ca. 348 i v1)", "(324 rækker)",
          "2.848 af 3.418", "94,8 %", "51.791", "8.000 og 98.000"]:
    check(t, "datakvalitet", DQ)
check("1.692 af 7.004 indberetninger (24,2 %)")
check("8.000–98.000 handler")
check("SIVB, der lukkes til 106,04")
assert "SIVB positions exit at 106.04" in (RES / "ASSUMPTIONS.md").read_text()

# ---- RESUME -------------------------------------------------------------------------------
for t in [f"{pct(p['total_return'])} mod SPY's {pct(p['spy_total_return'])}", f"Sharpe var {num(p['sharpe'])} mod {num(p['spy_sharpe'])}",
          f"alfa {num(ps.alpha_annual * 100, 1)} % om året (t = {num(ps.alpha_t_nw)})", "7.004 PTR", "5.312 (75,8 %)",
          "51.791 handler", "1.692 scannede (24,2 %)", "2.848 af 3.418", "94,8 %",
          f"DelBene har {cc.loc['in_sample_cut2021'].top_member_weight_share * 100:.0f} %"]:
    check(t, "RESUME", RESUME)

# ---- site_data.json -----------------------------------------------------------------------
h = site["headline"]
for k in ["total_return", "cagr", "volatility", "sharpe", "max_drawdown"]:
    eq(h["oos_continuous_primary"]["strategy"][k], p[k], f"oos {k}")
    eq(h["oos_continuous_primary"]["spy"][k], p["spy_" + k], f"oos spy {k}")
    eq(h["in_sample_selection"]["strategy"][k], sm[k], f"is {k}")
    eq(h["equal_x_none"]["oos_continuous_primary"][k], rec["equal_weight_X_none"]["PRIMARY_oos_continuous"][k], f"eq {k}")
eq(h["oos_continuous_primary"]["significance"]["alpha_t_nw"], ps.alpha_t_nw, "alpha t")
for row in site["yearly"]:
    r = yc[yc.year == row["year"]].iloc[0]
    eq(row["excess"], r.excess, f"år {row['year']}")
for key, sample in [("in_sample", "in_sample_cut2021"), ("oos_continuous", "oos_continuous")]:
    for pt in site["return_vs_x"][key]["points"]:
        eq(pt["sharpe"], g(sample, "hold_6m", "amount", pt["X"]).sharpe, f"X {key} {pt['X']}")
ew = site["equity_weekly"]
eqc = pd.read_csv(RES / "equity_curves.csv")
ac = eqc[(eqc["sample"] == "all_continuous") & (eqc.series == "strategy[chosen]")].set_index("date").equity
eq(ew["strategy"][-1], round(ac.iloc[-1], 6), "equity slut", 1e-9)
assert ew["oos_start"] == "2022-01-03" and ew["dates"][-1] == "2026-11-16"
# Det kontinuerlige OOS-afkast kan genfindes fra den fulde kurve
eq(ac.iloc[-1] / ac[ac.index < "2022-01-03"].iloc[-1] - 1, p["total_return"], "kontinuerlig slice", 1e-9)

print(f"{n_ok} kontroller OK, {len(fails)} fejl")
for f in fails:
    print("  FEJL:", f)
sys.exit(1 if fails else 0)
