"""
fetch_prices.py
---------------
Henter justerede dagskurser (open, high, low, close, volume; adjustment=all) fra Alpaca for alle
tickere i handler med include_v1 plus SPY, og gemmer én fil pr. symbol i data/prices/daily/.

Kør fra projektmappen (kræver ALPACA_KEY_ID og ALPACA_SECRET_KEY i .env):
    python3 prices/fetch_prices.py

Kan afbrydes og køres igen: kun manglende symboler og nye dage hentes. Symboler uden data
logges i data/prices/no_data.csv og spørges ikke igen, medmindre du bruger --retry-no-data.

Flag:  --retry-no-data   spørg igen om symboler uden data
       --full            hent alt forfra
       --no-asof         spring asof-forsøg over for symboler uden data
       --no-corp-actions spring opslag i Alpacas selskabshændelser over
       --start/--end     datoer (standard 2015-12-01 og i går)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ticker_map  # noqa: E402
from alpaca_client import (DATA_URL, AlpacaClient, AlpacaError, AuthError,  # noqa: E402
                           BadRequest, ForbiddenError)
from price_common import (DEFAULT_START, SPY, Paths, load_no_data, load_state,  # noqa: E402
                          load_trades, now_iso, parse_date, read_daily, save_no_data,
                          save_state, write_daily)

OVERLAP_DAYS = 7
CORP_TYPES = ["name_change", "cash_merger", "stock_merger", "stock_and_cash_merger",
              "redemption", "worthless_removal"]


def say(msg):
    print(msg, flush=True)


# ------------------------------------------------------------------ hjælpere

def bars_to_rows(bars):
    """Alpaca-bars -> [(date,o,h,l,c,v)]; dropper bars uden pris. Returnerer (rows, n_droppet)."""
    seen, dropped = {}, 0
    for b in bars:
        try:
            d = date.fromisoformat(str(b["t"])[:10])
            o, h, lo, c = (float(b[k]) for k in ("o", "h", "l", "c"))
            v = float(b.get("v") or 0)
        except (KeyError, TypeError, ValueError):
            dropped += 1
            continue
        if c <= 0 or o <= 0:
            dropped += 1
            continue
        seen[d] = (d, o, h, lo, c, v)
    return sorted(seen.values()), dropped


def differs(old, new):
    return abs(new - old) > 0.0002 + 1e-4 * abs(old)


def fetch_batch(client, syms, start, end, asof=None):
    """Returnerer (data, afvist). Ved 400 deles batchen, til det enkelte afviste symbol er fundet."""
    try:
        return client.bars_auto(syms, start, end, asof=asof), {}
    except BadRequest as exc:
        if len(syms) == 1:
            return {}, {syms[0]: "afvist af Alpaca: %s" % exc}
        mid = len(syms) // 2
        d1, r1 = fetch_batch(client, syms[:mid], start, end, asof)
        d2, r2 = fetch_batch(client, syms[mid:], start, end, asof)
        d1.update(d2)
        r1.update(r2)
        return d1, r1


def first_asof_date(info):
    d = info.get("first_tx") or info.get("first_filed")
    floor = date(2016, 1, 1)
    return max(d, floor) if d else floor


class Run:
    def __init__(self, args, paths, client):
        self.args, self.paths, self.client = args, paths, client
        self.state = load_state(paths)
        self.no_data = load_no_data(paths)
        self.end = args.end
        self.start = args.start
        self.failed = set()
        self.dropped = 0

    def save(self):
        save_state(self.paths, self.state)
        save_no_data(self.paths, self.no_data)

    def mark_ok(self, sym, **extra):
        st = self.state["symbols"].setdefault(sym, {})
        st.update(checked_through=self.end, feed=self.client.feed, **extra)
        self.no_data.pop(sym, None)

    def mark_no_data(self, sym, reason):
        old = self.no_data.get(sym, {})
        self.no_data[sym] = dict(
            symbol=sym, reason=reason, checked_at=now_iso(), feed=self.client.feed,
            asof_tried="nej", corp_checked=old.get("corp_checked", ""),
            corp_note=old.get("corp_note", ""), name_change_to=old.get("name_change_to", ""))

    # ----------------------------------------------------------- standardhentning
    def store_full(self, sym, bars, rejected=None):
        rows, dropped = bars_to_rows(bars)
        self.dropped += dropped
        if rows:
            write_daily(self.paths.daily_file(sym), rows)
            self.mark_ok(sym)
            return True
        self.mark_no_data(sym, rejected or "ingen data hos Alpaca (feed %s, fra %s)"
                          % (self.client.feed, self.start))
        return False

    def store_incremental(self, sym, bars):
        """Returnerer True hvis OK, False hvis symbolet skal hentes forfra (justering ændret)."""
        path = self.paths.daily_file(sym)
        old = read_daily(path)
        new, dropped = bars_to_rows(bars)
        self.dropped += dropped
        if not new:                      # intet nyt (fx afnoteret): behold filen
            self.mark_ok(sym)
            return True
        old_by = {r[0]: r for r in old}
        common = [r for r in new if r[0] in old_by]
        if not common:
            return False                 # kan ikke kontrollere justeringen
        for r in common:
            o = old_by[r[0]]
            if differs(o[4], r[4]) or differs(o[1], r[1]):
                return False             # udbytte/split har ændret historikken
        last = old[-1][0]
        merged = old + [r for r in new if r[0] > last]
        write_daily(path, merged)
        self.mark_ok(sym)
        return True

    def fetch_group(self, syms, start, incremental, label):
        """Henter syms i batches. Returnerer liste af symboler, der kræver fuld genhentning."""
        refull = []
        bs = self.args.batch_size
        for i in range(0, len(syms), bs):
            batch = syms[i:i + bs]
            try:
                data, rejected = fetch_batch(self.client, batch, start, self.end)
            except AuthError:
                raise
            except AlpacaError as exc:
                say("  Fejl (%s) - %d symboler springes over og tages næste kørsel" % (exc, len(batch)))
                self.failed.update(batch)
                continue
            for sym in batch:
                if incremental:
                    if not self.store_incremental(sym, data.get(sym, [])):
                        refull.append(sym)
                else:
                    self.store_full(sym, data.get(sym, []), rejected.get(sym))
            self.save()
            say("  %s: %d/%d (%s-data)" % (label, min(i + bs, len(syms)), len(syms),
                                           self.client.feed.upper()))
        return refull

    # --------------------------------------------------------------- hele flowet
    def plan(self, symbols):
        """Returnerer (fuld, inkrementel, antal sprunget over, {symbol: sidste dato})."""
        full, inc, skipped, lasts = [], [], 0, {}
        end = parse_date(self.end)
        for sym in symbols:
            st = self.state["symbols"].get(sym, {})
            if st.get("asof"):
                continue                       # håndteres i asof-opdatering
            if sym in self.no_data and not self.args.retry_no_data:
                skipped += 1
                continue
            rows = read_daily(self.paths.daily_file(sym))
            if self.args.full or not rows:
                full.append(sym)
            elif st.get("checked_through") != self.end:
                last = rows[-1][0]
                checked = parse_date(st.get("checked_through"))
                # data slutter for længst (afnoteret): tjek kun hver ~30. dag
                if last < end - timedelta(days=45) and checked and checked >= end - timedelta(days=30):
                    skipped += 1
                    continue
                inc.append(sym)
                lasts[sym] = last
        return full, inc, skipped, lasts

    def default_stage(self, symbols):
        full, inc, skipped, lasts = self.plan(symbols)
        say("Standardhentning: %d nye, %d at opdatere, %d springes over (ingen data/afnoteret)"
            % (len(full), len(inc), skipped))
        end = parse_date(self.end)
        groups = {}
        for sym in inc:
            recent = lasts[sym] >= end - timedelta(days=45)
            start = (lasts[sym] - timedelta(days=OVERLAP_DAYS)).isoformat()
            groups.setdefault("recent" if recent else start, []).append(sym)
        recent = groups.pop("recent", [])
        if recent:
            st = min(lasts[s] for s in recent) - timedelta(days=OVERLAP_DAYS)
            full += self.fetch_group(recent, max(st.isoformat(), self.start), True, "opdaterer")
        for start, syms in sorted(groups.items()):
            full += self.fetch_group(syms, max(start, self.start), True, "opdaterer (afnoteret)")
        if full:
            self.fetch_group(full, self.start, False, "henter")

    def asof_stage(self, info):
        """Symboler uden data: prøv Alpacas asof (symbolet, som det var på handelsdagen)."""
        todo = []
        for tk, e in info.items():
            syms = [s for s, _ in e["cands"]]
            if syms and all(s in self.no_data and self.no_data[s]["asof_tried"] != "ja" for s in syms):
                todo.append((tk, e))
        if self.args.no_asof or not todo:
            return
        say("asof-forsøg for %d tickere uden data ..." % len(todo))
        n_ok = 0
        for tk, e in todo:
            asof = first_asof_date(e).isoformat()
            hit = False
            for sym, _m in e["cands"]:
                try:
                    data, _rej = fetch_batch(self.client, [sym], self.start, self.end, asof=asof)
                except AuthError:
                    raise
                except AlpacaError as exc:
                    say("  Fejl ved asof %s (%s)" % (sym, exc))
                    self.failed.add(sym)
                    break
                key, bars = None, []
                if sym in data:
                    key, bars = sym, data[sym]
                elif len(data) == 1:
                    key, bars = next(iter(data.items()))
                rows, dropped = bars_to_rows(bars)
                self.dropped += dropped
                if rows:
                    write_daily(self.paths.daily_file(sym), rows)
                    self.mark_ok(sym, asof=asof, returned_symbol=key)
                    n_ok += 1
                    hit = True
                    break
            if not hit and not any(s in self.failed for s, _ in e["cands"]):
                for sym, _m in e["cands"]:
                    if sym in self.no_data:
                        self.no_data[sym]["asof_tried"] = "ja"
        self.save()
        say("  %d tickere fundet via asof" % n_ok)

    def refresh_asof(self):
        syms = [s for s, st in self.state["symbols"].items()
                if st.get("asof") and (st.get("checked_through") != self.end or self.args.full)]
        for sym in sorted(syms):
            st = self.state["symbols"][sym]
            try:
                data, _ = fetch_batch(self.client, [sym], self.start, self.end, asof=st["asof"])
            except AuthError:
                raise
            except AlpacaError as exc:
                say("  Fejl ved opdatering af %s (%s)" % (sym, exc))
                self.failed.add(sym)
                continue
            bars = data.get(sym) or (next(iter(data.values())) if len(data) == 1 else [])
            rows, _d = bars_to_rows(bars)
            if rows:
                write_daily(self.paths.daily_file(sym), rows)
                st["checked_through"] = self.end
        self.save()

    def corp_stage(self, info):
        """Selskabshændelser (kun information + gammelt->nyt symbol). Felter er ikke bekræftet."""
        if self.args.no_corp_actions:
            return
        syms = []
        for e in info.values():
            for s, _m in e["cands"]:
                nd = self.no_data.get(s)
                if nd and nd["asof_tried"] == "ja" and nd["corp_checked"] not in ("ja",):
                    syms.append(s)
        syms = sorted(set(syms))
        if not syms:
            return
        say("Slår %d tickere uden data op i Alpacas selskabshændelser ..." % len(syms))
        targets = set()
        for i in range(0, len(syms), 40):
            batch = syms[i:i + 40]
            try:
                pages = self.client.corporate_actions(batch, CORP_TYPES, self.start, self.end)
            except AuthError:
                raise
            except AlpacaError as exc:
                say("  Selskabshændelser kunne ikke hentes (%s) - springes over" % exc)
                for s in batch:
                    self.no_data[s]["corp_checked"] = "fejl"
                continue
            found = parse_corporate_actions(pages, set(batch))
            for s in batch:
                nd = self.no_data[s]
                nd["corp_checked"] = "ja"
                acts = found.get(s, [])
                nd["corp_note"] = " | ".join(a["text"] for a in acts)
                for a in acts:
                    if a.get("new_symbol"):
                        nd["name_change_to"] = a["new_symbol"]
                        targets.add(a["new_symbol"])
        self.save()
        missing = sorted(t for t in targets if not read_daily(self.paths.daily_file(t))
                         and t not in self.no_data)
        if missing:
            say("Henter %d nye symboler fra navneskift ..." % len(missing))
            self.fetch_group(missing, self.start, False, "navneskift")


def parse_corporate_actions(pages, wanted):
    """Finder rekursivt dicts med felterne old_symbol/new_symbol (name_change) eller
    acquiree_symbol/acquirer_symbol (fusion) og andre med symbol. Returnerer {symbol: [handling]}."""
    out = {}

    def add(sym, text, new_symbol=None):
        if sym in wanted:
            out.setdefault(sym, []).append({"text": text, "new_symbol": new_symbol})

    def walk(x):
        if isinstance(x, list):
            for i in x:
                walk(i)
        elif isinstance(x, dict):
            when = x.get("process_date") or x.get("effective_date") or ""
            if "old_symbol" in x and "new_symbol" in x:
                add(x["old_symbol"], "navneskift til %s pr. %s" % (x["new_symbol"], when), x["new_symbol"])
            elif "acquiree_symbol" in x:
                add(x["acquiree_symbol"], "fusion: opkøbt af %s pr. %s (ingen kurser kobles)"
                    % (x.get("acquirer_symbol", "?"), when))
            elif "symbol" in x:
                add(x["symbol"], "selskabshændelse pr. %s (%s)" % (when, x.get("type", "ukendt type")))
            for v in x.values():
                if isinstance(v, (list, dict)):
                    walk(v)
    for p in pages:
        walk(p)
    return out


# ------------------------------------------------------------------- main

def parse_args(argv):
    ap = argparse.ArgumentParser(description="Henter justerede dagskurser fra Alpaca.")
    ap.add_argument("--retry-no-data", action="store_true")
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--no-asof", action="store_true")
    ap.add_argument("--no-corp-actions", action="store_true")
    ap.add_argument("--start", default=DEFAULT_START)
    ap.add_argument("--end", default=(date.today() - timedelta(days=1)).isoformat())
    ap.add_argument("--feed", default="sip", choices=["sip", "iex"])
    ap.add_argument("--batch-size", type=int, default=100)
    ap.add_argument("--min-interval", type=float, default=0.35, help="sekunder mellem kald")
    ap.add_argument("--base-url", default=DATA_URL)
    return ap.parse_args(argv)


def main(argv=None, root=None, client=None):
    args = parse_args(argv)
    root = Path(root or Path(__file__).resolve().parent.parent)
    paths = Paths(root)
    if not paths.trades.exists():
        say("Fandt ikke %s - kør parseren først." % paths.trades)
        return 1
    if client is None:
        try:
            from dotenv import load_dotenv
            load_dotenv(root / ".env")
        except ImportError:
            pass
        key, secret = os.getenv("ALPACA_KEY_ID"), os.getenv("ALPACA_SECRET_KEY")
        if not key or not secret:
            say("Mangler ALPACA_KEY_ID / ALPACA_SECRET_KEY i .env i projektmappen.")
            return 1
        client = AlpacaClient(key, secret, base_url=args.base_url, feed=args.feed,
                              min_interval=args.min_interval, log=say)
    else:
        client.log = say

    trades = load_trades(paths.trades)
    info = ticker_map.all_candidates(trades)
    symbols, seen = [SPY], {SPY}
    for tk in sorted(info):
        for s, _m in info[tk]["cands"]:
            if s not in seen:
                seen.add(s)
                symbols.append(s)
    say("%d handler, %d tickere i indberetningerne, %d symboler at slå op (inkl. SPY)"
        % (len(trades), len(info), len(symbols)))

    run = Run(args, paths, client)
    paths.daily.mkdir(parents=True, exist_ok=True)
    try:
        # SPY først: afgør feed og hvor langt tilbage kontoen har data
        spy_args = run.plan([SPY])
        if spy_args[0] or spy_args[1]:
            try:
                run.default_stage([SPY])
            except BadRequest:
                say("Alpaca afviste startdatoen %s - prøver 2016-01-01." % run.start)
                run.start = "2016-01-01"
                run.default_stage([SPY])
        if not read_daily(paths.daily_file(SPY)):
            say("Ingen SPY-kurser hentet (feed %s). Tjek nøgler og plan. Afbryder." % client.feed)
            return 1
        run.default_stage([s for s in symbols if s != SPY])
        run.asof_stage(info)
        run.refresh_asof()
        run.corp_stage(info)
    except AuthError:
        run.save()
        say("Alpaca afviste nøglerne (401). Tjek .env. Afbryder; fremdrift er gemt.")
        return 1
    except ForbiddenError as exc:
        run.save()
        say("Alpaca nægter adgang (%s). Afbryder; fremdrift er gemt." % exc)
        return 1
    except KeyboardInterrupt:
        run.save()
        say("Afbrudt. Fremdrift er gemt - kør igen for at fortsætte.")
        return 130
    run.save()

    rows = ticker_map.build_and_write(paths, trades)
    spy = read_daily(paths.daily_file(SPY))
    with_data = [s for s in symbols if read_daily(paths.daily_file(s))]
    matched = sum(1 for r in rows if r["price_symbol"])
    meta = {
        "feed": client.feed, "feed_fallback_to_iex": client.fell_back,
        "start_used": run.start, "end": run.end,
        "spy_first_date": spy[0][0].isoformat(), "spy_last_date": spy[-1][0].isoformat(),
        "symbols_total": len(symbols), "symbols_with_data": len(with_data),
        "symbols_without_data": len(symbols) - len(with_data),
        "filing_tickers": len(rows), "filing_tickers_matched": matched,
        "filing_tickers_unmatched": len(rows) - matched,
        "symbols_failed_this_run": sorted(run.failed),
        "api_calls_this_run": client.n_calls, "http_429_this_run": client.n_429,
        "dropped_bars_this_run": run.dropped, "run_at": now_iso(),
    }
    paths.meta.write_text(json.dumps(meta, indent=1), encoding="utf-8")

    say("")
    say("Færdig. Feed: %s. SPY-data fra %s til %s." % (meta["feed"].upper(), meta["spy_first_date"],
                                                      meta["spy_last_date"]))
    say("Symboler med kurser: %d, uden: %d. Tickere i handler koblet: %d af %d."
        % (meta["symbols_with_data"], meta["symbols_without_data"], matched, len(rows)))
    say("API-kald i denne kørsel: %d (heraf %d gange 429)." % (client.n_calls, client.n_429))
    if run.failed:
        say("%d symboler fejlede og hentes ved næste kørsel - kør scriptet igen." % len(run.failed))
    say("Næste skridt: python3 prices/coverage.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
