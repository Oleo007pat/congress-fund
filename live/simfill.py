"""Simulated fills for buys that Alpaca rejected only because of a scheduling error on the fund's side.

Why: market-on-open (opg) orders are accepted by Alpaca only between 19:00 and 09:28 ET.  When GitHub delays the
scheduled run past that window, a valid signal would be lost although the strategy says "buy at the next open".
Such a buy is queued here and booked at the OFFICIAL opening price of the target session (Alpaca daily bar,
the same price source as the backtest).  It is a paper position that lives only in state.json, clearly marked
"simulated" everywhere it is shown.  Nothing is invented: if Alpaca has no bar for the session, the position is
not created (and after a grace period the queue entry is dropped and logged).

state["sim_pending"][cid] = {cid, symbol, target, session, seed, reason, queued_at}
Simulated lots are ordinary lots with "simulated": True.  They are sold on paper at the CLOSE of the first
session on or after planned_exit (same rule as the real cls exit), or at the latest close once a member sale
is recorded (member_sells rule).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from live.signals import _bar_date, valid_bar, whole_shares

TIMING_REJECT = re.compile(r"must be submitted after|submitted (before|after) \d", re.I)
SIM_DROP_AFTER_DAYS = 10          # no bar for the session after this many days -> drop (logged, never guessed)


def is_timing_reject(reason: str) -> bool:
    return bool(TIMING_REJECT.search(reason or ""))


def queue(state: dict, plan: dict, reason: str, now_iso: str) -> None:
    state.setdefault("sim_pending", {})[plan["cid"]] = {
        "cid": plan["cid"], "symbol": plan["symbol"], "target": round(float(plan["target"]), 2),
        "session": plan["session"], "seed": dict(plan["seed"]), "reason": reason[:200], "queued_at": now_iso}


def sim_lots(state: dict) -> list[dict]:
    return [l for l in state["lots"].values() if l.get("simulated")]


def symbols(state: dict) -> list[str]:
    s = {p["symbol"] for p in state.get("sim_pending", {}).values()}
    s |= {l["symbol"] for l in sim_lots(state)}
    return sorted(s)


def earliest_date(state: dict) -> str | None:
    ds = [p["session"] for p in state.get("sim_pending", {}).values()]
    ds += [l["entry_date"] for l in sim_lots(state) if l.get("entry_date")]
    return min(ds) if ds else None


def _series(bars: list[dict]) -> list[tuple[str, dict]]:
    return sorted(((_bar_date(b).isoformat(), b) for b in bars if valid_bar(b)), key=lambda t: t[0])


@dataclass
class SimBook:
    unrealized: float = 0.0          # open simulated lots: qty x (last close - entry)
    realized: float = 0.0            # closed simulated lots: qty x (exit - entry)
    open_cost: float = 0.0           # qty x entry of open simulated lots (cash tied up on paper)
    value_by_symbol: dict = field(default_factory=dict)
    price_by_symbol: dict = field(default_factory=dict)
    open_symbols: set = field(default_factory=set)

    @property
    def equity_adj(self) -> float:
        return self.unrealized + self.realized

    @property
    def cash_adj(self) -> float:
        return self.realized - self.open_cost


def process(state: dict, bars: dict, today: str, planned_exit_date, spy_price, log) -> tuple[SimBook, list[str]]:
    """Resolve queued simulated buys, close due simulated lots, and value the open ones.  Returns (book, notes)."""
    notes = []
    series = {s: _series(b) for s, b in bars.items()}
    pend = state.setdefault("sim_pending", {})
    for cid, p in list(pend.items()):
        sym, sess = p["symbol"], p["session"]
        bar = dict(series.get(sym, [])).get(sess)
        if bar is None:
            if (date.fromisoformat(today) - date.fromisoformat(sess)).days > SIM_DROP_AFTER_DAYS:
                pend.pop(cid)
                notes.append(f"simuleret køb {sym}: ingen åbningskurs for {sess} hos Alpaca; droppet")
                log(kind="fill", client_order_id=cid, symbol=sym, side="buy", order_status="sim_dropped", filled_qty=0,
                    member=p["seed"].get("member", ""), doc_id=p["seed"].get("doc_id", ""),
                    reason=f"simuleret fyld droppet: ingen åbningskurs for {sess}")
            continue
        price = float(bar["o"])
        qty = whole_shares(p["target"], price)
        pend.pop(cid)
        if qty < 1:
            notes.append(f"simuleret køb {sym}: 1 aktie ({price:.2f}) over målbeløbet; intet køb")
            log(kind="fill", client_order_id=cid, symbol=sym, side="buy", order_status="sim_none", filled_qty=0,
                member=p["seed"].get("member", ""), doc_id=p["seed"].get("doc_id", ""),
                reason="simuleret fyld: for dyrt for hele aktier")
            continue
        seed = dict(p["seed"])
        state["lots"][cid] = {**seed, "lot_id": cid, "status": "open", "qty": float(qty), "entry_qty": float(qty),
                              "entry_price": price, "entry_date": sess, "entry_order_id": "simulated",
                              "planned_exit": planned_exit_date(sess, seed["exit_rule"]), "exit_attempts": 0,
                              "member_sale": None, "spy_entry": spy_price(sess, "open"), "simulated": True,
                              "sim_reason": p.get("reason", "")}
        notes.append(f"simuleret køb {sym}: {qty} stk. til åbningskursen {price:.2f} d. {sess}")
        log(kind="fill", client_order_id=cid, order_id="simulated", symbol=sym, side="buy", tif="opg",
            order_status="sim_filled", filled_qty=qty, fill_price=price, fill_time=sess,
            member=seed.get("member", ""), doc_id=seed.get("doc_id", ""), ticker=seed.get("raw_ticker", ""),
            reason="simuleret fyld til officiel åbningskurs (ordren blev afvist pga. tidspunkt)")

    book = SimBook()
    for lot in sim_lots(state):
        ser = series.get(lot["symbol"], [])
        if lot["status"] == "open":
            due_bar = None
            if lot.get("planned_exit"):
                due_bar = next(((d, b) for d, b in ser if d >= lot["planned_exit"]), None)
            elif lot.get("member_sale") and ser:
                due_bar = ser[-1]
            if due_bar:
                d, b = due_bar
                lot.update(status="closed", qty=0.0, exit_price=float(b["c"]), exit_date=d, exit_order_id="simulated",
                           spy_exit=spy_price(d, "close"))
                notes.append(f"simuleret salg {lot['symbol']}: lukket til slutkursen {float(b['c']):.2f} d. {d}")
                log(kind="fill", client_order_id=lot["lot_id"] + "-sim-s", order_id="simulated", symbol=lot["symbol"],
                    side="sell", tif="cls", order_status="sim_filled", filled_qty=lot["entry_qty"],
                    fill_price=float(b["c"]), fill_time=d, member=lot["member"], doc_id=lot["doc_id"],
                    ticker=lot["raw_ticker"], reason="simuleret salg til slutkursen (planlagt exit)")
        if lot["status"] == "closed" and lot.get("exit_price") is not None:
            book.realized += float(lot["entry_qty"]) * (float(lot["exit_price"]) - float(lot["entry_price"]))
        elif lot["status"] == "open":
            last = float(ser[-1][1]["c"]) if ser else float(lot["entry_price"])
            q = float(lot["qty"])
            book.unrealized += q * (last - float(lot["entry_price"]))
            book.open_cost += q * float(lot["entry_price"])
            book.value_by_symbol[lot["symbol"]] = book.value_by_symbol.get(lot["symbol"], 0.0) + q * last
            book.price_by_symbol[lot["symbol"]] = last
            book.open_symbols.add(lot["symbol"])
    return book, notes


def pnl_on(state: dict, bars: dict, d: str) -> float | None:
    """Simulated P&L (realized + unrealized) as of the close of market date d, for back-filling history rows.
    None when a needed close is missing (the row is then left as it is)."""
    series = {s: dict(_series(b)) for s, b in bars.items()}
    total = 0.0
    for lot in sim_lots(state):
        if not lot.get("entry_date") or lot["entry_date"] > d:
            continue
        q, e = float(lot["entry_qty"]), float(lot["entry_price"])
        if lot["status"] == "closed" and lot.get("exit_date") and lot["exit_date"] <= d:
            total += q * (float(lot["exit_price"]) - e)
            continue
        ser = series.get(lot["symbol"], {})
        closes = [k for k in ser if k <= d]
        if not closes:
            return None
        total += q * (float(ser[max(closes)]["c"]) - e)
    return total


def bars_start(state: dict) -> str | None:
    d = earliest_date(state)
    return (date.fromisoformat(d) - timedelta(days=5)).isoformat() if d else None
