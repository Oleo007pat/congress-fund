"""
coverage.py
-----------
Efter hentningen: kontrollerer for hver include_v1-handel, om der findes kurs på indgangsdagen
(første handelsdag efter filing_date) og 3, 6 og 12 måneder senere.

Kør:
    python3 prices/coverage.py

Skriver i data/prices/:
    coverage_summary.csv   dækning pr. år, separat for køb (scope=buy) og alle handler (scope=alle),
                           som andel af handler og andel af tickere
    unmatched_trades.csv   hver handel, hvor mindst én kurs mangler, med årsag
    coverage_reasons.csv   antal handler pr. årsag (år, scope, tidspunkt)

Regler (konservative):
  - Handelskalenderen er SPY's datoer. Indgangsdag = første SPY-dato efter filing_date.
  - "ok" kræver en bar for symbolet på præcis den dag (ingen tolerance). Med IEX-feed kan bars
    mangle på dage uden IEX-handler; det vises som hul_i_data.
  - 3/6/12 mdr.: kalendermåneder efter indgangsdagen; kursen tages første SPY-dato på/efter dagen.
  - Symboler, hvis data slutter, fjernes ikke: handlen får årsag data_slutter_foer.

Årsager: ingen_kursdata, data_starter_senere, data_slutter_foer (sandsynligvis afnoteret/opkøbt),
         hul_i_data, datofejl, ikke_naaet_endnu (horisonten ligger efter sidste kursdato).
"""

from __future__ import annotations

import bisect
import csv
import sys
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ticker_map  # noqa: E402
from price_common import (SPY, Paths, load_trades, parse_date, read_daily_dates,  # noqa: E402
                          truthy, write_csv)

HORIZONS = (3, 6, 12)
UNMATCHED_METHODS = ("unmatched", "not_fetched")


def add_months(d, n):
    y, m = divmod(d.month - 1 + n, 12)
    y, m = d.year + y, m + 1
    for day in (d.day, 30, 29, 28):
        try:
            return date(y, m, day)
        except ValueError:
            continue
    raise ValueError


class SymbolBars:
    def __init__(self, dates):
        self.dates = set(dates)
        self.first, self.last = min(dates), max(dates)


def load_map(paths, trades):
    if not paths.ticker_map.exists():
        ticker_map.build_and_write(paths, trades)
    with paths.ticker_map.open(encoding="utf-8", newline="") as fh:
        return {r["filing_ticker"]: r for r in csv.DictReader(fh)}


def date_error(t, today):
    if t.get("date_suspect") and truthy(t["date_suspect"]):
        return True
    f, x = t["_filed"], t["_tx"]
    if f is None or f.year < 2016 or f > today:
        return True
    if x is not None and f < x:
        return True
    return False


def status_for(day, bars, spy_last):
    if bars is None:
        return "ingen_kursdata"
    if day in bars.dates:
        return "ok"
    if bars.first > day:
        return "data_starter_senere"
    if bars.last < day and bars.last < spy_last - timedelta(days=7):
        return "data_slutter_foer"
    return "hul_i_data"


def evaluate(trades, tmap, spy_dates, bars_of, today):
    spy_last = spy_dates[-1]
    out = []
    for t in trades:
        tm = tmap.get(t["_ticker"])
        sym = tm["price_symbol"] if tm else ""
        bars = bars_of(sym) if sym else None
        res = {"entry_date": ""}
        if date_error(t, today):
            res.update(entry="datofejl", **{"h%d" % h: "datofejl" for h in HORIZONS})
        else:
            i = bisect.bisect_right(spy_dates, t["_filed"])
            if i >= len(spy_dates):
                res.update(entry="ikke_naaet_endnu", **{"h%d" % h: "ikke_naaet_endnu" for h in HORIZONS})
            else:
                entry = spy_dates[i]
                res["entry_date"] = entry.isoformat()
                res["entry"] = status_for(entry, bars, spy_last)
                for h in HORIZONS:
                    target = add_months(entry, h)
                    if target > spy_last:
                        res["h%d" % h] = "ikke_naaet_endnu"
                    else:
                        ex = spy_dates[bisect.bisect_left(spy_dates, target)]
                        res["h%d" % h] = status_for(ex, bars, spy_last)
        res["trade"] = t
        res["tm"] = tm
        out.append(res)
    return out


def trade_year(r):
    f = r["trade"]["_filed"]
    if f:
        return f.year
    try:
        return int(r["trade"].get("year", ""))
    except ValueError:
        return 0


def pct(a, b):
    return "%.1f" % (100.0 * a / b) if b else ""


def summarize(results, spy_last):
    cols = ["year", "scope", "n_trades", "n_tickers", "entry_ok", "entry_ok_pct"]
    for h in HORIZONS:
        cols += ["h%d_ok" % h, "h%d_eligible" % h, "h%d_ok_pct" % h]
    cols += ["tickers_any_entry", "tickers_any_entry_pct", "tickers_no_data", "tickers_no_data_pct",
             "tickers_data_ends_early", "tickers_data_ends_early_pct"]
    groups = defaultdict(list)
    for r in results:
        y = trade_year(r)
        for scope in ("alle", "buy") if r["trade"].get("type") == "buy" else ("alle",):
            groups[(y, scope)].append(r)
            groups[("ALL", scope)].append(r)
    rows = []
    key = lambda k: (str(k[0]) == "ALL", str(k[0]), k[1] != "buy")
    for (y, scope) in sorted(groups, key=key):
        rs = groups[(y, scope)]
        row = {"year": y, "scope": scope, "n_trades": len(rs)}
        ok = sum(1 for r in rs if r["entry"] == "ok")
        row.update(entry_ok=ok, entry_ok_pct=pct(ok, len(rs)))
        for h in HORIZONS:
            elig = [r for r in rs if r["h%d" % h] != "ikke_naaet_endnu"]
            hok = sum(1 for r in elig if r["h%d" % h] == "ok")
            row.update({"h%d_ok" % h: hok, "h%d_eligible" % h: len(elig),
                        "h%d_ok_pct" % h: pct(hok, len(elig))})
        tick = defaultdict(list)
        for r in rs:
            tick[r["trade"]["_ticker"]].append(r)
        n = len(tick)
        any_entry = sum(1 for v in tick.values() if any(r["entry"] == "ok" for r in v))
        nod = ended = 0
        for tk, v in tick.items():
            tm = v[0]["tm"]
            if tm is None or tm["method"] in UNMATCHED_METHODS:
                nod += 1
            elif parse_date(tm["last_date"]) and parse_date(tm["last_date"]) < spy_last - timedelta(days=30):
                ended += 1
        row.update(n_tickers=n, tickers_any_entry=any_entry, tickers_any_entry_pct=pct(any_entry, n),
                   tickers_no_data=nod, tickers_no_data_pct=pct(nod, n),
                   tickers_data_ends_early=ended, tickers_data_ends_early_pct=pct(ended, n))
        rows.append(row)
    return cols, rows


def main(root=None):
    paths = Paths(root or Path(__file__).resolve().parent.parent)
    if not paths.trades.exists() or not paths.daily_file(SPY).exists():
        print("Mangler trades.csv eller SPY-kurser - kør python3 prices/fetch_prices.py først.")
        return 1
    trades = load_trades(paths.trades)
    spy_dates = read_daily_dates(paths.daily_file(SPY))
    if not spy_dates:
        print("SPY-filen er tom - kør fetch_prices.py igen.")
        return 1
    tmap = load_map(paths, trades)
    cache = {}

    def bars_of(sym):
        if sym not in cache:
            d = read_daily_dates(paths.daily_file(sym))
            cache[sym] = SymbolBars(d) if d else None
        return cache[sym]

    results = evaluate(trades, tmap, spy_dates, bars_of, date.today())
    cols, rows = summarize(results, spy_dates[-1])
    write_csv(paths.coverage, cols, rows)

    un_cols = ["year", "doc_id", "member", "ticker", "type", "transaction_date", "filing_date",
               "entry_date", "price_symbol", "match_method", "symbol_first_date", "symbol_last_date",
               "entry_status", "h3_status", "h6_status", "h12_status", "first_problem"]
    un_rows = []
    reasons = Counter()
    for r in results:
        t, tm = r["trade"], r["tm"] or {}
        stat = [("entry", r["entry"])] + [("h%d" % h, r["h%d" % h]) for h in HORIZONS]
        scopes = ("alle", "buy") if t.get("type") == "buy" else ("alle",)
        for label, s in stat:
            if s != "ok":
                for sc in scopes:
                    reasons[(trade_year(r), sc, label, s)] += 1
                    reasons[("ALL", sc, label, s)] += 1
        bad = [(l, s) for l, s in stat if s not in ("ok", "ikke_naaet_endnu")]
        if bad:
            un_rows.append([trade_year(r), t.get("doc_id", ""), t.get("member", ""), t["_ticker"],
                            t.get("type", ""), t.get("transaction_date", ""), t.get("filing_date", ""),
                            r["entry_date"], tm.get("price_symbol", ""), tm.get("method", ""),
                            tm.get("first_date", ""), tm.get("last_date", ""),
                            r["entry"], r["h3"], r["h6"], r["h12"], "%s:%s" % bad[0]])
    write_csv(paths.unmatched, un_cols, un_rows)
    write_csv(paths.reasons, ["year", "scope", "when", "reason", "n_trades"],
              [list(k) + [v] for k, v in sorted(reasons.items(), key=lambda kv: (str(kv[0][0]),) + kv[0][1:])])

    allrow = next(r for r in rows if r["year"] == "ALL" and r["scope"] == "buy")
    print("Dækning (køb, alle år): indgangskurs %s%%, +3 mdr %s%%, +6 mdr %s%%, +12 mdr %s%%"
          % (allrow["entry_ok_pct"], allrow["h3_ok_pct"], allrow["h6_ok_pct"], allrow["h12_ok_pct"]))
    print("Tickere i køb uden nogen kurs: %s af %s" % (allrow["tickers_no_data"], allrow["n_tickers"]))
    print("Skrevet: %s, %s, %s" % (paths.coverage.name, paths.unmatched.name, paths.reasons.name))
    return 0


if __name__ == "__main__":
    sys.exit(main())
