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
         '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Instrument+Serif&family=Inter:wght@400;500;600'
         '&family=IBM+Plex+Mono:wght@400;500&display=swap">')

MARK = ('<svg viewBox="0 0 28 28" aria-hidden="true"><rect class="mk-bg" x="1" y="1" width="26" height="26" rx="6"/>'
        '<path class="mk-roof" d="M6 11.5 14 6l8 5.5z"/>'
        '<path class="mk-col" d="M8.5 13.5v7.5M12.2 13.5v7.5M15.8 13.5v7.5M19.5 13.5v7.5M6 22.4h16"/></svg>')
MARK_CSS = (".mk-bg{fill:var(--ink)}.mk-roof{fill:var(--accent)}.mk-col{stroke:var(--canvas);stroke-width:1.6;fill:none}"
            "footer .mk-bg{fill:var(--band-ink)}footer .mk-col{stroke:var(--band)}")
FAVICON = ("data:image/svg+xml," + "%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 28 28'%3E"
           "%3Crect x='1' y='1' width='26' height='26' rx='6' fill='%230E1726'/%3E"
           "%3Cpath d='M6 11.5 14 6l8 5.5z' fill='%238EA6FF'/%3E"
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
    rows = (f"<tr><th>Ekklesia, %</th>{fund}</tr><tr><th>S&amp;P 500, %</th>{spy}</tr>"
            f"<tr><th>Forskel, procentpoint</th>{diff}</tr>")
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
<link rel="canonical" href="{url}">
<link rel="icon" href="{FAVICON}">
<meta name="theme-color" content="#F6F7F9">
{FONTS}
{POSTHOG}
<style>
:root{{color-scheme:light}}body{{margin:0}}img{{max-width:100%}}[hidden]{{display:none!important}}
{css}
</style>
{extra}</head>
<body>
"""


RESEARCH_OVERRIDES = """
/* Research page: map its old tokens onto the shared design system */
:root{--paper:var(--canvas);--sheet:var(--surface);--rule:var(--line);
  --step--1:.75rem;--step0:1rem;--step1:1.125rem;--step2:1.5rem;--step3:clamp(2rem,3.8vw,3rem);--step4:clamp(2.6rem,6vw,4.4rem)}
h1,h2,h3{font-stretch:normal}
h1,h2{font-family:var(--display);font-weight:400;letter-spacing:-.01em}
h3{font-family:var(--body);font-weight:600}
.figures b,.alpha b,.record-fields b{font-family:var(--display);font-weight:400;font-stretch:normal}
.record-fields b{font-family:var(--mono);font-weight:500}
b,strong{font-weight:600}
.r-hero{padding-block:64px 48px}
.figures,.record,.chart-box,.stage,.pending,.alpha{border-radius:var(--r)}
.figures{background:var(--line)}
#equity text,#yearly text,#xchart text,#buys text{font-size:8px}
@media (max-width:640px){#equity text,#yearly text,#xchart text,#buys text{font-size:12px}}
.status{font-size:.75rem}
section{border-top:0}
"""


def strip_tokens(css: str) -> str:
    """Drop the research page's own colour/token blocks; the shared design system defines them."""
    css = re.sub(r":root\{.*?\}\s*@media \(prefers-color-scheme: dark\)\{:root:not\(\[data-theme=\"light\"\]\)\{.*?\}\}\s*:root\[data-theme=\"dark\"\]\{.*?\}", "", css, count=1, flags=re.S)
    css = re.sub(r"(^|\n)body\{[^}]*\}", "\n", css, count=1)
    css = re.sub(r"(^|\n)footer\{[^}]*\}", "\n", css, count=1)
    css = re.sub(r"(^|\n)header\{[^}]*\}", "\n", css, count=1)
    return css


def main() -> None:
    DOCS.mkdir(exist_ok=True)
    PREVIEW.mkdir(parents=True, exist_ok=True)
    css = (SRC / "fund.css").read_text() + "\n" + MARK_CSS
    top = (SRC / "top.html").read_text()
    data, yearly = build_data()
    yh, yr = year_table(yearly)
    body = (SRC / "index.body.html").read_text()
    body = (body.replace("%%TOP%%", top).replace("%%MARK%%", MARK).replace("%%YEAR_HEAD%%", yh).replace("%%YEAR_ROWS%%", yr)
                .replace("%%DATA%%", json.dumps(data, separators=(",", ":"))))
    assert "%%" not in body
    footer = body[body.index("<footer>"):body.index("</footer>") + len("</footer>")]
    footer_abs = re.sub(r'href="#([a-z-]+)"', r'href="index.html#\1"', footer)
    title = "Ekklesia Capital"
    desc = ("Ekklesia Capital er en regelbaseret papirfond, der følger amerikanske kongresmedlemmers "
            "offentliggjorte aktiekøb. Ærlige resultater, åben kode og en live portefølje.")
    (DOCS / "index.html").write_text(head(title, desc, "index.html", css) + body + "\n</body>\n</html>\n")
    (PREVIEW / "index.html").write_text(
        f'<title>{title}</title>\n<meta name="claude:viewport-fit-sides" content="cover">\n{FONTS}\n<style>\n{css}\n</style>\n' + body)

    # Research page (case study): the detailed project page, with the shared nav and footer.
    r = (ROOT / "site" / "index.html").read_text()
    r = re.sub(r'<section aria-labelledby="h-live" id="live">.*?</section>\s*', "", r, count=1, flags=re.S)
    r = re.sub(r"<script>\s*\(function\(\)\{\s*var root=document.getElementById\('live-root'\).*?</script>\s*", "", r, count=1, flags=re.S)
    r = r.replace('<a href="#live">live papirfond</a>', '<a href="index.html#live">live papirfond</a>')
    # colours: old "oos" blue becomes slate, old soft shade becomes the shared test-period shade
    r = re.sub(r"var\(--oos\)", "var(--ink-2)", r).replace("var(--oos-soft)", "var(--oos)")
    r = r.replace('class="hero"', 'class="r-hero"').replace(".hero{", ".r-hero{").replace(".hero ", ".r-hero ")
    style = strip_tokens(re.search(r"<style>(.*?)</style>", r, flags=re.S).group(1))
    rbody = r[r.index("</style>") + len("</style>"):]
    rbody = re.sub(r"<header>.*?</header>\s*", "", rbody, count=1, flags=re.S)
    rbody = re.sub(r"<footer>.*?</footer>\s*", "", rbody, count=1, flags=re.S)
    k = rbody.index("<script")
    rhtml, rscripts = rbody[:k].strip(), rbody[k:].strip()
    rtitle = "Research og metode · Ekklesia Capital"
    rdesc = "Metode, datakvalitet, backtest og uafhængig fejlkontrol bag Ekklesia Capital."
    rcss = style + "\n" + css + "\n" + RESEARCH_OVERRIDES
    full = (head(rtitle, rdesc, "research.html", rcss) + top.replace("%%MARK%%", MARK)
            + '\n<main id="indhold">\n' + rhtml + "\n</main>\n" + footer_abs + "\n" + rscripts + "\n</body>\n</html>\n")
    full = full.replace('<a href="research.html">Research</a>', '<a href="research.html" aria-current="page">Research</a>')
    assert "live-root" not in full and "index.html#live" in full and "%%" not in full
    (DOCS / "research.html").write_text(full)
    (PREVIEW / "research.html").write_text(full)

    # Search engines
    (DOCS / "robots.txt").write_text(f"User-agent: *\nAllow: /\nSitemap: {SITE_URL}sitemap.xml\n")
    (DOCS / "sitemap.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"  <url><loc>{SITE_URL}</loc><changefreq>daily</changefreq><priority>1.0</priority></url>\n"
        f"  <url><loc>{SITE_URL}research.html</loc><changefreq>monthly</changefreq><priority>0.7</priority></url>\n"
        "</urlset>\n")
    print("ok", len(data["dates"]), "weeks;", {k: round(v, 4) for k, v in data["metrics"]["all"]["fund"].items()})


if __name__ == "__main__":
    main()
