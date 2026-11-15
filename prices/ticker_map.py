"""
ticker_map.py
-------------
Kobler tickerne i indberetningerne til kursfilerne i data/prices/daily/ og skriver
data/prices/ticker_map.csv  (filing_ticker, price_symbol, method, first_date, last_date, n_bars, note).

Kør (efter fetch_prices.py, som også kalder dette script til sidst):
    python3 prices/ticker_map.py

method:
  exact               tickeren fra indberetningen findes hos Alpaca uændret
  normalized          fundet efter rensning (store bogstaver, fjernet støj)
  class_variant       klasseaktie fundet under anden skrivemåde (BRK.B / BRK-B / BRK/B)
  alpaca_asof         fundet via Alpacas asof-parameter (gammelt symbol på handelsdagen)
  alpaca_name_change  Alpacas selskabshændelser siger gammelt -> nyt symbol, og det nye har kurser
  unmatched           ingen kurser fundet (årsag i note)
  not_fetched         tickeren er endnu ikke forsøgt hentet (kør fetch_prices.py igen)

price_symbol er navnet på filen i data/prices/daily/ (BRK/B gemmes som BRK_B.csv).
Der er ingen håndskrevet omdøbningsliste: tickerskift løses kun via Alpacas egne data.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from price_common import (Paths, SPY, load_no_data, load_state, load_trades,  # noqa: E402
                          parse_date, read_daily, write_csv)

MAP_COLS = ["filing_ticker", "price_symbol", "method", "first_date", "last_date", "n_bars", "note"]
_STRIP = "$()[]{}\"'*,;: \t"


def candidate_symbols(raw):
    """Returnerer [(symbol, method)] i prøverækkefølge. Tom liste = ugyldigt format.
    Skrivemåden fra indberetningen prøves først."""
    s = (raw or "").strip()
    cleaned = re.sub(r"\s+", " ", s.upper().strip(_STRIP)).rstrip("./-").strip()
    if not cleaned or cleaned in ("N/A", "N.A", "NONE", "NULL"):
        return []
    base_method = "exact" if cleaned == s else "normalized"
    if re.fullmatch(r"[A-Z]{1,6}", cleaned):
        return [(cleaned, base_method)]
    m = re.fullmatch(r"([A-Z]{1,5})[./\- ]([A-Z]{1,2})", cleaned)
    if not m:
        return []
    base, cls = m.groups()
    variants = [base + sep + cls for sep in (".", "-", "/")]
    own = cleaned.replace(" ", ".")            # skrivemåden fra indberetningen først
    order = [own] + [v for v in variants if v != own]
    out = []
    for v in order:
        method = base_method if v == cleaned else "class_variant"
        out.append((v, method))
    return out


def all_candidates(trades):
    """{filing_ticker: {'cands': [(sym, method)], 'first_tx': date|None, 'first_filed': date|None}}"""
    info = {}
    for t in trades:
        tk = t["_ticker"]
        e = info.setdefault(tk, {"cands": candidate_symbols(tk), "first_tx": None, "first_filed": None})
        for key, fld in (("first_tx", "_tx"), ("first_filed", "_filed")):
            d = t[fld]
            if d and (e[key] is None or d < e[key]):
                e[key] = d
    return info


def _bar_info(paths, sym, cache):
    if sym not in cache:
        rows = read_daily(paths.daily_file(sym))
        cache[sym] = (rows[0][0], rows[-1][0], len(rows)) if rows else None
    return cache[sym]


def build_map(paths, trades, ref_end=None):
    """Returnerer liste af dicts (én pr. filing_ticker)."""
    info = all_candidates(trades)
    state = load_state(paths)["symbols"]
    no_data = load_no_data(paths)
    cache = {}
    if ref_end is None:
        spy = _bar_info(paths, SPY, cache)
        ref_end = spy[1] if spy else None
    rows = []
    for tk in sorted(info):
        e = info[tk]
        chosen = None          # (sym, method, barinfo)
        with_data = []
        for sym, method in e["cands"]:
            bi = _bar_info(paths, sym, cache)
            if bi:
                with_data.append(sym)
                if chosen is None:
                    chosen = (sym, method, bi)
        notes = []
        if chosen:
            sym, method, bi = chosen
            st = state.get(sym, {})
            if st.get("asof"):
                method = "alpaca_asof"
                notes.append("Alpaca asof=%s" % st["asof"])
                if st.get("returned_symbol") and st["returned_symbol"] != sym:
                    notes.append("Alpacas eget symbol: %s" % st["returned_symbol"])
            if len(with_data) > 1:
                notes.append("flere skrivemåder havde data: %s" % "|".join(with_data))
            _decorate(notes, bi, e, ref_end)
            rows.append(dict(filing_ticker=tk, price_symbol=sym, method=method,
                             first_date=bi[0].isoformat(), last_date=bi[1].isoformat(),
                             n_bars=bi[2], note="; ".join(notes)))
            continue
        # ingen direkte kurser - prøv selskabshændelse (gammelt -> nyt symbol)
        target = None
        for sym, _m in e["cands"]:
            nd = no_data.get(sym)
            if nd and nd.get("name_change_to"):
                bi = _bar_info(paths, nd["name_change_to"], cache)
                if bi:
                    target = (nd["name_change_to"], bi, nd)
                    break
        if target:
            sym, bi, nd = target
            notes.append("Alpaca selskabshændelse: %s" % nd.get("corp_note", ""))
            notes.append("kurser findes kun, hvis Alpaca har linket dem til det nye symbol")
            _decorate(notes, bi, e, ref_end)
            rows.append(dict(filing_ticker=tk, price_symbol=sym, method="alpaca_name_change",
                             first_date=bi[0].isoformat(), last_date=bi[1].isoformat(),
                             n_bars=bi[2], note="; ".join(notes)))
            continue
        if not e["cands"]:
            rows.append(dict(filing_ticker=tk, price_symbol="", method="unmatched", first_date="",
                             last_date="", n_bars=0, note="ugyldigt tickerformat"))
            continue
        reasons, fetched = [], False
        for sym, _m in e["cands"]:
            nd = no_data.get(sym)
            if nd:
                fetched = True
                reasons.append("%s: %s" % (sym, nd["reason"]))
                if nd.get("corp_note"):
                    reasons.append("Alpaca selskabshændelse: %s" % nd["corp_note"])
        if not fetched:
            rows.append(dict(filing_ticker=tk, price_symbol="", method="not_fetched", first_date="",
                             last_date="", n_bars=0, note="ikke forsøgt hentet endnu"))
            continue
        if re.fullmatch(r"[A-Z]{4,5}[YF]", e["cands"][0][0]):
            reasons.append("ligner OTC/ADR-ticker (heuristik: Y/F-endelse); OTC er ikke dækket af gratis feed")
        if asof_tried_all(no_data, e["cands"]):
            reasons.append("asof-forsøg uden resultat")
        rows.append(dict(filing_ticker=tk, price_symbol="", method="unmatched", first_date="",
                         last_date="", n_bars=0, note="; ".join(dict.fromkeys(reasons))))
    return rows


def asof_tried_all(no_data, cands):
    return all(no_data.get(s, {}).get("asof_tried") == "ja" for s, _ in cands)


def _decorate(notes, bi, e, ref_end):
    first, last = bi[0], bi[1]
    if ref_end and last < ref_end - timedelta(days=10):
        notes.append("data slutter %s: sandsynligvis afnoteret/opkøbt/tickerskift - symbolet er IKKE "
                     "fjernet, backtesten skal håndtere exit" % last.isoformat())
    ft = e.get("first_tx") or e.get("first_filed")
    if ft and first > ft + timedelta(days=30) and first > parse_date("2016-02-01"):
        notes.append("data starter %s, første handel %s: handler før dette er uden kurs "
                     "(senere børsnotering eller genbrugt ticker - ikke verificeret)"
                     % (first.isoformat(), ft.isoformat()))


def build_and_write(paths, trades=None):
    trades = trades if trades is not None else load_trades(paths.trades)
    rows = build_map(paths, trades)
    write_csv(paths.ticker_map, MAP_COLS, rows)
    return rows


def main(root=None):
    paths = Paths(root or Path(__file__).resolve().parent.parent)
    if not paths.trades.exists():
        print("Fandt ikke %s" % paths.trades)
        return 1
    rows = build_and_write(paths)
    from collections import Counter
    c = Counter(r["method"] for r in rows)
    print("Tickerkobling skrevet til %s" % paths.ticker_map)
    for k, v in sorted(c.items()):
        print("  %-20s %d" % (k, v))
    return 0


if __name__ == "__main__":
    sys.exit(main())
