"""Figurer og hjemmesidedata for medlemsudvælgelses-testen (reports/member_selection_v1.md).

Kør fra projektmappen:  python3 reports/make_figures_ms.py

Læser kun results/member_selection/ (ændrer intet dér). Skriver:
    reports/figures/ms_equity_curve.png   valgt konfiguration mod SPY og v1, OOS-start markeret
    reports/figures/ms_yearly_excess.png  merafkast pr. kalenderår (valgt og v1)
    reports/site_data_ms.json             nøgletal, årligt merafkast og ugentlig værdikurve
"""
from __future__ import annotations

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.ticker import FixedLocator, FuncFormatter, NullLocator  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
RES = ROOT / "results" / "member_selection"
OUT = ROOT / "reports"
FIG = OUT / "figures"
OOS_START = "2022-01-03"
CH, V1 = "chosen_member_selection", "v1_chosen_all_members(hold_6m/amount/X=10)"

BLUE, ORANGE, GREY, INK, MUTED, RULE = "#2a78d6", "#eb6834", "#6b6a66", "#1f1f1e", "#52514e", "#dedcd6"
plt.rcParams.update({
    "font.family": "DejaVu Sans", "font.size": 11, "axes.titlesize": 13, "axes.titleweight": "bold",
    "axes.labelsize": 11, "axes.edgecolor": RULE, "axes.labelcolor": MUTED, "xtick.color": MUTED,
    "ytick.color": MUTED, "axes.spines.top": False, "axes.spines.right": False, "axes.grid": True,
    "grid.color": "#ecebe7", "grid.linewidth": 0.8, "legend.frameon": False, "figure.dpi": 100,
    "savefig.dpi": 160, "savefig.bbox": "tight", "text.color": INK,
})
pct = FuncFormatter(lambda v, _: f"{v * 100:.0f} %".replace("-", "−"))


def dk(v, d=1):
    return f"{v:.{d}f}".replace(".", ",").replace("-", "−")


def main():
    eq = pd.read_csv(RES / "equity_curves.csv", parse_dates=["date"])
    yearly = pd.read_csv(RES / "yearly.csv")
    rec = json.loads((RES / "recommendation.json").read_text())
    c = eq[eq["sample"] == "common_start_chosen_book"].pivot(index="date", columns="series", values="equity")
    strat, spy, v1 = c["strategy[chosen_member_selection]"], c["SPY_same_days"], c["v1_chosen_all_members"]
    ch = rec["chosen"]
    label = (f"Udvalgte medlemmer: top 20 %, W = {ch['W']}, K = {ch['K']}, hold 6 mdr., beløbsvægtet, X = ingen")

    # (a) equity curve
    fig, (ax, ax2) = plt.subplots(2, 1, figsize=(10, 6.8), sharex=True,
                                  gridspec_kw={"height_ratios": [2.4, 1], "hspace": 0.12})
    oos0 = pd.Timestamp(OOS_START)
    for a in (ax, ax2):
        a.axvspan(oos0, c.index.max(), color="#eef3fa", zorder=0, lw=0)
        a.axvline(oos0, color=MUTED, lw=1, ls=(0, (4, 3)))
    ax.plot(spy.index, spy.values, color=GREY, lw=1.8, label="S&P 500 (SPY), samme dage")
    ax.plot(v1.index, v1.values, color=ORANGE, lw=1.6, label="v1: alle medlemmer, hold 6 mdr., beløbsvægtet, X = 10 %")
    ax.plot(strat.index, strat.values, color=BLUE, lw=2, label=label)
    ax.set_ylabel("Værdi af 1 kr. (log-skala)")
    ax.set_yscale("log")
    ax.yaxis.set_major_locator(FixedLocator([1, 1.5, 2, 3, 4, 6, 8]))
    ax.yaxis.set_minor_locator(NullLocator())
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: dk(v, 0) if v == int(v) else dk(v, 1)))
    ax.set_title(f"Kontinuerlig bog fra {c.index.min():%d-%m-%Y}, konfiguration låst i 2021", loc="left")
    ax.text(oos0, ax.get_ylim()[1], "  out-of-sample fra 3/1-2022", va="top", ha="left", color=MUTED, fontsize=10)
    ax.legend(loc="upper left", fontsize=9.5)
    ax2.plot(strat.index, strat / spy, color=BLUE, lw=1.8, label="Udvalgte / SPY")
    ax2.plot(v1.index, v1 / spy, color=ORANGE, lw=1.4, label="v1 / SPY")
    ax2.axhline(1, color=GREY, lw=1)
    ax2.set_ylabel("Forhold til SPY")
    ax2.legend(loc="upper left", fontsize=9.5)
    fig.savefig(FIG / "ms_equity_curve.png")
    plt.close(fig)

    # (b) yearly excess
    yc = yearly[yearly["config_name"] == CH].set_index("year")
    yv = yearly[yearly["config_name"] == V1].set_index("year")
    yrs = yc.index.to_numpy()
    fig, ax = plt.subplots(figsize=(10, 4.8))
    ax.set_axisbelow(True)
    wdt = 0.38
    ax.bar(yrs - wdt / 2, yc["excess"], wdt, color=BLUE, label="Udvalgte medlemmer − SPY")
    ax.bar(yrs + wdt / 2, yv.loc[yrs, "excess"], wdt, color=ORANGE, label="v1 (alle medlemmer) − SPY")
    ax.axhline(0, color=GREY, lw=1)
    ax.axvline(2021.5, color=MUTED, lw=1, ls=(0, (4, 3)))
    ax.text(2021.55, ax.get_ylim()[1], " out-of-sample", va="top", color=MUTED, fontsize=10)
    ax.yaxis.set_major_formatter(pct)
    ax.set_xticks(yrs)
    ax.set_xticklabels([str(y) if y < 2026 else "2026*" for y in yrs])
    ax.set_ylabel("Merafkast i procentpoint")
    ax.set_title("Merafkast mod SPY pr. kalenderår (kontinuerlig bog)", loc="left")
    ax.legend(loc="upper right", fontsize=9.5)
    ax.text(0, -0.13, "* 2026 til 16/11. 2016 starter 7/9 for de udvalgte medlemmer (ingen historik før).",
            transform=ax.transAxes, fontsize=9, color=MUTED)
    fig.savefig(FIG / "ms_yearly_excess.png")
    plt.close(fig)

    # site data
    wk = c.resample("W-FRI").last().dropna()
    p = rec["comparisons_PRIMARY_oos"]
    keys = ("total_return", "cagr", "volatility", "sharpe", "max_drawdown", "alpha_annual", "alpha_t_nw", "beta",
            "information_ratio", "sharpe_diff_memmel_z")
    conc = {r["universe"]: r for r in rec["concentration"]}
    site = {
        "_kilde": "Alle tal direkte fra results/member_selection/ (recommendation.json, yearly.csv, equity_curves.csv). "
                  "Genereret af reports/make_figures_ms.py.",
        "chosen_config": {**ch, "beskrivelse": "Kopiér kun køb fra medlemmer i top 20 % efter krympet gennemsnitligt "
                                               "merafkast på deres afsluttede 6-måneders handler de seneste 24 måneder "
                                               "(mindst 5 handler); hold 6 mdr., beløbsvægtet, intet skip."},
        "n_combinations_tested": rec["n_combinations_tested"],
        "periods": {"in_sample": [rec["selection_sample_metrics"]["period_start"], "2021-12-31"],
                    "oos": [OOS_START, rec["PRIMARY_oos_continuous_book"]["period_end"]], "oos_start": OOS_START},
        "headline": {
            "in_sample_selection": {"strategy": {k: rec["selection_sample_metrics"].get(k) for k in keys},
                                    "spy_sharpe": rec["selection_sample_metrics"]["spy_sharpe"],
                                    "spy_total_return": rec["selection_sample_metrics"]["spy_total_return"]},
            "oos_continuous": {"strategy": {k: p[CH].get(k) for k in keys},
                               "v1": {k: p[V1].get(k) for k in keys},
                               "spy": {k: p[CH].get(f"spy_{k}") for k in ("total_return", "cagr", "volatility",
                                                                           "sharpe", "max_drawdown")}},
            "oos_excl_top_member": {"member": conc["oos_continuous_active_in_window"]["top_member"],
                                    "weight_share": conc["oos_continuous_active_in_window"]["top_member_weight_share"],
                                    "total_return": conc["oos_continuous_active_in_window"]["excl_top_member_total_return"],
                                    "sharpe": conc["oos_continuous_active_in_window"]["excl_top_member_sharpe"]},
            "multiple_testing": {k: rec["multiple_testing"][k] for k in (
                "n_in_sample_sharpe_above_spy", "n_oos_continuous_sharpe_above_spy",
                "chosen_in_sample_alpha_p_bonferroni_128")}
            | {"deflated_sharpe_active_N128": rec["multiple_testing"]["deflated_sharpe_in_sample"]["active_return_vs_SPY_N=128"]["dsr"]},
        },
        "yearly": [{"year": int(y), "strategy": float(yc.loc[y, "strategy_return"]), "spy": float(yc.loc[y, "spy_return"]),
                    "excess": float(yc.loc[y, "excess"]), "v1_excess": float(yv.loc[y, "excess"]),
                    "period": "in_sample" if y <= 2021 else "oos"} for y in yrs],
        "equity_weekly": {"note": f"Kontinuerlig bog, værdi af 1 ved start {c.index.min():%Y-%m-%d} (første dag med en "
                                  "udvalgt position); v1 og SPY over de samme dage; sidste handelsdag i hver uge.",
                          "oos_start": OOS_START, "dates": [d.strftime("%Y-%m-%d") for d in wk.index],
                          "strategy": [round(float(v), 5) for v in wk["strategy[chosen_member_selection]"]],
                          "spy": [round(float(v), 5) for v in wk["SPY_same_days"]],
                          "v1": [round(float(v), 5) for v in wk["v1_chosen_all_members"]]},
    }
    (OUT / "site_data_ms.json").write_text(json.dumps(site, indent=1, ensure_ascii=False))
    print("wrote", FIG / "ms_equity_curve.png", FIG / "ms_yearly_excess.png", OUT / "site_data_ms.json")


if __name__ == "__main__":
    main()
