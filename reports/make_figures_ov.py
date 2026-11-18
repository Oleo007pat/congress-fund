"""Figur og hjemmesidedata for risiko-overlay-testen (reports/overlays_v1.md).

Kør fra projektmappen (efter python3 -m src.backtest.overlays):  python3 reports/make_figures_ov.py

Læser kun results/overlays/ (ændrer intet dér). Skriver:
    reports/figures/ov_equity.png   in-sample og out-of-sample: værdi og fald for v1 (= begge valg),
                                    de tre enkelt-overlays på v1, SPY og SPY + vol-targeting
    reports/site_data_ov.json       nøgletal, gitter, multiple testing, årstabel og ugentlige værdikurver
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
RES = ROOT / "results" / "overlays"
OUT = ROOT / "reports"
FIG = OUT / "figures"
SEL, PRIM = "in_sample_cut2021", "oos_continuous"

BLUE, ORANGE, GREY, INK, MUTED, RULE = "#2a78d6", "#eb6834", "#6b6a66", "#1f1f1e", "#52514e", "#dedcd6"
TEAL, PURPLE = "#1a9e8f", "#8a5cc2"
plt.rcParams.update({
    "font.family": "DejaVu Sans", "font.size": 11, "axes.titlesize": 13, "axes.titleweight": "bold",
    "axes.labelsize": 11, "axes.edgecolor": RULE, "axes.labelcolor": MUTED, "xtick.color": MUTED,
    "ytick.color": MUTED, "axes.spines.top": False, "axes.spines.right": False, "axes.grid": True,
    "grid.color": "#ecebe7", "grid.linewidth": 0.8, "legend.frameon": False, "figure.dpi": 100,
    "savefig.dpi": 160, "savefig.bbox": "tight", "text.color": INK,
})
pct = FuncFormatter(lambda v, _: f"{v * 100:.0f} %".replace("-", "−"))

V1 = "weighting=amount;cap=none;vol_target=off"
LINES = [  # (series key in equity_curves.csv, label, colour, width, dash)
    ("SPY", "S&P 500 (SPY)", GREY, 1.8, "-"),
    ("SPY;vol_target=on", "SPY + vol-targeting (samme regel)", GREY, 1.5, (0, (4, 2))),
    ("weighting=amount;cap=position;vol_target=off", "v1 + loft 5 % pr. position (ikke valgt)", BLUE, 1.4, "-"),
    ("weighting=amount;cap=member;vol_target=off", "v1 + loft 15 % pr. medlem (ikke valgt)", TEAL, 1.4, "-"),
    ("weighting=amount;cap=none;vol_target=on", "v1 + vol-targeting 15 % (ikke valgt)", PURPLE, 1.4, "-"),
    (V1, "v1 = begge forudregistrerede valg", ORANGE, 2.4, "-"),
]


def dk(v, d=1):
    return f"{v:.{d}f}".replace(".", ",").replace("-", "−")


def main():
    eq = pd.read_csv(RES / "equity_curves.csv", parse_dates=["date"])
    rec = json.loads((RES / "recommendation.json").read_text())
    grid = pd.read_csv(RES / "grid_summary.csv")
    yearly = pd.read_csv(RES / "yearly.csv")
    panels = {s: eq[eq["sample"] == s].pivot(index="date", columns="series", values="equity") for s in (SEL, PRIM)}

    fig, axes = plt.subplots(2, 2, figsize=(13, 7.6), sharex="col",
                             gridspec_kw={"height_ratios": [2.2, 1], "hspace": 0.12, "wspace": 0.12})
    titles = {SEL: "In-sample 2016–2021 (valget træffes her)", PRIM: "Out-of-sample 2022-01-03 til 2026-11-16 (pseudo)"}
    for j, s in enumerate((SEL, PRIM)):
        c = panels[s]
        ax, axd = axes[0, j], axes[1, j]
        for key, lab, colr, lw, ls in LINES:
            y = c[key]
            ax.plot(y.index, y.values, color=colr, lw=lw, ls=ls, label=lab)
            axd.plot(y.index, (y / y.cummax() - 1).values, color=colr, lw=lw * 0.8, ls=ls)
        ax.set_yscale("log")
        ax.yaxis.set_major_locator(FixedLocator([0.8, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0]))
        ax.yaxis.set_minor_locator(NullLocator())
        ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: dk(v, 2) if v in (0.8, 1.25) else dk(v, 1)))
        ax.set_title(titles[s], loc="left")
        axd.yaxis.set_major_formatter(pct)
        if j == 0:
            ax.set_ylabel("Værdi af 1 kr. (log-skala)")
            axd.set_ylabel("Fald fra top")
    axes[0, 0].legend(loc="upper left", fontsize=9)
    fig.suptitle("Risiko-overlays på v1: intet slår v1 in-sample; out-of-sample er forskellene støj (z ≤ 0,83)",
                 x=0.125, ha="left", fontsize=13, fontweight="bold", y=0.995)
    fig.savefig(FIG / "ov_equity.png")
    plt.close(fig)

    # ---- site data --------------------------------------------------------------
    keep = ("total_return", "cagr", "volatility", "sharpe", "max_drawdown", "alpha_annual", "alpha_t_nw", "beta",
            "information_ratio", "sharpe_diff_z_vs_spy", "sharpe_diff_z_vs_v1", "mean_exposure")

    def rows(sample):
        g = grid[grid["sample"] == sample]
        return [{"weighting": r["weighting"], "cap": r["cap"], "vol_target": r["vol_target"], "is_v1": bool(r["is_v1"]),
                 **{k: (None if pd.isna(r[k]) else float(r[k])) for k in keep}} for _, r in g.iterrows()]

    wk = {s: panels[s][[k for k, *_ in LINES]].resample("W-FRI").last().dropna() for s in (SEL, PRIM)}
    spy_vt = rec["SPY_plus_vol_targeting"]
    mt = rec["multiple_testing"]
    site = {
        "_kilde": "Alle tal direkte fra results/overlays/ (recommendation.json, grid_summary.csv, yearly.csv, "
                  "equity_curves.csv). Genereret af reports/make_figures_ov.py.",
        "spoergsmaal": rec["question"],
        "konklusion": "Begge forudregistrerede valg er v1 selv (beløbsvægt, intet loft, ingen vol-targeting). Ingen "
                      "overlay slår v1 in-sample. Out-of-sample (pseudo) har flere overlays højere Sharpe end v1, men "
                      "ingen forskel er signifikant, og vol-targeting gør det samme for SPY. Papirfonden bør ikke skifte.",
        "n_combinations_tested": rec["n_combinations_tested"],
        "picks": {"primary": rec["primary_pick"], "risk_reduction": rec["risk_reduction_pick"]["config"]},
        "periods": {"in_sample": [rec["in_sample_selection"]["v1"]["period_start"], rec["in_sample_selection"]["v1"]["period_end"]],
                    "oos": [rec["PRIMARY_oos_continuous"]["v1"]["period_start"], rec["PRIMARY_oos_continuous"]["v1"]["period_end"]]},
        "headline": {
            "v1_in_sample": rec["in_sample_selection"]["v1"],
            "v1_oos": rec["PRIMARY_oos_continuous"]["v1"],
            "spy_plus_vol_targeting": spy_vt,
            "single_overlays_on_v1_not_picked": rec["descriptive_single_overlays_on_v1_not_picked"],
        },
        "multiple_testing": {k: mt[k] for k in ("n_in_sample_sharpe_above_v1", "n_in_sample_sharpe_above_spy",
                                                "n_oos_sharpe_above_v1", "n_oos_sharpe_above_spy",
                                                "rank_correlation_is_vs_oos_sharpe_spearman")}
        | {"dsr_raw_N18": mt["deflated_sharpe_in_sample"]["raw_return_N=18"]["dsr"],
           "dsr_active_vs_spy_N18": mt["deflated_sharpe_in_sample"]["active_return_vs_SPY_N=18"]["dsr"],
           "ex_post_best_oos": {k: v for k, v in mt["ex_post_best_oos_vs_v1"].items()
                                if k in ("config", "oos_sharpe", "memmel_z_vs_v1", "memmel_z_vs_spy")}
           | {"dsr_active_vs_v1_N18": mt["ex_post_best_oos_vs_v1"].get("dsr_active_vs_v1_N=18", {}).get("dsr")}},
        "grid": {"in_sample": rows(SEL), "oos": rows(PRIM)},
        "yearly": [{"config_name": r["config_name"], "year": int(r["year"]), "return": float(r["strategy_return"]),
                    "spy": float(r["spy_return"]), "v1": float(r["v1_return"])}
                   for _, r in yearly[yearly["config_name"].isin(["v1", "SPY_plus_vol_targeting",
                                                                  "not_picked_v1+position_cap", "not_picked_v1+member_cap",
                                                                  "not_picked_v1+vol_target"])].iterrows()],
        "equity_weekly": {s: {"dates": [str(d.date()) for d in wk[s].index],
                              **{lab: [round(float(v), 5) for v in wk[s][key]] for key, lab, *_ in LINES}}
                          for s in (SEL, PRIM)},
    }
    (OUT / "site_data_ov.json").write_text(json.dumps(site, indent=1, ensure_ascii=False))
    print("wrote reports/figures/ov_equity.png and reports/site_data_ov.json")


if __name__ == "__main__":
    main()
