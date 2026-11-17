"""Laver figurerne til reports/backtest_v1.md og tallene til hjemmesiden (reports/site_data.json).

Kør fra projektmappen:  python3 reports/make_figures.py

Læser kun results/ (ændrer intet dér). Skriver:
    reports/figures/equity_curve.png    (a) kontinuerlig bog, valgt konfiguration mod SPY
    reports/figures/return_vs_x.png     (b) CAGR og Sharpe som funktion af X (hold_6m, beløbsvægtet)
    reports/figures/yearly_excess.png   (c) merafkast pr. kalenderår
    reports/site_data.json              tal og serier til hjemmesiden
"""
from __future__ import annotations

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.ticker import FuncFormatter  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
RES = ROOT / "results"
OUT = ROOT / "reports"
FIG = OUT / "figures"

OOS_START = "2022-01-03"
CHOSEN = "exit=hold_6m;weighting=amount;X=10"
CHOSEN_NONE = "exit=hold_6m;weighting=amount;X=none"
EQUAL_NONE = "exit=hold_6m;weighting=equal;X=none"

# Farveblind-sikre (valideret referencepalet: blå/orange) + neutral grå til SPY.
BLUE, ORANGE, GREY, INK, MUTED, RULE = "#2a78d6", "#eb6834", "#6b6a66", "#1f1f1e", "#52514e", "#dedcd6"

plt.rcParams.update({
    "font.family": "DejaVu Sans", "font.size": 11, "axes.titlesize": 13, "axes.titleweight": "bold",
    "axes.labelsize": 11, "axes.edgecolor": RULE, "axes.labelcolor": MUTED, "xtick.color": MUTED,
    "ytick.color": MUTED, "axes.spines.top": False, "axes.spines.right": False, "axes.grid": True,
    "grid.color": "#ecebe7", "grid.linewidth": 0.8, "legend.frameon": False, "figure.dpi": 100,
    "savefig.dpi": 160, "savefig.bbox": "tight", "text.color": INK,
})

pct = FuncFormatter(lambda v, _: f"{v * 100:.0f} %".replace("-", "−"))


def dk(v: float, d: int = 1) -> str:
    return f"{v:.{d}f}".replace(".", ",").replace("-", "−")


def load():
    eq = pd.read_csv(RES / "equity_curves.csv", parse_dates=["date"])
    grid = pd.read_csv(RES / "grid_summary.csv", dtype={"X": str})
    yearly = pd.read_csv(RES / "yearly.csv")
    rec = json.loads((RES / "recommendation.json").read_text())
    sig = pd.read_csv(RES / "significance.csv")
    oos = pd.read_csv(RES / "oos_continuous.csv")
    return eq, grid, yearly, rec, sig, oos


def series(eq, sample, name):
    s = eq[(eq["sample"] == sample) & (eq["series"] == name)]
    return s.set_index("date")["equity"]


def fig_equity(eq):
    strat = series(eq, "all_continuous", "strategy[chosen]")
    spy = series(eq, "all_continuous", "SPY_same_days_as[chosen]")
    fig, (ax, ax2) = plt.subplots(2, 1, figsize=(10, 6.6), sharex=True,
                                  gridspec_kw={"height_ratios": [2.4, 1], "hspace": 0.12})
    oos0 = pd.Timestamp(OOS_START)
    for a in (ax, ax2):
        a.axvspan(oos0, strat.index.max(), color="#eef3fa", zorder=0, lw=0)
        a.axvline(oos0, color=MUTED, lw=1, ls=(0, (4, 3)))
    ax.plot(spy.index, spy.values, color=GREY, lw=1.8, label="S&P 500 (SPY), samme dage")
    ax.plot(strat.index, strat.values, color=BLUE, lw=2, label="Strategi: hold 6 mdr., beløbsvægtet, X = 10 %")
    ax.set_ylabel("Værdi af 1 kr. investeret")
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: dk(v, 0) if v == int(v) else dk(v, 1)))
    ax.set_title("Kontinuerlig bog: alle signaler fra 2016, konfiguration låst i 2021", loc="left")
    ymax = max(strat.max(), spy.max())
    ax.text(pd.Timestamp("2016-03-01"), ymax * 0.97, "In-sample (2016–2021)\nher blev konfigurationen valgt",
            va="top", fontsize=10, color=MUTED)
    ax.text(oos0 + pd.Timedelta(days=40), ymax * 0.97, "Out-of-sample fra 3. jan. 2022\n(primært mål)",
            va="top", fontsize=10, color=MUTED)
    ax.legend(loc="lower right")
    rel = strat / spy
    ax2.plot(rel.index, rel.values, color=BLUE, lw=1.6)
    ax2.axhline(1, color=GREY, lw=1)
    ax2.set_ylabel("Strategi / SPY")
    ax2.yaxis.set_major_formatter(FuncFormatter(lambda v, _: dk(v, 2)))
    ax2.xaxis.set_major_locator(mdates.YearLocator())
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    fig.savefig(FIG / "equity_curve.png")
    plt.close(fig)
    return strat, spy


def x_curve(grid, sample):
    g = grid[(grid["sample"] == sample) & (grid["exit_rule"] == "hold_6m") & (grid["weighting"] == "amount")]
    fin = g[g["X"] != "none"].assign(Xn=lambda d: d["X"].astype(float)).sort_values("Xn")
    none = g[g["X"] == "none"].iloc[0]
    return fin, none


def fig_x(grid):
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.6), gridspec_kw={"wspace": 0.28})
    specs = [("in_sample_cut2021", "In-sample 2016–2021 (valggrundlag)", BLUE, "o"),
             ("oos_continuous", "Out-of-sample 2022–2026 (kontinuerlig bog)", ORANGE, "s")]
    for ax, (metric, title, fmt) in zip(axes, [("cagr", "Årligt afkast (CAGR)", pct),
                                               ("sharpe", "Sharpe-ratio (rf = 0)", None)]):
        for sample, label, color, marker in specs:
            fin, none = x_curve(grid, sample)
            ax.plot(fin["Xn"], fin[metric], color=color, lw=2, marker=marker, ms=5, label=label)
            ax.plot([34], [none[metric]], color=color, marker=marker, ms=9, ls="none",
                    markeredgecolor="white", markeredgewidth=1.5)
            ax.axhline(none[f"spy_{metric}"], color=color, lw=1, ls=(0, (4, 3)), alpha=0.9)
        ax.axvline(10, color=MUTED, lw=1, ls=":")
        ax.axvline(32, color=RULE, lw=1)
        ax.set_xticks(list(range(0, 31, 4)) + [34])
        ax.set_xticklabels([str(x) for x in range(0, 31, 4)] + ["ingen"])
        ax.set_xlim(-1, 35.5)
        ax.set_title(title, loc="left")
        if fmt:
            ax.yaxis.set_major_formatter(fmt)
        else:
            ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: dk(v, 1)))
        ax.text(10.4, ax.get_ylim()[0] + 0.02 * (ax.get_ylim()[1] - ax.get_ylim()[0]), "valgt X = 10",
                fontsize=9, color=MUTED, va="bottom")
    h, lab = axes[0].get_legend_handles_labels()
    fig.legend(h, lab, loc="upper center", ncol=2, bbox_to_anchor=(0.5, 1.07), fontsize=10)
    fig.supxlabel("Skip-grænse X (%): købet springes over, hvis kursen er steget mere end X fra handelsdag til indgang",
                  fontsize=10.5, color=MUTED, y=-0.01)
    fig.text(0.01, -0.07, "Stiplede linjer: SPY i samme periode (samme farve som perioden). "
             "Store punkter yderst til højre: X = ingen (intet skip). Exit: hold 6 mdr., beløbsvægtet.",
             fontsize=9.5, color=MUTED)
    fig.savefig(FIG / "return_vs_x.png")
    plt.close(fig)


def fig_yearly(yearly):
    y = yearly[yearly["config"] == CHOSEN].sort_values("year")
    fig, ax = plt.subplots(figsize=(10, 4.2))
    colors = [BLUE if p == "in_sample_years" else ORANGE for p in y["period"]]
    bars = ax.bar(y["year"].astype(str), y["excess"], color=colors, width=0.62, zorder=2)
    ax.axhline(0, color=GREY, lw=1, zorder=3)
    for b, v in zip(bars, y["excess"]):
        ax.text(b.get_x() + b.get_width() / 2, v + (0.008 if v >= 0 else -0.008),
                f"{dk(v * 100)}", ha="center", va="bottom" if v >= 0 else "top", fontsize=9, color=MUTED)
    ax.yaxis.set_major_formatter(pct)
    ax.set_ylabel("Strategi minus SPY")
    ax.set_title("Merafkast pr. kalenderår (kontinuerlig bog, hold 6 mdr., beløbsvægtet, X = 10 %)", loc="left")
    from matplotlib.patches import Patch
    ax.legend(handles=[Patch(color=BLUE, label="In-sample-år"), Patch(color=ORANGE, label="Out-of-sample-år")],
              loc="upper right")
    lo, hi = ax.get_ylim()
    ax.set_ylim(lo - 0.02, hi + 0.01)
    ax.set_xlabel("2026 går til 16. november 2026", fontsize=9, color=MUTED, loc="right")
    fig.savefig(FIG / "yearly_excess.png")
    plt.close(fig)


def metrics(d: dict, keys=("total_return", "cagr", "volatility", "sharpe", "max_drawdown")):
    return {k: d[k] for k in keys}


def site_data(eq, grid, yearly, rec, sig, oos, strat, spy):
    def sigrow(cfg, measure):
        r = sig[(sig["config"] == cfg) & (sig["measure"] == measure)].iloc[0]
        return {k: float(r[k]) for k in ["alpha_annual", "alpha_t_nw", "beta", "information_ratio",
                                           "tracking_error", "sharpe_diff_memmel_z"]}

    prim = rec["PRIMARY_oos_continuous_book"]
    sel = rec["selection_sample_metrics"]
    eqn = rec["equal_weight_X_none"]
    full = pd.DataFrame({"strategy": strat, "spy": spy,
                         "equal_x_none": series(eq, "all_continuous", "strategy[equal_X=none]")})
    # Sidste handelsdag i hver uge, mærket med den faktiske dato (ingen fremtidige datoer)
    weekly = full.groupby(full.index.to_period("W-FRI")).tail(1)

    def xs(sample):
        fin, none = x_curve(grid, sample)
        pts = [{"X": str(int(r.Xn)), "cagr": r.cagr, "sharpe": r.sharpe, "total_return": r.total_return,
                "n_trades": int(r.n_trades)} for r in fin.itertuples()]
        pts.append({"X": "none", "cagr": none["cagr"], "sharpe": none["sharpe"],
                    "total_return": none["total_return"], "n_trades": int(none["n_trades"])})
        return {"points": pts, "spy_cagr": none["spy_cagr"], "spy_sharpe": none["spy_sharpe"],
                "spy_total_return": none["spy_total_return"]}

    yc = yearly[yearly["config"] == CHOSEN].sort_values("year")
    data = {
        "_kilde": "Alle tal direkte fra results/ (recommendation.json, grid_summary.csv, yearly.csv, "
                  "significance.csv, equity_curves.csv). Genereret af reports/make_figures.py.",
        "chosen_config": {"exit_rule": "hold_6m", "weighting": "amount", "X_pct": 10,
                          "beskrivelse": "Hold 6 måneder, beløbsvægtet, spring køb over hvis kursen er steget mere end 10 %"},
        "periods": {"in_sample": [sel["period_start"], sel["period_end"]],
                    "oos": [prim["period_start"], prim["period_end"]], "oos_start": OOS_START},
        "headline": {
            "in_sample_selection": {"strategy": metrics(sel),
                                    "spy": {k.replace("spy_", ""): sel[k] for k in
                                            ["spy_total_return", "spy_cagr", "spy_volatility", "spy_sharpe",
                                             "spy_max_drawdown"]},
                                    "significance": sigrow(CHOSEN, "selection_sample_in_sample_cut2021"),
                                    "n_trades": sel["n_trades"]},
            "oos_continuous_primary": {"strategy": metrics(prim),
                                       "spy": {k.replace("spy_", ""): prim[k] for k in
                                               ["spy_total_return", "spy_cagr", "spy_volatility", "spy_sharpe",
                                                "spy_max_drawdown"]},
                                       "significance": sigrow(CHOSEN, "PRIMARY_continuous_book_2022-01-03_to_end"),
                                       "n_trades_active_in_window": prim["n_trades_active_in_window"]},
            "equal_x_none": {"in_sample_selection": metrics(eqn["selection"]),
                             "oos_continuous_primary": metrics(eqn["PRIMARY_oos_continuous"]),
                             "oos_significance": sigrow(EQUAL_NONE, "PRIMARY_continuous_book_2022-01-03_to_end")},
            "oos_secondary_signals_only_LABEL": "Sekundært mål: kun signaler fra 2022, starter tomt (forvrænget, review F1)",
            "oos_secondary_signals_only": metrics(rec["SECONDARY_oos_signals_only_book"]),
        },
        "yearly": [{"year": int(r.year), "strategy": r.strategy_return, "spy": r.spy_return, "excess": r.excess,
                    "period": "in_sample" if r.period == "in_sample_years" else "oos"} for r in yc.itertuples()],
        "return_vs_x": {"exit_rule": "hold_6m", "weighting": "amount",
                        "in_sample": xs("in_sample_cut2021"), "oos_continuous": xs("oos_continuous")},
        "equity_weekly": {"note": "Kontinuerlig bog (alle signaler fra 2016), værdi af 1 ved start 2016-01-05, "
                                  "sidste handelsdag i hver uge.", "oos_start": OOS_START,
                          "dates": [d.strftime("%Y-%m-%d") for d in weekly.index],
                          "strategy": [round(v, 6) for v in weekly["strategy"]],
                          "spy": [round(v, 6) for v in weekly["spy"]],
                          "equal_x_none": [round(v, 6) for v in weekly["equal_x_none"]]},
    }
    (OUT / "site_data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1))


def main():
    FIG.mkdir(parents=True, exist_ok=True)
    eq, grid, yearly, rec, sig, oos = load()
    strat, spy = fig_equity(eq)
    fig_x(grid)
    fig_yearly(yearly)
    site_data(eq, grid, yearly, rec, sig, oos, strat, spy)
    print("Skrevet:", *sorted(p.name for p in FIG.glob("*.png")), "site_data.json")


if __name__ == "__main__":
    main()
