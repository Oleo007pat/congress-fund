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
SITE_URL = "https://ekklesia-capital.vercel.app/"
POSTHOG_TOKEN = "phc_AK2oxvEZ4qMo3GcPnu7RjbgbFF8aaM4QZd82L7NS9mtA"  # public client token, safe to publish
POSTHOG_HOST = "https://eu.i.posthog.com"

FONTS = ('<link rel="preconnect" href="https://fonts.googleapis.com">\n'
         '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>\n'
         '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Libre+Caslon+Display'
         '&family=Public+Sans:wght@400;500;600&display=swap">')

MARK = ('<svg viewBox="0 0 28 28" aria-hidden="true"><rect class="mk-bg" x="1" y="1" width="26" height="26" rx="6"/>'
        '<path class="mk-roof" d="M6 11.5 14 6l8 5.5z"/>'
        '<path class="mk-col" d="M8.5 13.5v7.5M12.2 13.5v7.5M15.8 13.5v7.5M19.5 13.5v7.5M6 22.4h16"/></svg>')
FAVICON = ("data:image/svg+xml," + "%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 28 28'%3E"
           "%3Crect x='1' y='1' width='26' height='26' rx='6' fill='%2317211C'/%3E"
           "%3Cpath d='M6 11.5 14 6l8 5.5z' fill='%237CC7A5'/%3E"
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
                "note": "3 January 2022 to 16 November 2026, after the rules were fixed. Sharpe ratio without a risk-free rate.",
            },
            "all": {
                "fund": stats(s), "spy": stats(m),
                "note": ("6 January 2016 to 16 November 2026, simulated. Sharpe ratio without a risk-free rate. "
                         "The rules were chosen using 2016–2021 data."),
            },
        },
    }
    return data, sd["yearly"]


def year_table(yearly: list) -> tuple[str, str]:
    f = lambda v: ("+" if v > 0 else "−" if v < 0 else "") + f"{abs(v) * 100:.1f}"
    head = "".join(f'<th class="r">{y["year"]}{"*" if y["year"] == 2026 else ""}</th>' for y in yearly)
    fund = "".join(f'<td class="r">{f(y["strategy"])}</td>' for y in yearly)
    spy = "".join(f'<td class="r">{f(y["spy"])}</td>' for y in yearly)
    diff = "".join(f'<td class="r {"pos" if y["excess"] > 0 else "neg"}">{f(y["excess"])}</td>' for y in yearly)
    rows = (f"<tr><th>Ekklesia, %</th>{fund}</tr><tr><th>S&amp;P 500, %</th>{spy}</tr>"
            f"<tr><th>Difference, points</th>{diff}</tr>")
    return head, rows


POSTHOG = """<script>
/* Anonymous, cookieless page statistics (PostHog EU). Runs only on the public site, not on local copies. */
if (/(vercel\\.app|github\\.io)$/.test(location.hostname)) {
!function(t,e){var o,n,p,r;e.__SV||(window.posthog=e,e._i=[],e.init=function(i,s,a){function g(t,e){var o=e.split(".");2==o.length&&(t=t[o[0]],e=o[1]),t[e]=function(){t.push([e].concat(Array.prototype.slice.call(arguments,0)))}}(p=t.createElement("script")).type="text/javascript",p.crossOrigin="anonymous",p.async=!0,p.src=s.api_host.replace(".i.posthog.com","-assets.i.posthog.com")+"/static/array.js",(r=t.getElementsByTagName("script")[0]).parentNode.insertBefore(p,r);var u=e;for(void 0!==a?u=e[a]=[]:a="posthog",u.people=u.people||[],u.toString=function(t){var e="posthog";return"posthog"!==a&&(e+="."+a),t||(e+=" (stub)"),e},u.people.toString=function(){return u.toString(1)+".people (stub)"},o="capture identify alias people.set people.set_once set_config register register_once unregister opt_out_capturing has_opted_out_capturing opt_in_capturing reset isFeatureEnabled onFeatureFlags getFeatureFlag getFeatureFlagPayload reloadFeatureFlags group".split(" "),n=0;n<o.length;n++)g(u,o[n]);e._i.push([i,s,a])},e.__SV=1)}(document,window.posthog||[]);
posthog.init("%s",{api_host:"%s",cookieless_mode:"always",person_profiles:"identified_only",autocapture:false,capture_pageview:true,capture_pageleave:true,disable_session_recording:true});
}
</script>""" % (POSTHOG_TOKEN, POSTHOG_HOST)


def head(title: str, desc: str, path: str, css: str, extra: str = "") -> str:
    url = SITE_URL + ("" if path == "index.html" else path)
    return f"""<!doctype html>
<html lang="en">
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
<link rel="canonical" href="{url}">
<link rel="icon" href="{FAVICON}">
<meta name="theme-color" content="#F2F3EF">
{FONTS}
{POSTHOG}
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
    css = (SRC / "site.css").read_text()
    header = (SRC / "header.html").read_text().replace("%%MARK%%", MARK)
    footer = (SRC / "footer.html").read_text().replace("%%MARK%%", MARK)
    data, yearly = build_data()
    yh, yr = year_table(yearly)

    def assemble(src: str, **extra: str) -> str:
        body = (SRC / src).read_text().replace("%%HEADER%%", header).replace("%%FOOTER%%", footer)
        for k, v in extra.items():
            body = body.replace(f"%%{k}%%", v)
        assert "%%" not in body, src
        return body

    home = assemble("home.html", YEAR_HEAD=yh, YEAR_ROWS=yr, DATA=json.dumps(data, separators=(",", ":")),
                    LIVEJS=(SRC / "live.html").read_text())
    title = "Ekklesia Capital — a fund that follows Congress's stock trades"
    desc = ("Ekklesia Capital is a rule-based paper fund that follows the stock purchases disclosed by members of the "
            "U.S. Congress. Ten years of simulated results, open code and a live portfolio.")
    (DOCS / "index.html").write_text(head(title, desc, "index.html", css) + home + "\n</body>\n</html>\n")
    (PREVIEW / "index.html").write_text(
        f'<title>Ekklesia Capital</title>\n<meta name="claude:viewport-fit-sides" content="cover">\n{FONTS}\n<style>\n{css}\n</style>\n' + home)

    research = assemble("research.html", RESDATA=(SRC / "research_data.json").read_text())
    research = research.replace('<a href="research.html">Research</a>', '<a href="research.html" aria-current="page">Research</a>', 1)
    rtitle = "Research and method · Ekklesia Capital"
    rdesc = "How Ekklesia Capital's backtest was built: data, method, an independent review and the limits of the data."
    full = head(rtitle, rdesc, "research.html", css) + research + "\n</body>\n</html>\n"
    (DOCS / "research.html").write_text(full)
    (PREVIEW / "research.html").write_text(full)

    (DOCS / "robots.txt").write_text(f"User-agent: *\nAllow: /\nSitemap: {SITE_URL}sitemap.xml\n")
    (DOCS / "sitemap.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"  <url><loc>{SITE_URL}</loc><changefreq>daily</changefreq><priority>1.0</priority></url>\n"
        f"  <url><loc>{SITE_URL}research.html</loc><changefreq>monthly</changefreq><priority>0.7</priority></url>\n"
        "</urlset>\n")
    print("ok", len(data["dates"]), "weeks;", {k: round(v, 4) for k, v in data["metrics"]["all"]["fund"].items()})


if __name__ == "__main__":
    main()
