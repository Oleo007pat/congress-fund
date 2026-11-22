"""Daily runner for the live paper fund.

    python3 -m live.run_daily [--dry-run] [--start-date YYYY-MM-DD]

Detects new US House PTRs, applies the locked strategy (live/strategy.json) and trades on the Alpaca PAPER
account.  Refuses to run unless ALPACA_BASE_URL is exactly https://paper-api.alpaca.markets.

Exit codes: 0 ok | 1 unexpected error | 2 refused/config error | 3 house.gov unreachable | 4 Alpaca error.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from live import house, simfill, store, strategy as strat
from live import signals as sg
from live.broker import Broker
from live.execution import Executor, planned_exit_date, trading_windows
from live.member_filter import MemberFilter, lots_history
from live.safety import NotPaperError, redact, validate_paper_url
from live.site import build_live_json
from prices.alpaca_client import AlpacaError
from prices.ticker_map import candidate_symbols
from src.backtest.engine import add_canonical_member, load_trades, select_buy_signals, select_sales

ROOT = Path(__file__).resolve().parent.parent
PREFIXES_SUSPECT = ("datomistanke", "dublet", "slettet")
BARS_LOOKBACK_DAYS = 10
STALE_LAST_DAYS = 5


class Log:
    """In-memory trades_log rows for this run."""

    def __init__(self, now: datetime, dry_run: bool):
        self.rows: list[dict] = []
        self.ts = now.isoformat(timespec="seconds")
        self.dry = "1" if dry_run else "0"

    def __call__(self, **fields) -> None:
        row = {k: ("" if v is None else v) for k, v in fields.items()}
        row.setdefault("on_site", "0")
        row.update(ts_utc=self.ts, dry_run=self.dry)
        self.rows.append(row)


def _f(x, default=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def bars_window(now: datetime, start_date: str) -> tuple[str, str]:
    """Daily bars up to 20 minutes ago (plans without real-time SIP data refuse more recent SIP queries)."""
    end = (now - timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"{start_date}T00:00:00Z", end


def fetch_bars(broker, symbols: list[str], start_date: str, now: datetime) -> dict:
    out: dict = {}
    start, end = bars_window(now, start_date)
    syms = sorted(set(symbols))
    for i in range(0, len(syms), 50):
        out.update(broker.bars(syms[i:i + 50], start, end))
    return out


def clean_row(r: dict) -> dict:
    return {k: (None if (isinstance(v, float) and v != v) else v) for k, v in r.items()}


def row_cid(doc_id: str, row_in_doc: int, r: dict) -> str:
    key = "|".join(str(x) for x in (doc_id, row_in_doc, r.get("owner"), r.get("ticker"), r.get("type"),
                                    r.get("transaction_date"), r.get("amount_low"), r.get("amount_high"),
                                    r.get("sub_holding"), r.get("asset")))
    return f"cf-{doc_id}-{hashlib.sha1(key.encode()).hexdigest()[:10]}"


class Run:
    """State shared by the steps of one run."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


# ----------------------------------------------------------------------------- filings -> signals

def process_filings(run: Run, index_rows: list[dict], session_date: str) -> None:
    st, cfg, log, now = run.state, run.cfg, run.log, run.now
    seen = set(st["seen_doc_ids"])
    new = house.select_new_filings(index_rows, st["start_date"], session_date, seen)
    run.stats["filings_checked"] = len(new)
    parsed_by_doc: dict[str, list[dict]] = {}
    pdf_session = run.house_session
    for fr in new:
        doc = fr["doc_id"]
        meta = {"doc_id": doc, "member": f"{fr['first']} {fr['last']}".strip(), "filing_date": fr["filing_date"]}
        if doc.startswith(house.SCANNED_PREFIXES):
            st["seen_doc_ids"][doc] = {"filing_date": fr["filing_date"], "status": "scanned_paper", "seen_at": log.ts}
            log(kind="info", decision="ignored", reason="ikke læsbar: scannet papirindberetning", **meta)
            run.stats["unreadable"] += 1
            continue
        pdf, status = house.download_pdf(pdf_session, fr, run.sleep)
        res = None
        if pdf is not None:
            try:
                res = house.parse_filing(run.root, fr, pdf)
            except Exception as exc:                      # a broken PDF must not crash the run
                res = {"status": "parse_error", "trades": [], "warnings": [type(exc).__name__]}
        if res is None or res["status"] == "parse_error":
            n = st["doc_failures"].get(doc, 0) + 1
            st["doc_failures"][doc] = n
            why = status if res is None else "; ".join(res["warnings"])[:150]
            if n >= house.MAX_DOC_FAILURES:
                st["seen_doc_ids"][doc] = {"filing_date": fr["filing_date"], "status": "failed", "seen_at": log.ts}
            log(kind="info", decision="ignored", reason=f"indberetning kunne ikke hentes/læses (forsøg {n}): {why}", **meta)
            continue
        if res["status"] == "unreadable":
            st["seen_doc_ids"][doc] = {"filing_date": fr["filing_date"], "status": "unreadable", "seen_at": log.ts}
            log(kind="info", decision="ignored", reason="ikke læsbar: PDF uden tekstlag (scannet/håndskrevet)", **meta)
            run.stats["unreadable"] += 1
            continue
        parsed_by_doc[doc] = res["trades"]
        st["seen_doc_ids"][doc] = {"filing_date": fr["filing_date"], "status": "parsed", "seen_at": log.ts}
    if not parsed_by_doc:
        return

    # Duplicates / deleted / date_suspect across ALL stored live rows (backtest finalize logic)
    prior = [r for r in house.read_parsed(run.paths.parsed) if r["doc_id"] not in parsed_by_doc]
    allrows = prior + [t for doc in parsed_by_doc for t in parsed_by_doc[doc]]
    house.finalize(allrows)
    house.write_parsed(run.paths.parsed, allrows)
    df = add_canonical_member(load_trades(run.paths.parsed))
    df["doc_id"] = df["doc_id"].astype(str)
    df["row_in_doc"] = df.groupby("doc_id").cumcount()
    buys = select_buy_signals(df).set_index("row_id")
    sales = select_sales(df).copy()
    sales["doc_id"] = sales["doc_id"].astype(str)
    note_deleted_trades(run, df, parsed_by_doc)
    newdf = df[df["doc_id"].isin(parsed_by_doc)].sort_values(["filing_date", "doc_id", "row_id"])
    retry_docs: set[str] = set()

    # member sales: recorded; they trigger exits under the member_sells rule
    for s in sales[sales["doc_id"].isin(parsed_by_doc)].itertuples():
        run.ex.record_member_sale(s.member_canon, s.ticker, s.transaction_date,
                                  {"doc_id": s.doc_id, "filing_date": s.filing_date, "txn_date": s.transaction_date})

    cands = []
    for r in newdf.to_dict("records"):
        r = clean_row(r)
        base = {"doc_id": r["doc_id"], "member": r["member"], "owner": r["owner"], "ticker": r["ticker"] or "",
                "filing_date": r["filing_date"], "txn_date": r["transaction_date"] or "", "trade_type": r["type"],
                "amount_low": r["amount_low"], "amount_high": r["amount_high"]}
        if r["type"] == "buy":
            rid = r["row_id"]
            if rid in buys.index:
                dr = buys.loc[rid, "drop_reason"]
                if dr:
                    log(kind="signal", decision="ignored", reason=sg.drop_reason_da(dr), on_site="1", **base)
                elif r["filing_status"] == "Amended":
                    # Conservative: the original may pre-date the fund's start (not in the live data), so an
                    # amendment that matches no known filing is not treated as a new signal.
                    log(kind="signal", decision="ignored", on_site="1", reason="ændringsindberetning (Amended) uden kendt "
                        "original i fondens data: ignoreres for ikke at købe en gammel handel to gange", **base)
                else:
                    run.stats["new_signals"] += 1
                    cands.append((r, base))
            else:
                why = str(r["include_reason"])
                log(kind="signal", decision="ignored", reason=why,
                    on_site="1" if why.startswith(PREFIXES_SUSPECT) else "0", **base)
        elif r["type"] in ("sell", "partial_sale"):
            log(kind="signal", decision="ignored", reason="salg registreret (bruges kun til member_sells-exit)", **base)
        else:
            log(kind="signal", decision="ignored", reason="bytte (exchange) understøttes ikke", **base)
    evaluate_candidates(run, cands, sales, session_date, retry_docs)
    for doc in retry_docs:                                   # transient order error: look at the filing again
        st["seen_doc_ids"].pop(doc, None)


def note_deleted_trades(run: Run, df, new_docs: dict) -> None:
    """A new filing deleted a trade we already ordered/hold: logged only (the paper position is kept; documented
    deviation from the backtest, which drops deleted trades)."""
    for r in df[(df["deleted_by"].notna()) & (~df["doc_id"].isin(list(new_docs)))].to_dict("records"):
        r = clean_row(r)
        if r["type"] != "buy" or not r["deleted_by"]:
            continue
        cid = row_cid(r["doc_id"], int(r["row_in_doc"]), r)
        lot, pend = run.state["lots"].get(cid), run.state["pending_orders"].get(cid)
        holder = lot or pend
        if holder is not None and not holder.get("deleted_by"):
            holder["deleted_by"] = str(r["deleted_by"])
            run.log(kind="info", decision="ignored", doc_id=r["doc_id"], member=r["member"], ticker=r["ticker"] or "",
                    reason=f"handlen er slettet af indberetning {r['deleted_by']}; papirpositionen/ordren bevares")


def resolve_symbol(run: Run, raw: str) -> tuple[str | None, dict | None, str]:
    for sym, _m in candidate_symbols(raw):
        if sym not in run.asset_cache:
            run.asset_cache[sym] = run.broker.asset(sym)
        a = run.asset_cache[sym]
        if not a:
            continue
        if a.get("status") != "active" or not a.get("tradable"):
            return None, a, f"{sym} er ikke handelbar hos Alpaca (afnoteret/suspenderet?)"
        if str(a.get("exchange", "")).upper() == "OTC":
            return None, a, f"{sym} handles OTC (ikke understøttet)"
        return sym, a, ""
    return None, None, "ticker ikke fundet hos Alpaca"


def evaluate_candidates(run: Run, cands: list, sales, session_date: str, retry_docs: set) -> None:
    cfg, log, st = run.cfg, run.log, run.state
    x = cfg["skip_rise_pct"]
    xs = "ingen grænse" if x is None else f"grænse {x:g} %"
    max_lag = int(cfg["entry"]["max_entry_lag_days"])
    mf = run.member_filter
    pre = []                                                       # candidates that survive the cheap checks
    for r, base in cands:
        lag = (date.fromisoformat(session_date) - date.fromisoformat(r["filing_date"])).days
        if lag > max_lag:
            log(kind="signal", decision="ignored", on_site="1", reason=f"for sent: indgang ville ligge {lag} dage efter "
                f"indberetningen (maks {max_lag})", **base)
            continue
        if cfg["exit"]["rule"] == "member_sells":
            q = sales[(sales["member_canon"] == r["member_canon"]) & (sales["ticker"] == r["ticker"])
                      & (sales["transaction_date"] >= r["transaction_date"])]
            if len(q):
                log(kind="signal", decision="ignored", on_site="1", reason="medlemmet har allerede indberettet salg "
                    "(backtesten ville lave en rundtur uden afkast)", **base)
                continue
        if mf is not None:
            ok, why = mf.allows(r["member_canon"], run.now.date())
            if not ok:
                log(kind="signal", decision="ignored", on_site="1", reason=why, **base)
                continue
        sym, asset, why = resolve_symbol(run, r["ticker"] or "")
        if sym is None:
            log(kind="signal", decision="ignored", on_site="1", reason=why, **base)
            continue
        pre.append((r, base, sym, asset))
    if not pre:
        return
    start = (date.fromisoformat(min(r["transaction_date"] for r, *_ in pre)) - timedelta(days=BARS_LOOKBACK_DAYS)).isoformat()
    bars = fetch_bars(run.broker, [p[2] for p in pre], start, run.now)
    snaps: dict = {}
    dry_cash_used, dry_sym_used = 0.0, {}
    equity = run.equity
    for r, base, sym, asset in pre:
        b = bars.get(sym, [])
        ref, ref_date, status = sg.reference_close(b, r["transaction_date"])
        extra = {"symbol": sym, "ref_close": ref, "ref_date": ref_date}
        if ref is None:
            log(kind="signal", decision="ignored", on_site="1", reason=f"ingen brugbar referencekurs ({status})", **base, **extra)
            continue
        last, last_date = sg.latest_close(b)
        src = "dagskurs"
        if last is None or (date.fromisoformat(session_date) - date.fromisoformat(last_date)).days > STALE_LAST_DAYS:
            if sym not in snaps:
                try:
                    snaps.update(run.broker.snapshots([sym]))
                except AlpacaError:
                    pass
            last, src = sg.snapshot_price(snaps.get(sym)), "iex-snapshot"
        if last is None:
            log(kind="signal", decision="ignored", on_site="1", reason="ingen aktuel kurs fra Alpaca", **base, **extra)
            continue
        rise = sg.rise_since_reference(last, ref)
        extra.update(last_price=last, rise_pct=round(rise * 100, 2))
        if sg.should_skip(rise, x):
            log(kind="signal", decision="skipped", on_site="1", reason=f"sprunget over: kursen er steget {rise*100:.1f} % siden "
                f"handelsdagen ({xs})", **base, **extra)
            continue
        mid = sg.midpoint(r)
        tgt = sg.target_notional(equity, cfg["weighting"], mid, cfg["sizing"])
        notes = []
        existing = (_f((run.positions.get(sym) or {}).get("market_value")) + run.ex.committed_by_symbol(sym)
                    + dry_sym_used.get(sym, 0.0) + run.sim.value_by_symbol.get(sym, 0.0))
        tgt2, capped = sg.apply_position_cap(tgt, equity, existing, cfg["sizing"])
        if capped:
            notes.append("begrænset af positionsloft")
        if tgt2 < 1.0:
            log(kind="signal", decision="skipped", on_site="1", reason="positionsloft nået (10 % af kapitalen)", **base, **extra)
            continue
        avail = run.cash - run.ex.committed_cash() - dry_cash_used
        tgt3, scaled = sg.apply_cash_limit(tgt2, avail, cfg["orders"]["gap_buffer_pct"])
        if scaled:
            notes.append(f"nedskaleret af kontanter ({tgt2:.0f} -> {max(tgt3, 0):.0f} USD)")
        if tgt3 < 1.0:
            log(kind="signal", decision="skipped", on_site="1", reason="ikke nok kontanter (kun kontanter, ingen gearing)", **base, **extra)
            continue
        cid = row_cid(r["doc_id"], int(r["row_in_doc"]), r)
        plan = {"cid": cid, "symbol": sym, "price": last, "target": tgt3, "fractionable": bool(asset.get("fractionable")),
                "session": run.windows.opg_session, "seed": {
                    "doc_id": r["doc_id"], "member": r["member"], "member_canon": r["member_canon"], "owner": r["owner"],
                    "raw_ticker": r["ticker"], "symbol": sym, "filing_date": r["filing_date"], "txn_date": r["transaction_date"],
                    "amount_mid": mid, "exit_rule": cfg["exit"]["rule"]}}
        res = run.ex.place_buy(plan)
        orderinfo = {"side": "buy", "client_order_id": cid, "tif": res.get("tif", "opg"), "qty": res.get("qty"),
                     "notional": res.get("notional"), "order_id": res.get("order_id", ""), "order_status": res.get("status", res["result"])}
        extra_txt = ("; " + "; ".join(notes)) if notes else ""
        if res["result"] in ("submitted", "already", "dry_run"):
            if res["result"] == "dry_run":
                dry_cash_used += tgt3 * (1 + cfg["orders"]["gap_buffer_pct"] / 100)
                dry_sym_used[sym] = dry_sym_used.get(sym, 0.0) + tgt3
            what = res.get("mode", "")
            log(kind="signal", decision="taken", on_site="1", reason=f"køb ({res['result']}{', ' + what if what else ''}): "
                f"mål {tgt3:.0f} USD, kurs steget {rise*100:+.1f} % ({xs}), kilde {src}{extra_txt}"
                f"{'; ' + res['reason'] if res.get('reason') else ''}", **base, **extra)
            log(kind="order", decision="", reason=res.get("reason") or res["result"], member=r["member"], doc_id=r["doc_id"],
                ticker=r["ticker"], symbol=sym, **orderinfo)
            if res["result"] == "submitted":
                run.stats["orders_sent"] += 1
        elif res["result"] == "rejected" and simfill.is_timing_reject(res["reason"]) and not run.dry_run:
            # The strategy's buy was valid; only the submission time was outside Alpaca's opg window (a scheduling
            # error on our side).  Book it on paper at the official open of the target session (live/simfill.py).
            simfill.queue(st, plan, res["reason"], run.now.isoformat(timespec="seconds"))
            run.ex.persist()
            log(kind="signal", decision="taken", on_site="1", reason=f"køb (simuleret fyld): mål {tgt3:.0f} USD, kurs "
                f"steget {rise*100:+.1f} % ({xs}), kilde {src}{extra_txt}; Alpaca afviste ordren pga. tidspunktet, "
                f"så købet registreres til den officielle åbningskurs d. {plan['session']}", **base, **extra)
            log(kind="order", reason=res["reason"] + " -> simuleret fyld", member=r["member"], doc_id=r["doc_id"],
                ticker=r["ticker"], symbol=sym, **{**orderinfo, "order_status": "rejected"})
        elif res["result"] == "rejected":
            log(kind="signal", decision="ignored", on_site="1", reason=res["reason"], **base, **extra)
            log(kind="order", reason=res["reason"], member=r["member"], doc_id=r["doc_id"], ticker=r["ticker"], symbol=sym,
                **{**orderinfo, "order_status": "rejected"})
        else:                                                   # transient error: retry the filing next run
            retry_docs.add(r["doc_id"])
            log(kind="signal", decision="ignored", on_site="0", reason=f"midlertidig fejl, prøver igen: {res['reason']}", **base, **extra)


# ----------------------------------------------------------------------------- main run

def parse_args(argv):
    p = argparse.ArgumentParser(prog="python3 -m live.run_daily", description="Daglig kørsel af papirfonden.")
    p.add_argument("--dry-run", action="store_true", help="beregn alt og skriv output i sandkassen, men send ingen ordrer")
    p.add_argument("--start-date", metavar="YYYY-MM-DD", help="fondens startdato (kun første kørsel; standard: i dag)")
    p.add_argument("--allow-nonempty-account", action="store_true",
                   help="tillad første kørsel på en konto med eksisterende positioner/ordrer (anbefales ikke)")
    return p.parse_args(argv)


def final_message(run: Run, status: str, extra: list[str]) -> str:
    s = run.stats
    head = {"ok": "Kørslen er gennemført.", "house_unreachable": "House Clerk kunne ikke nås; ingen handler i dag.",
            "alpaca_error": "Alpaca-fejl; ingen handler.", "error": "Uventet fejl; ingen handler gennemført efter fejlen."}.get(status, status)
    bits = [head, f"{s['filings_checked']} nye indberetninger, {s['new_signals']} nye købssignaler, "
            f"{s['orders_sent']} ordrer sendt" + (f" ({run.ex.orders_planned} ville blive sendt)" if run.dry_run else "") + "."]
    if s["unreadable"]:
        bits.append(f"{s['unreadable']} indberetninger var ikke læsbare (scannet) og blev sprunget over.")
    if run.dry_run:
        bits.append("DRY-RUN: ingen ordrer er sendt; resultater ligger i sandkassen.")
    return " ".join(bits + extra)


def run_main(argv=None, env=None, *, root: Path = ROOT, broker=None, house_session=None, now: datetime | None = None,
             sleep=time.sleep) -> int:
    args = parse_args(argv)
    if env is None:
        try:
            from dotenv import load_dotenv
            load_dotenv(Path(root) / ".env")
        except ImportError:
            pass
        env = os.environ
    # ---- 1. safety (before any network access)
    try:
        validate_paper_url(env.get("ALPACA_BASE_URL"))
    except NotPaperError as exc:
        print(f"AFVIST: {exc}", file=sys.stderr)
        return 2
    if broker is None and not (env.get("ALPACA_KEY_ID") and env.get("ALPACA_SECRET_KEY")):
        print("AFVIST: ALPACA_KEY_ID og ALPACA_SECRET_KEY skal være sat.", file=sys.stderr)
        return 2
    paths = store.LivePaths(root, args.dry_run)
    try:
        cfg = strat.load(paths.strategy)
    except strat.StrategyError as exc:
        print(f"AFVIST: {exc}", file=sys.stderr)
        return 2
    if args.start_date:
        try:
            date.fromisoformat(args.start_date)
        except ValueError:
            print("AFVIST: --start-date skal være YYYY-MM-DD.", file=sys.stderr)
            return 2
    now = now or datetime.now(timezone.utc)
    paths.seed_dry_run()
    if broker is None:
        broker = Broker(env["ALPACA_KEY_ID"], env["ALPACA_SECRET_KEY"], env["ALPACA_BASE_URL"], sleep=sleep)
    if house_session is None:
        house_session = house.make_session()

    state = store.load_state(paths.state)
    log = Log(now, args.dry_run)
    stats = {"filings_checked": 0, "new_signals": 0, "orders_sent": 0, "unreadable": 0}
    status, code, extra = "ok", 0, []
    spy_last, spy_by_date = "", {}
    run = Run(root=Path(root), paths=paths, cfg=cfg, state=state, broker=broker, now=now, log=log, stats=stats,
              dry_run=args.dry_run, house_session=house_session, sleep=sleep, asset_cache={}, ex=None, equity=0.0,
              cash=0.0, positions={}, windows=None, member_filter=None, sim=simfill.SimBook(), sim_bars={})

    def persist():
        if run.state is not None:
            store.save_state(paths.state, run.state)

    try:
        account, clock = broker.account(), broker.clock()
        if account.get("trading_blocked") or account.get("account_blocked"):
            raise AlpacaError("kontoen er blokeret hos Alpaca")
        run.equity, run.cash = _f(account.get("equity")), _f(account.get("cash"))
        pos_list = broker.positions()
        run.positions = {p["symbol"]: p for p in pos_list}
        run.windows = trading_windows(clock, now)
        today = now.date().isoformat()
        if state is None:                                           # first run: lock start date and equity
            open_orders = broker.orders(status="open")
            if (pos_list or open_orders) and not args.allow_nonempty_account:
                msg = ("Paperkontoen er ikke tom (positioner eller åbne ordrer). Nulstil kontoen, eller kør med "
                       "--allow-nonempty-account." )
                if not args.dry_run:
                    print("AFVIST: " + msg, file=sys.stderr)
                    return 2
                extra.append("ADVARSEL: " + msg)
            run.state = state = store.new_state(args.start_date or today, run.equity, now.isoformat(timespec="seconds"),
                                                strat.strategy_hash(cfg), cfg["name"])
        else:
            if args.start_date and args.start_date != state["start_date"]:
                extra.append(f"--start-date ignoreret: startdatoen er låst til {state['start_date']}.")
            h = strat.strategy_hash(cfg)
            if h != state["strategy_hash"]:
                state["strategy_changes"].append({"date": today, "hash": h, "name": cfg["name"]})
                state["strategy_hash"], state["strategy_name"] = h, cfg["name"]
                extra.append("ADVARSEL: strategy.json er ændret siden sidst (registreret i state og vist på siden).")
        start = state["start_date"]
        after = (date.fromisoformat(start) - timedelta(days=1)).isoformat() + "T00:00:00Z"
        run.ex_orders = {o["client_order_id"]: o for o in broker.orders(after=after) if o.get("client_order_id")}
        # SPY: last close for the history, and prices for lot bookkeeping
        spy_bars = fetch_bars(broker, ["SPY"], (now.date() - timedelta(days=45)).isoformat(), now).get("SPY", [])
        spy_by_date = {sg._bar_date(b).isoformat(): b for b in spy_bars if sg.valid_bar(b)}
        spy_last = max(spy_by_date) if spy_by_date else ""

        def spy_price(d, kind):
            b = spy_by_date.get(d)
            return float(b["o" if kind == "open" else "c"]) if b else None

        run.ex = Executor(broker, state, cfg, now, args.dry_run, log, persist, run.ex_orders, run.positions, spy_price)
        mfcfg = cfg["member_filter"]
        run.member_filter = MemberFilter(mfcfg, lots_history(state["lots"])) if mfcfg["enabled"] else None
        extra += run.ex.sync_pending()                              # fills -> lots
        # simulated fills (live/simfill.py): resolve queued buys at the official open, exit, value; the fund's
        # equity and cash then include these paper-only positions
        sim_syms = simfill.symbols(state)
        if sim_syms:
            run.sim_bars = fetch_bars(broker, sim_syms, simfill.bars_start(state), now)
            run.sim, notes = simfill.process(state, run.sim_bars, run.windows.today, planned_exit_date, spy_price, log)
            extra += notes
            run.equity += run.sim.equity_adj
            run.cash += run.sim.cash_adj
            persist()

        # ---- 2.-4. filings -> signals -> orders (buys) and exits
        house_ok = True
        if run.windows.opg_session is None:
            extra.append(f"Nye indberetninger afventer: {run.windows.opg_reason}.")
        else:
            try:
                rows = []
                for y in house.index_years(now.date()):
                    rows += house.fetch_index(house_session, y, sleep)
                process_filings(run, rows, run.windows.opg_session)
            except house.HouseUnreachable as exc:
                house_ok, status, code = False, "house_unreachable", 3
                extra.append(str(exc))
        if house_ok:
            extra += run.ex.run_exits(run.windows)
        extra += run.ex.reconcile()
        stats["orders_sent"] = run.ex.orders_sent
        persist()
    except AlpacaError as exc:
        status, code = "alpaca_error", 4
        extra.append(f"Alpaca: {redact(exc)[:200]}")
    except Exception as exc:                                       # never leak secrets, never crash silently
        status, code = "error", 1
        extra.append(f"{type(exc).__name__}: {redact(exc)[:200]}")
    if status not in ("ok", "house_unreachable"):
        spy_last = ""                                              # no history row after a failed run
    return finish(run, args, status, code, extra, spy_last, spy_by_date)


def finish(run: Run, args, status: str, code: int, extra: list[str], spy_last: str, spy_by_date: dict) -> int:
    """Records: history, state, trades_log, site JSON (also on failures, so the site shows the status)."""
    paths, state, cfg, now = run.paths, run.state, run.cfg, run.now
    last_run = {"time_utc": now.isoformat(timespec="seconds"), "status": "dry_run" if (args.dry_run and status == "ok") else status,
                "filings_checked": run.stats["filings_checked"], "new_signals": run.stats["new_signals"],
                "orders_sent": run.stats["orders_sent"], "message": "", "dry_run": bool(args.dry_run)}
    last_run["message"] = final_message(run, status, extra)
    if state is None:                                              # nothing to record yet
        print(last_run["message"], file=sys.stderr)
        return code
    state["last_run"] = last_run
    history = store.read_csv(paths.history)
    if spy_last and status in ("ok", "house_unreachable"):
        history = store.upsert_history(paths.history, {
            "date": now.date().isoformat(), "equity": f"{run.equity:.2f}", "cash": f"{run.cash:.2f}",
            "n_positions": len(set(run.positions) | run.sim.open_symbols),
            "spy_close": f"{float(spy_by_date[spy_last]['c']):.4f}", "spy_close_date": spy_last,
            "sim_pnl": f"{run.sim.equity_adj:.2f}" if run.sim_bars else ""})
        if run.sim_bars and not args.dry_run:
            # earlier rows written before a simulated lot existed: add its P&L at that row's market close
            changed = False
            for h in history:
                if h.get("sim_pnl") not in (None, "") or not h.get("spy_close_date"):
                    continue
                pnl = simfill.pnl_on(state, run.sim_bars, h["spy_close_date"])
                if pnl is None:
                    continue
                h["equity"] = f"{_f(h['equity']) + pnl:.2f}"
                h["sim_pnl"] = f"{pnl:.2f}"
                changed = True
            if changed:
                history = store.rewrite_history(paths.history, history)
    store.save_state(paths.state, state)
    store.append_csv(paths.trades_log, store.LOG_FIELDS, run.log.rows)
    log_rows = store.read_csv(paths.trades_log)
    doc = build_live_json(state, cfg, history, run.positions, log_rows, last_run, now, run.sim.price_by_symbol)
    store.write_json(paths.live_json, doc)
    print(last_run["message"])
    return code


def main() -> int:
    return run_main()


if __name__ == "__main__":
    sys.exit(main())
