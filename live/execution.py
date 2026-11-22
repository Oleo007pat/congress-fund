"""Order handling for the live fund: trading windows, idempotent order submission, fill sync, lots, exits.

Lots (state["lots"][lot_id]) hold one copied buy each:
  lot_id, doc_id, member, member_canon, owner, raw_ticker, symbol, filing_date, txn_date, amount_mid, exit_rule,
  status (open | closed | closed_external), qty (current), entry_qty, entry_price, entry_date, entry_order_id,
  planned_exit, exit_attempts, member_sale (None | {doc_id, filing_date, txn_date}), spy_entry,
  exit_price, exit_date, spy_exit, exit_order_id.  simulated=True marks a paper-only lot (see live/simfill.py).
Pending orders (state["pending_orders"][client_order_id]) hold submitted, not yet final orders.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd

from live.broker import DuplicateClientOrderId, OrderRejected
from live.safety import redact
from live.signals import whole_shares
from live import strategy as strat
from prices.alpaca_client import AlpacaError

OPG_CUTOFF = timedelta(minutes=3)     # Alpaca accepts opg until ~09:28 ET
OPG_WINDOW_START = dtime(19, 0)       # ... and only from 19:00 ET (orders submitted 09:28-19:00 ET are rejected)
OPG_WINDOW_END = dtime(9, 28)
CLS_DEAD_START = dtime(15, 50)        # cls orders are rejected 15:50-19:00 ET, queued for the next session after 19:00
ET = ZoneInfo("America/New_York")
CLS_CUTOFF = timedelta(minutes=12)    # ... and cls until ~15:50 ET
FAILED = {"canceled", "expired", "rejected"}
TIF_HINT = re.compile(r"fraction|notional|time.in.force|\bopg\b|\bcls\b|\bday\b|market on", re.I)
PENDING_MAX_AGE_DAYS = 5


@dataclass
class Windows:
    today: str                 # ET date of the clock timestamp
    opg_session: str | None    # date of the session a market-on-open order submitted now targets
    opg_reason: str
    cls_session: str | None
    cls_reason: str


def _parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def trading_windows(clock: dict, now: datetime) -> Windows:
    """Target session for opg/cls orders from the Alpaca clock.  Market closed (pre-open, weekend, holiday):
    the next session is the target, but opg only inside Alpaca's window 19:00-09:28 ET.  Market open: opg is impossible; cls only until shortly before the close."""
    today = str(clock["timestamp"])[:10]
    nxt_open, nxt_close = _parse_ts(clock["next_open"]), _parse_ts(clock["next_close"])
    if clock.get("is_open"):
        opg_s, opg_r = None, "markedet er åbent: market-on-open kan ikke sendes"
        if now < nxt_close - CLS_CUTOFF:
            cls_s, cls_r = str(clock["next_close"])[:10], ""
        else:
            cls_s, cls_r = None, "for sent til market-on-close i dag"
    else:
        et = now.astimezone(ET).time()
        if CLS_DEAD_START <= et < OPG_WINDOW_START:
            cls_s, cls_r = None, "market-on-close modtages ikke mellem 15:50 og 19:00 ET"
        else:
            cls_s, cls_r = str(clock["next_open"])[:10], ""
        if now >= nxt_open - OPG_CUTOFF:
            opg_s, opg_r = None, "for sent til market-on-open (lukket for opg-ordrer lige før åbning)"
        elif OPG_WINDOW_END <= et < OPG_WINDOW_START:
            opg_s, opg_r = None, ("for tidligt til market-on-open: Alpaca modtager opg-ordrer fra kl. 19:00 til "
                                  "09:28 ET (kl. %s ET nu)" % et.strftime("%H:%M"))
        else:
            opg_s, opg_r = str(clock["next_open"])[:10], ""
    return Windows(today, opg_s, opg_r, cls_s, cls_r)


def planned_exit_date(entry_date: str, rule: str) -> str | None:
    """Backtest R5b: entry date + N calendar months (the exit is the close on/after it).  None = member_sells."""
    m = {"hold_3m": 3, "hold_6m": 6, "hold_12m": 12}.get(rule)
    if m is None:
        return None
    return str((pd.Timestamp(entry_date) + pd.DateOffset(months=m)).date())


def exit_due(lot: dict, session_date: str) -> str | None:
    """Reason string if the lot should be sold in the session, else None.
    Fixed hold: on/after the planned exit date.  member_sells: once a qualifying member sale is recorded."""
    if lot.get("status") != "open":
        return None
    if lot["exit_rule"] == "member_sells":
        return "medlem har solgt" if lot.get("member_sale") else None
    if lot.get("planned_exit") and lot["planned_exit"] <= session_date:
        return "planlagt exit-dato nået (%s)" % lot["planned_exit"]
    return None


def fmt_qty(q: float) -> str:
    return f"{q:.9f}".rstrip("0").rstrip(".")


def _f(x, default=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


class Executor:
    def __init__(self, broker, state: dict, cfg: dict, now: datetime, dry_run: bool, log, persist,
                 existing_orders: dict | None = None, positions: dict | None = None, spy_price=None):
        self.broker, self.state, self.cfg, self.now = broker, state, cfg, now
        self.dry_run, self.log, self.persist = dry_run, log, persist
        self.existing = existing_orders if existing_orders is not None else {}
        self.positions = positions if positions is not None else {}     # symbol -> {"qty", "qty_available", ...}
        self.spy_price = spy_price or (lambda d, kind: None)
        self.orders_sent = 0
        self.orders_planned = 0

    # ------------------------------------------------------------------ helpers
    def _lookup(self, cid: str) -> dict | None:
        if cid in self.existing:
            return self.existing[cid]
        try:
            return self.broker.order_by_client_id(cid)
        except AlpacaError:
            return None

    def committed_cash(self) -> float:
        return (sum(_f(p.get("est_cost")) for p in self.state["pending_orders"].values() if p["side"] == "buy")
                + sum(_f(p.get("target")) for p in self.state.get("sim_pending", {}).values()))

    def committed_by_symbol(self, symbol: str) -> float:
        return (sum(_f(p.get("est_cost")) for p in self.state["pending_orders"].values()
                    if p["side"] == "buy" and p["symbol"] == symbol)
                + sum(_f(p.get("target")) for p in self.state.get("sim_pending", {}).values() if p["symbol"] == symbol))

    def _adopt(self, pend: dict, order: dict) -> None:
        pend["order_id"] = order.get("id", "")
        self.state["pending_orders"][pend["client_order_id"]] = pend

    # ------------------------------------------------------------------ buys
    def place_buy(self, plan: dict) -> dict:
        """plan: cid, symbol, price (est.), target (USD notional after caps/cash), fractionable, seed (lot fields),
        session (target session date).  Returns {"result": submitted|already|dry_run|rejected|error, ...}."""
        cid, sym, price, target = plan["cid"], plan["symbol"], plan["price"], plan["target"]
        buf = 1.0 + self.cfg["orders"]["gap_buffer_pct"] / 100.0
        if cid in self.state["lots"] or cid in self.state["pending_orders"]:
            return {"result": "already", "reason": "ordren er allerede sendt (idempotens)", "cid": cid}
        found = self._lookup(cid)
        pend_base = {"client_order_id": cid, "lot_id": cid, "side": "buy", "symbol": sym, "seed": plan["seed"],
                     "submitted_at": self.now.isoformat(timespec="seconds"), "session": plan.get("session", "")}
        if found:                                   # earlier crashed run: adopt the existing order
            self._adopt({**pend_base, "mode": "adopted", "est_cost": target * buf, "notional": target,
                         "qty": found.get("qty"), "tif": found.get("time_in_force", "")}, found)
            self.persist()
            return {"result": "already", "reason": "ordren fandtes allerede hos Alpaca (idempotens)", "cid": cid,
                    "order_id": found.get("id", "")}
        mode = self.cfg["orders"]["mode"]
        caps = self.state["capabilities"]
        attempts = []
        if mode in ("auto", "notional") and plan.get("fractionable") and caps.get("notional_opg") is not False:
            attempts.append("notional")
        if mode in ("auto", "whole_shares"):
            attempts.append("whole")
        if not attempts:
            return {"result": "rejected", "reason": "brøkdelsordrer (notional) med opg understøttes ikke af Alpaca "
                    "(orders.mode = notional)", "cid": cid}
        last_reason = ""
        for att in attempts:
            if att == "notional":
                if target < 1.0:
                    last_reason = "beløb under 1 USD"
                    continue
                target = math.floor(target * 100) / 100          # whole cents, rounded DOWN (never above cash)
                kw, est, qty = {"notional": f"{target:.2f}"}, target * buf, None
            else:
                qty = whole_shares(target, price)
                if qty < 1:
                    last_reason = ("for dyrt for hele aktier: 1 aktie koster %.2f USD, målbeløb %.2f USD"
                                   % (price, target))
                    continue
                kw, est = {"qty": str(qty)}, qty * price * buf
            self.orders_planned += 1
            if self.dry_run:
                return {"result": "dry_run", "reason": "dry-run: ordre ikke sendt", "cid": cid, "mode": att,
                        "notional": target if att == "notional" else qty * price, "qty": qty, "tif": "opg"}
            try:
                order = self.broker.submit_order(sym, "buy", "opg", cid, **kw)
            except DuplicateClientOrderId:
                order = self._lookup(cid)
                if not order:
                    return {"result": "error", "reason": "duplikeret client_order_id, men ordren blev ikke fundet",
                            "cid": cid}
            except OrderRejected as exc:
                msg = redact(exc)
                if att == "notional" and TIF_HINT.search(msg):
                    caps["notional_opg"] = False        # remembered: do not try fractional opg again
                    self.persist()
                    last_reason = "notional+opg afvist (%s); prøver hele aktier" % msg[:120]
                    continue
                return {"result": "rejected", "reason": "ordre afvist af Alpaca: %s" % msg[:200], "cid": cid}
            except AlpacaError as exc:
                order = self._lookup(cid)               # a network error may still have created the order
                if not order:
                    return {"result": "error", "reason": "Alpaca-fejl ved ordre: %s" % redact(exc)[:200], "cid": cid}
            if att == "notional" and caps.get("notional_opg") is None:
                caps["notional_opg"] = True
            self.orders_sent += 1
            self._adopt({**pend_base, "mode": att, "est_cost": est, "notional": kw.get("notional"),
                         "qty": kw.get("qty"), "tif": "opg"}, order)
            self.persist()
            return {"result": "submitted", "cid": cid, "mode": att, "notional": target if att == "notional" else None,
                    "qty": qty, "tif": "opg", "order_id": order.get("id", ""), "status": order.get("status", ""),
                    "est_cost": est,
                    "reason": ("fallback til hele aktier: " + last_reason) if last_reason and att == "whole" else ""}
        return {"result": "rejected", "reason": last_reason or "ingen gyldig ordretype", "cid": cid}

    # ------------------------------------------------------------------ fills
    def sync_pending(self) -> list[str]:
        """Update pending orders from Alpaca: fills become lots (buys) or close lots (sells).
        Returns Danish info lines."""
        notes = []
        for cid, p in list(self.state["pending_orders"].items()):
            try:
                o = self._lookup(cid)
            except AlpacaError:
                continue
            age = (self.now - _parse_ts(p["submitted_at"])).days
            if o is None:
                if age > PENDING_MAX_AGE_DAYS and not self.dry_run:
                    notes.append(f"ordre {cid} findes ikke hos Alpaca efter {age} dage: fjernet fra afventende")
                    self._fail(cid, p, "ordren findes ikke hos Alpaca")
                continue
            status, fq = o.get("status", ""), _f(o.get("filled_qty"))
            if status == "filled" or (status in FAILED and fq > 0):
                self._fill(cid, p, o)
            elif status in FAILED:
                notes.append(f"ordre {cid} {status}: ingen handel")
                self._fail(cid, p, f"ordre {status}", o)
            else:
                p["order_id"] = o.get("id", p.get("order_id", ""))
        self.persist()
        return notes

    def _fill(self, cid: str, p: dict, o: dict) -> None:
        qty, price = _f(o.get("filled_qty")), _f(o.get("filled_avg_price"))
        fdate = str(o.get("filled_at") or "")[:10] or self.now.date().isoformat()
        self.log(kind="fill", client_order_id=cid, order_id=o.get("id", ""), symbol=p["symbol"], side=p["side"],
                 tif=o.get("time_in_force", ""), order_status=o.get("status", ""), filled_qty=qty, fill_price=price,
                 fill_time=o.get("filled_at", ""), member=p.get("seed", {}).get("member", ""),
                 doc_id=p.get("seed", {}).get("doc_id", ""), ticker=p.get("seed", {}).get("raw_ticker", ""),
                 reason=("køb udført" if p["side"] == "buy" else "salg udført"))
        if p["side"] == "buy":
            seed = dict(p["seed"])
            lot = {**seed, "lot_id": p["lot_id"], "status": "open", "qty": qty, "entry_qty": qty,
                   "entry_price": price, "entry_date": fdate, "entry_order_id": o.get("id", ""),
                   "planned_exit": planned_exit_date(fdate, seed["exit_rule"]), "exit_attempts": 0,
                   "member_sale": p.get("member_sale"), "spy_entry": self.spy_price(fdate, "open")}
            self.state["lots"][p["lot_id"]] = lot
        else:
            lot = self.state["lots"].get(p["lot_id"])
            if lot:
                lot["qty"] = max(0.0, _f(lot["qty"]) - qty)
                if lot["qty"] < 1e-6:
                    lot.update({"status": "closed", "exit_price": price, "exit_date": fdate,
                                "exit_order_id": o.get("id", ""), "spy_exit": self.spy_price(fdate, "close")})
                else:
                    lot["exit_attempts"] = lot.get("exit_attempts", 0) + 1
        self.state["pending_orders"].pop(cid, None)

    def _fail(self, cid: str, p: dict, why: str, o: dict | None = None) -> None:
        self.log(kind="fill", client_order_id=cid, order_id=(o or {}).get("id", ""), symbol=p["symbol"],
                 side=p["side"], order_status=(o or {}).get("status", "removed"), filled_qty=0, reason=why,
                 member=p.get("seed", {}).get("member", ""), doc_id=p.get("seed", {}).get("doc_id", ""))
        if p["side"] == "sell":
            lot = self.state["lots"].get(p["lot_id"])
            if lot:
                lot["exit_attempts"] = lot.get("exit_attempts", 0) + 1     # next try gets a new client_order_id
        self.state["pending_orders"].pop(cid, None)

    # ------------------------------------------------------------------ member sales
    def record_member_sale(self, member_canon: str, raw_ticker: str, sale_txn: str, sale: dict) -> int:
        """A member sale (same member and filing ticker, sale transaction date >= lot's buy transaction date)
        marks open lots and pending buys for exit under the member_sells rule.  Returns lots marked."""
        n = 0
        for lot in self.state["lots"].values():
            if (lot.get("status") == "open" and lot["member_canon"] == member_canon and lot["raw_ticker"] == raw_ticker
                    and sale_txn >= lot["txn_date"] and not lot.get("member_sale")):
                lot["member_sale"] = sale
                n += 1
        for p in self.state["pending_orders"].values():
            s = p.get("seed", {})
            if (p["side"] == "buy" and s.get("member_canon") == member_canon and s.get("raw_ticker") == raw_ticker
                    and sale_txn >= s.get("txn_date", "9999") and not p.get("member_sale")):
                p["member_sale"] = sale
                n += 1
        return n

    # ------------------------------------------------------------------ exits
    def run_exits(self, windows: Windows) -> list[str]:
        """Submit sells for due lots: hold rules with market-on-close (cls) in the target session, member_sells with
        market-on-open (opg).  Missed windows defer to the next run."""
        notes = []
        pending_sell_lots = {p["lot_id"] for p in self.state["pending_orders"].values() if p["side"] == "sell"}
        for lot_id, lot in sorted(self.state["lots"].items()):
            if lot_id in pending_sell_lots or lot.get("simulated"):     # simulated lots exit in live/simfill.py
                continue
            member_sells = lot["exit_rule"] == "member_sells"
            session = windows.opg_session if member_sells else windows.cls_session
            if session is None:
                if exit_due(lot, windows.today):
                    notes.append(f"exit af {lot['symbol']} udskudt: {windows.opg_reason if member_sells else windows.cls_reason}")
                continue
            why = exit_due(lot, session)
            if not why:
                continue
            notes.append(self._sell(lot, "opg" if member_sells else "cls", session, why))
        self.persist()
        return [n for n in notes if n]

    def _sell(self, lot: dict, tif: str, session: str, why: str) -> str:
        sym = lot["symbol"]
        pos = self.positions.get(sym)
        base = {"member": lot["member"], "doc_id": lot["doc_id"], "ticker": lot["raw_ticker"], "symbol": sym,
                "side": "sell", "tif": tif}
        if pos is None:
            lot["status"] = "closed_external"
            self.log(kind="order", reason="position findes ikke hos Alpaca (afnoteret/solgt udefra?); lot lukket "
                     "uden ordre", order_status="none", **base)
            return f"{sym}: position findes ikke; lot lukket uden ordre"
        qty = min(_f(lot["qty"]), _f(pos.get("qty_available", pos.get("qty"))))
        if qty <= 0:
            self.log(kind="order", reason="ingen ledige aktier at sælge (åben ordre?); prøver igen", order_status="none", **base)
            return f"{sym}: ingen ledige aktier at sælge"
        cid = f"{lot['lot_id']}-s{lot.get('exit_attempts', 0)}"
        pend = {"client_order_id": cid, "lot_id": lot["lot_id"], "side": "sell", "symbol": sym, "seed": {
                "member": lot["member"], "doc_id": lot["doc_id"], "raw_ticker": lot["raw_ticker"]},
                "submitted_at": self.now.isoformat(timespec="seconds"), "session": session, "qty": fmt_qty(qty), "tif": tif,
                "est_cost": 0.0}
        found = self._lookup(cid)
        if found:
            self._adopt(pend, found)
            return ""
        self.orders_planned += 1
        if self.dry_run:
            self.log(kind="order", reason=f"dry-run: salg ikke sendt ({why})", order_status="dry_run", qty=fmt_qty(qty), **base)
            return f"{sym}: dry-run, salg af {fmt_qty(qty)} ville blive sendt ({tif})"
        used = tif
        try:
            try:
                order = self.broker.submit_order(sym, "sell", tif, cid, qty=fmt_qty(qty))
            except OrderRejected as exc:
                msg = redact(exc)
                if TIF_HINT.search(msg) and tif != "day":
                    used = "day"            # documented fallback (fractional lots): sells at the open, not the close
                    order = self.broker.submit_order(sym, "sell", "day", cid, qty=fmt_qty(qty))
                else:
                    raise
        except DuplicateClientOrderId:
            order = self._lookup(cid)
            if not order:
                return f"{sym}: duplikeret ordre-id, ordre ikke fundet"
        except (OrderRejected, AlpacaError) as exc:
            self.log(kind="order", reason=f"salgsordre afvist/fejlet: {redact(exc)[:200]}; prøver igen næste kørsel",
                     order_status="rejected", qty=fmt_qty(qty), **base)
            return f"{sym}: salgsordre afvist ({redact(exc)[:100]})"
        self.orders_sent += 1
        pend["tif"] = used
        self._adopt(pend, order)
        pos["qty_available"] = _f(pos.get("qty_available", pos.get("qty"))) - qty
        self.log(kind="order", reason=f"salg sendt ({why})" + (" - fallback til day-ordre" if used != tif else ""),
                 order_status=order.get("status", ""), qty=fmt_qty(qty), client_order_id=cid,
                 order_id=order.get("id", ""), **{**base, "tif": used})
        return ""

    # ------------------------------------------------------------------ reconciliation
    def reconcile(self) -> list[str]:
        """Compare open lots with Alpaca positions; reports differences only (never trades on them)."""
        out = []
        by_sym: dict[str, float] = {}
        for lot in self.state["lots"].values():
            if lot["status"] == "open" and not lot.get("simulated"):     # simulated lots have no Alpaca position
                by_sym[lot["symbol"]] = by_sym.get(lot["symbol"], 0.0) + _f(lot["qty"])
        for sym, q in by_sym.items():
            pq = _f((self.positions.get(sym) or {}).get("qty"))
            if abs(pq - q) > 1e-6 and sym in self.positions:
                out.append(f"afstemning: {sym} lots {q:g} mod position {pq:g}")
            elif sym not in self.positions:
                out.append(f"afstemning: {sym} har åbne lots men ingen position")
        for sym in self.positions:
            if sym not in by_sym and not any(p["symbol"] == sym for p in self.state["pending_orders"].values()):
                out.append(f"afstemning: position i {sym} uden tilhørende lot (ikke oprettet af fonden)")
        return out
