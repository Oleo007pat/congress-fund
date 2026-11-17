"""Build the public site.

Reads site/src/*, results/ and reports/site_data.json, and writes:
  docs/index.html, docs/research.html      (full HTML documents for GitHub Pages)
  site/preview/index.html                   (fragment for the Claude artifact preview)
  site/preview/research.html                (full document, served next to the preview)
Every number shown comes from results/ or reports/site_data.json.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "site" / "src"
DOCS = ROOT / "docs"
PREVIEW = ROOT / "site" / "preview"
SITE_URL = "https://oleo007pat.github.io/congress-fund/"

FONTS = ('<link rel="preconnect" href="https://fonts.googleapis.com">\n'
         '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>\n'
         '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wdth,wght@75..100,500..800'
         '&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap">')

MARK = ('<svg viewBox="0 0 28 28" aria-hidden="true"><rect class="mk-bg" x="1" y="1" width="26" height="26" rx="3"/>'
        '<path class="mk-roof" d="M6 11.5 14 6l8 5.5z"/>'
        '<path class="mk-col" d="M8.5 13.5v7.5M12.2 13.5v7.5M15.8 13.5v7.5M19.5 13.5v7.5M6 22.4h16"/></svg>')
MARK_CSS = (".mk-bg{fill:var(--ink)}.mk-roof{fill:var(--accent)}.mk-col{stroke:var(--paper);stroke-width:1.8;fill:none}"
            "footer .mk-bg{fill:var(--band-ink)}footer .mk-col{stroke:var(--band)}")
FAVICON = ("data:image/svg+xml," + "%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 28 28'%3E"
           "%3Crect x='1' y='1' width='26' height='26' rx='4' fill='%2314213A'/%3E"
           "%3Cpath d='M6 11.5 14 6l8 5.5z' fill='%23B7791F'/%3E"
           "%3Cpath d='M8.5 13.5v7.5M12.2 13.5v7.5M15.8 13.5v7.5M19.5 13.5v7.5M6 22.4h16' stroke='white' stroke-width='1.8'/%3E%3C/svg%3E")


def series(eq: pd.DataFrame, name: str) -> pd.Series:
    g = eq[(eq["sample"] == "all_continuous") & (eq["series"] == name)].sort_values("date")
    return pd.Series(g["equity"].values, index=pd.to_datetime(g["date"]))


def stats(v: pd.Series) -> dict:
    r = v.pct_change().dropna()
    yrs = (v.index[-1] - v.index[0]).days / 365.25
    vol = r.std(ddof=1) * np.sqrt(252)
    return {
        "total": float(v.iloc[-1] / v.iloc[0] - 1),
        "cagr": float((v.iloc[-1] / v.iloc[0]) ** (1 / yrs) - 1),
        "vol": float(vol),
        "sharpe": float(r.mean() * 252 / vol),
        "mdd": float((v / v.cummax() - 1).min()),
    }


def build_data() -> tuple[dict, list]:
    eq = pd.read_csv(ROOT / "results" / "equity_curves.csv")
    s = series(eq, "strategy[chosen]")
    m = series(eq, "SPY_same_days_as[chosen]")
    s_n, m_n = s / s.iloc[0], m / m.iloc[0]
    # last real trading day of each week (holidays never produce a missing label)
    idx = list(s_n.groupby(s_n.index.to_period("W-FRI")).apply(lambda g: g.index[-1]))
    sd = json.loads((ROOT / "reports" / "site_data.json").read_text())
    h = sd["headline"]["oos_continuous_primary"]
    sig = h["significance"]
    t_alpha = sig["alpha_annual"] * 100
    fmt = lambda x, d: f"{abs(x):.{d}f}".replace(".", ",")
    sign = lambda x: "+" if x > 0 else ("−" if x < 0 else "")
    data = {
        "dates": [d.strftime("%Y-%m-%d") for d in idx],
        "s": [round(float(s_n[d]), 4) for d in idx],
        "m": [round(float(m_n[d]), 4) for d in idx],
        "oos": sd["periods"]["oos_start"],
        "metrics": {
            "oos": {
                "fund": {"total": h["strategy"]["total_return"], "cagr": h["strategy"]["cagr"], "vol": h["strategy"]["volatility"],
                         "sharpe": h["strategy"]["sharpe"], "mdd": h["strategy"]["max_drawdown"]},
                "spy": {"total": h["spy"]["total_return"], "cagr": h["spy"]["cagr"], "vol": h["spy"]["volatility"],
                        "sharpe": h["spy"]["sharpe"], "mdd": h["spy"]["max_drawdown"]},
                "extra": f"{sign(t_alpha)}{fmt(t_alpha, 1)} % (t = {sign(sig['alpha_t_nw'])}{fmt(sig['alpha_t_nw'], 2)})",
                "note": "3. januar 2022 til 16. november 2026. Reglerne var låst inden perioden. Sharpe-ratio uden risikofri rente.",
            },
            "all": {
                "fund": stats(s), "spy": stats(m),
                "note": ("6. januar 2016 til 16. november 2026. Perioden indeholder 2016–2021, hvor reglerne blev valgt, "
                         "og tallene er derfor for optimistiske."),
            },
        },
    }
    return data, sd["yearly"]


def year_table(yearly: list) -> tuple[str, str]:
    f = lambda v: ("+" if v > 0 else "−" if v < 0 else "") + f"{abs(v) * 100:.1f}".replace(".", ",")
    head = "".join(f'<th class="r">{y["year"]}{"*" if y["year"] == 2026 else ""}</th>' for y in yearly)
    fund = "".join(f'<td class="r">{f(y["strategy"])}</td>' for y in yearly)
    spy = "".join(f'<td class="r">{f(y["spy"])}</td>' for y in yearly)
    diff = "".join(f'<td class="r {"pos" if y["excess"] > 0 else "neg"}">{f(y["excess"])}</td>' for y in yearly)
    rows = (f"<tr><th>Congress Fund, %</th>{fund}</tr><tr><th>S&amp;P 500, %</th>{spy}</tr>"
            f"<tr><th>Forskel, procentpoint</th>{diff}</tr>")
    return head, rows


def head(title: str, desc: str, path: str, css: str, extra: str = "") -> str:
    url = SITE_URL + ("" if path == "index.html" else path)
    return f"""<!doctype html>
<html lang="da">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>{title}</title>
<meta name="description" content="{desc}">
<meta property="og:type" content="website">
<meta property="og:title" content="{title}">
<meta property="og:description" content="{desc}">
<meta property="og:url" content="{url}">
<meta property="og:image" content="{SITE_URL}assets/og.png">
<meta name="twitter:card" content="summary_large_image">
<link rel="icon" href="{FAVICON}">
{FONTS}
<style>
:root{{color-scheme:light}}body{{margin:0}}img{{max-width:100%}}[hidden]{{display:none!important}}
{css}
</style>
{extra}</head>
<body>
"""


def main() -> None:
    DOCS.mkdir(exist_ok=True)
    PREVIEW.mkdir(parents=True, exist_ok=True)
    css = (SRC / "fund.css").read_text() + "\n" + MARK_CSS
    data, yearly = build_data()
    yh, yr = year_table(yearly)
    body = (SRC / "index.body.html").read_text()
    body = (body.replace("%%MARK%%", MARK).replace("%%YEAR_HEAD%%", yh).replace("%%YEAR_ROWS%%", yr)
                .replace("%%DATA%%", json.dumps(data, separators=(",", ":"))))
    assert "%%" not in body
    title = "Congress Fund"
    desc = ("En regelbaseret papirfond, der følger amerikanske kongresmedlemmers offentliggjorte aktiekøb. "
            "Ærlige resultater, åben kode og en live portefølje.")
    (DOCS / "index.html").write_text(head(title, desc, "index.html", css) + body + "\n</body>\n</html>\n")
    (PREVIEW / "index.html").write_text(
        f'<title>{title}</title>\n<meta name="claude:viewport-fit-sides" content="cover">\n{FONTS}\n<style>\n{css}\n</style>\n' + body)

    # Research page: the earlier project page, without its live section (that lives on the front page now).
    r = (ROOT / "site" / "index.html").read_text()
    r = re.sub(r'<section aria-labelledby="h-live" id="live">.*?</section>\s*', "", r, count=1, flags=re.S)
    r = re.sub(r"<script>\s*\(function\(\)\{\s*var root=document.getElementById\('live-root'\).*?</script>\s*", "", r, count=1, flags=re.S)
    r = r.replace('<a href="#live">live papirfond</a>', '<a href="index.html#live">live papirfond</a>')
    r = r.replace('<span class="label">Forskningsprojekt · Backtest v1 · Kontrolleret</span>',
                  '<a class="label" href="index.html">← Congress Fund</a><span class="label">Research · Backtest v1 · Kontrolleret</span>')
    assert "live-root" not in r and "index.html#live" in r and "← Congress Fund" in r
    style = re.search(r"<style>(.*?)</style>", r, flags=re.S).group(1)
    rbody = r[r.index("</style>") + len("</style>"):]
    rtitle = "Congress Fund Research"
    rdesc = "Metode, datakvalitet, backtest og uafhængig fejlkontrol bag Congress Fund."
    full = head(rtitle, rdesc, "research.html", style) + rbody.strip() + "\n</body>\n</html>\n"
    (DOCS / "research.html").write_text(full)
    (PREVIEW / "research.html").write_text(full)
    print("ok", len(data["dates"]), "weeks;", {k: round(v, 4) for k, v in data["metrics"]["all"]["fund"].items()})


if __name__ == "__main__":
    main()
