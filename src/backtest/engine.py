"""Per-signal logic: signal selection, entry, reference price, skip rule, exits.

Everything here is computed once per buy signal; the X grid is applied later as
a filter on the stored rise (``skip_decision``), so the paths are identical for
every X.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import config
from .prices import PriceBook


# ----------------------------------------------------------------------------
# R1: signal selection
# ----------------------------------------------------------------------------
def load_trades(path=config.TRADES_CSV) -> pd.DataFrame:
    t = pd.read_csv(path, low_memory=False, keep_default_na=False, na_values=[""])
    t["row_id"] = t.index  # row number in data/processed/trades.csv (0-based, header excluded)
    for c in ("include_v1", "short_related", "option_like"):
        if t[c].dtype != bool:
            t[c] = t[c].astype(str).str.lower().eq("true")
    return t


# ----------------------------------------------------------------------------
# Round 2: member-name normalisation (sale matching, duplicates, concentration)
# ----------------------------------------------------------------------------
HONORIFICS = {"hon", "honorable", "dr", "mr", "mrs", "ms", "miss", "jr", "sr", "ii", "iii", "iv",
              "md", "facs", "phd", "esq", "rev"}


def member_key(name: str, state_dst) -> str:
    """first + last name token (honorifics and single-letter initials removed) + state.
    Derived only from the names in trades.csv; the state guards against merging two
    different people with the same first/last name."""
    toks = [t for t in re.sub(r"[^a-z ]", " ", str(name).lower()).split() if t not in HONORIFICS and len(t) > 1]
    state = str(state_dst)[:2].upper() if isinstance(state_dst, str) else ""
    return f"{toks[0]} {toks[-1]}|{state}" if toks else f"{name}|{state}"


def build_member_map(trades: pd.DataFrame) -> pd.DataFrame:
    """Mapping member spelling -> canonical spelling (the most frequent spelling of the key)."""
    m = trades.groupby(["member", "state_dst"], dropna=False).size().reset_index(name="n_rows")
    m["key"] = [member_key(a, b) for a, b in zip(m["member"], m["state_dst"])]
    canon = (m.sort_values(["key", "n_rows", "member"], ascending=[True, False, True])
             .drop_duplicates("key").set_index("key")["member"])
    m["canonical"] = m["key"].map(canon)
    m["n_spellings_for_key"] = m.groupby("key")["member"].transform("nunique")
    return m.sort_values(["key", "member"])


def add_canonical_member(trades: pd.DataFrame) -> pd.DataFrame:
    mm = build_member_map(trades)
    lut = {(a, b if isinstance(b, str) else ""): c for a, b, c in zip(mm["member"], mm["state_dst"], mm["canonical"])}
    t = trades.copy()
    t["member_canon"] = [lut[(a, b if isinstance(b, str) else "")] for a, b in zip(t["member"], t["state_dst"])]
    return t


# ----------------------------------------------------------------------------
# Round 2 (F3): non-discretionary buys, by keyword in description + comments.
# Most conservative reading: any match excludes the row.  Order = reported category.
# ----------------------------------------------------------------------------
NONDISCRETIONARY_PATTERNS = [
    ("vesting_compensation", r"\bvest(ed|ing)?\b|\brsus?\b|restricted stock|compensation|employee stock|"
                             r"stock plan|\bespp\b|\bgrant(ed)?\b|\baward(ed)?\b|option exercise|exercised? (of )?options?"),
    ("dividend_reinvestment", r"re-?invest|reinves|\bdrip\b|stock dividend|dividend received|div(idend)? reinv"),
    ("automatic_plan", r"\bautomatic|\bauto[- ]?(invest|purchase|buy)|periodic|systematic|recurring|"
                       r"scheduled (purchase|investment)|payroll"),
    ("gift_inheritance", r"\bgift|inherit|bequest|bequeath|\bestate of\b|from (the )?estate|decedent"),
    ("transfer_distribution_corporate_action", r"transfer(red)? in\b|\bdistribution|spin-?off|spun off|"
                                               r"\bmerger\b|received as a result|acquired through|rollover|roll over"),
    ("third_party_managed", r"rebalanc|managed account|professionally managed|asset manager|account manager|"
                            r"advisor|adviser|without (my )?(input|knowledge)|without knowledge|in error|"
                            r"purchased by [a-z]+ management|do not control|discretionary account"),
]
_ND_RE = [(c, re.compile(p)) for c, p in NONDISCRETIONARY_PATTERNS]


def nondiscretionary_category(description, comments) -> str:
    """'' if the text gives no sign of a non-discretionary acquisition, else the first
    matching category (case-insensitive)."""
    txt = " ".join(x for x in (description, comments) if isinstance(x, str)).lower()
    if not txt:
        return ""
    for cat, rx in _ND_RE:
        if rx.search(txt):
            return cat
    return ""


DUP_IGNORE_COLS = ("row_id", "duplicate_of", "deleted_by", "member_canon")


def select_buy_signals(trades: pd.DataFrame) -> pd.DataFrame:
    """R1 + round-2 cleaning.  include_v1 buys (stocks + ETFs, all owners).  Each row
    keeps one drop_reason (first that applies), in this order:
      short_related_excluded      short covers (R1)
      exact_duplicate_row         identical to an earlier buy row in every column
      cross_doc_duplicate         same (canonical member, ticker, txn date, owner, amount)
                                  already reported in an earlier-filed document
      nondiscretionary_<cat>      text says it was not the member's own decision (F3)
    """
    t = trades if "member_canon" in trades else add_canonical_member(trades)
    b = t[(t["include_v1"]) & (t["type"] == "buy")].copy()
    reason = np.where(b["short_related"], "short_related_excluded", "")
    cols = [c for c in b.columns if c not in DUP_IGNORE_COLS]
    exact = b.duplicated(cols, keep="first").to_numpy()
    reason = np.where((reason == "") & exact, "exact_duplicate_row", reason)
    key = ["member_canon", "ticker", "transaction_date", "owner", "amount_low", "amount_high"]
    live = b[reason == ""]
    first_doc = (live.sort_values(["filing_date", "row_id"]).drop_duplicates(key)
                 .set_index(key)["doc_id"])
    fd = pd.Series([first_doc.get(tuple(k), np.nan) for k in b[key].itertuples(index=False, name=None)], index=b.index)
    cross = (reason == "") & (fd.to_numpy() != b["doc_id"].to_numpy())
    reason = np.where(cross, "cross_doc_duplicate", reason)
    cat = np.array([nondiscretionary_category(a, c) for a, c in zip(b["description"], b["comments"])], dtype=object)
    reason = np.where((reason == "") & (cat != ""), np.char.add("nondiscretionary_", cat.astype(str)), reason)
    b["drop_reason"] = reason
    b["nondiscretionary_category"] = cat
    return b


def select_sales(trades: pd.DataFrame) -> pd.DataFrame:
    """R5a: sales that can trigger the member_sells exit: include_v1 rows of type
    sell or partial_sale (partial treated as a full exit).  short_related sales are
    short sales (opening a short), not sales of a long holding -> not used.
    Matching key is the CANONICAL member name (round 2) and the filing ticker."""
    t = trades if "member_canon" in trades else add_canonical_member(trades)
    s = t[(t["include_v1"]) & (t["type"].isin(["sell", "partial_sale"])) & (~t["short_related"])]
    return s[["member_canon", "ticker", "transaction_date", "filing_date", "row_id", "doc_id"]].copy()


def sample_of(filing_date: str) -> str:
    """R11: split on filing_date."""
    if config.IS_START <= filing_date <= config.IS_END:
        return "in_sample"
    if filing_date >= config.OOS_START:
        return "out_of_sample"
    return "before_sample"


def amount_weight(low, high, flag) -> float:
    """R8b: midpoint of the amount range; open_over -> amount_low; exact -> the amount."""
    if flag == "open_over" or (high is None or (isinstance(high, float) and math.isnan(high))):
        return float(low)
    if flag == "exact":
        return float(low)
    return (float(low) + float(high)) / 2.0


# ----------------------------------------------------------------------------
# R2 + R6: entry
# ----------------------------------------------------------------------------
@dataclass
class Entry:
    idx: int = -1
    price: float = float("nan")
    drop_reason: str = ""


def find_entry(pb: PriceBook, col: int | None, filing_date: str) -> Entry:
    """Entry = OPEN of the first trading day STRICTLY after filing_date (R2).
    The transaction date is never used here.  If the symbol has no valid bar
    on exactly that day the signal is dropped with a reason (R6)."""
    if col is None:
        return Entry(drop_reason="no_symbol_mapping")
    i = pb.first_day_after(filing_date)
    if i >= len(pb.cal):
        return Entry(drop_reason="entry_after_end_of_data")
    if pb.valid[i, col]:
        return Entry(idx=i, price=float(pb.open[i, col]))
    lv, nv = pb.last_valid[i, col], pb.next_valid[i, col]
    if lv < 0 and nv >= len(pb.cal):
        reason = "no_price_data"
    elif lv < 0:
        reason = "data_starts_after_entry"
    elif nv >= len(pb.cal):
        reason = "data_ended_before_entry"  # no valid bar on/after the entry day at all
    elif pb.seg[i, col] < 0:
        reason = "entry_in_data_gap"  # between two segments (delisted / reused ticker)
    else:
        reason = "no_trade_on_entry_day"  # zero-volume / missing bar inside a segment
    return Entry(drop_reason=reason)


# ----------------------------------------------------------------------------
# R3: reference price and skip rule
# ----------------------------------------------------------------------------
@dataclass
class Ref:
    idx: int = -1
    price: float = float("nan")
    status: str = ""  # "ok" or reason it is missing


def find_reference(pb: PriceBook, col: int, transaction_date: str, entry_idx: int) -> Ref:
    """Reference = close of the member's transaction_date; if that is not a trading
    day (or the symbol did not trade), the last valid close BEFORE it (on/before).
    Using the last close on/before (never a later close) means the reference uses
    only information that existed on the member's trade date; it must lie in the
    same price segment as the entry (else possibly another security) and be at
    most REF_MAX_STALENESS_DAYS calendar days old.  All prices used are < entry
    day, so the decision uses only information available at the entry open."""
    t = pb.last_day_on_or_before(transaction_date)
    if t < 0:
        return Ref(status="missing_txn_before_price_history")
    r = int(pb.last_valid[t, col])
    if r < 0:
        return Ref(status="missing_txn_before_symbol_data")
    assert r < entry_idx, "reference must precede entry"
    if pb.seg[r, col] != pb.seg[entry_idx, col]:
        return Ref(status="missing_ref_in_other_segment")
    age = (np.datetime64(pd.Timestamp(transaction_date).date(), "D") - pb.cal[r]).astype(int)
    if age > config.REF_MAX_STALENESS_DAYS:
        return Ref(status="missing_ref_stale")
    return Ref(idx=r, price=float(pb.close[r, col]), status="ok")


def skip_decision(rise: float, x_pct: float | None) -> bool:
    """R3/R4 skip rule.  Skip ONLY if the price has RISEN by more than X %.
      * x_pct None ("none")  -> never skip.
      * rise <= X (incl. any fall, any rise exactly at X)  -> take.
      * rise is NaN (reference missing) -> skip for every finite X.  Since round 2
        (F2) such signals are removed from EVERY configuration before the grid
        (drop_reason ref_missing_*), so this branch is never reached in the run.
    The rise is computed on dividend-adjusted prices, i.e. it is a TOTAL-RETURN rise
    (F7, documented limitation: no unadjusted prices are available).
    """
    if x_pct is None:
        return False
    if rise is None or not np.isfinite(rise):
        return True
    return bool(rise > x_pct / 100.0 + config.SKIP_EPS)


def skip_mask(rise: np.ndarray, x_pct: float | None) -> np.ndarray:
    """Vectorised skip_decision (identical semantics)."""
    rise = np.asarray(rise, float)
    if x_pct is None:
        return np.zeros(rise.shape, bool)
    return ~np.isfinite(rise) | (rise > x_pct / 100.0 + config.SKIP_EPS)


# ----------------------------------------------------------------------------
# R5 / R6 / R7: exits
# ----------------------------------------------------------------------------
@dataclass
class Exit:
    idx: int
    kind: str      # "open" or "close": which price of day idx is the exit price
    price: float
    reason: str    # member_sale | member_sale_at_entry | horizon | delisted | open_at_end | cut_2021
    planned: str = ""  # planned exit date (for audit)
    planned_idx: int = -1  # first trading day the planned exit could happen (-1: none in data)


def _terminal_exit(pb: PriceBook, col: int, entry_idx: int, planned: str) -> Exit:
    """No planned exit inside the symbol's piece: either the piece truly ended
    (delisting, ticker change, bankruptcy, reused ticker -> exit at its last valid
    close, R6; OPTIMISTIC for bankruptcies, e.g. SIVB exits at its last close
    although the equity became ~worthless) or the symbol is still trading at the
    end of data (marked to market at last close, R7)."""
    end = int(pb.seg_end[entry_idx, col])
    final_segment = pb.seg[entry_idx, col] == pb.n_segments[col] - 1
    reason = "open_at_end" if (final_segment and pb.alive[col]) else "delisted"
    return Exit(end, "close", float(pb.close[end, col]), reason, planned)


def exit_fixed_hold(pb: PriceBook, col: int, entry_idx: int, months: int) -> Exit:
    """R5b: close of the first trading day on/after entry_date + N calendar months.
    If the symbol has no valid bar that day, the next valid bar of the same segment."""
    target = pd.Timestamp(pb.cal[entry_idx]) + pd.DateOffset(months=months)
    planned = str(target.date())
    p = pb.first_day_on_or_after(target)
    end = int(pb.seg_end[entry_idx, col])
    if p > end:
        return _terminal_exit(pb, col, entry_idx, planned)
    # F6: if the symbol has no valid bar on day p (halt / gap inside the same piece),
    # the exit is the first valid bar after it: known at that time, no look-ahead.
    x = int(pb.next_valid[p, col])  # <= end because end is a valid bar
    return Exit(x, "close", float(pb.close[x, col]), "horizon", planned, p)


def first_sale_exit_day(pb: PriceBook, sales_for_key: pd.DataFrame | None, buy_txn: str) -> tuple[int, str]:
    """R5a: among the member's (sell|partial_sale) of the same ticker with
    transaction_date >= the buy's transaction_date, the earliest day we can act =
    first trading day STRICTLY after that sale's filing_date; take the earliest.
    All-open-lots matching: every open lot of that member/ticker exits on the first
    such sale.  Returns (day index or n if none, sale filing date)."""
    n = len(pb.cal)
    if sales_for_key is None or len(sales_for_key) == 0:
        return n, ""
    q = sales_for_key[sales_for_key["transaction_date"] >= buy_txn]
    if len(q) == 0:
        return n, ""
    f = q["filing_date"].min()
    return pb.first_day_after(f), f


def exit_member_sells(pb: PriceBook, col: int, entry_idx: int, sale_day: int, sale_filing: str) -> Exit:
    """R5a: exit at the OPEN of the first trading day strictly after the sale's
    filing date, but never before our entry.  If the sale was already public when
    we entered (sale filed on/before the buy's filing), the exit is the entry open
    itself: a round trip that earns 0 gross and pays both costs (conservative)."""
    n = len(pb.cal)
    if sale_day >= n:
        return _terminal_exit(pb, col, entry_idx, sale_filing)
    d = max(sale_day, entry_idx)
    end = int(pb.seg_end[entry_idx, col])
    if d > end:
        return _terminal_exit(pb, col, entry_idx, sale_filing)
    x = int(pb.next_valid[d, col])  # F6: first valid bar on/after d inside the piece
    reason = "member_sale_at_entry" if x == entry_idx else "member_sale"
    return Exit(x, "open", float(pb.open[x, col]), reason, sale_filing, d)


def cut_exit(pb: PriceBook, col: int, ex: Exit, cut_idx: int) -> Exit:
    """Selection sample: force-close at the close of cut_idx if the exit is later."""
    if ex.idx <= cut_idx:
        return ex
    return Exit(cut_idx, "close", float(pb.close_ff[cut_idx, col]), "cut_2021", ex.planned, ex.planned_idx)


def trade_returns(entry_price: float, exit_price: float, cost: float = config.COST) -> tuple[float, float]:
    """R9: gross and net return.  Cost is charged on entry (we invest w, own
    w*(1-c)) and on exit (receive value*(1-c)):  net = (1-c)^2 * P_exit/P_entry - 1."""
    g = exit_price / entry_price
    return g - 1.0, (1.0 - cost) ** 2 * g - 1.0


def bench_window_return(pb: PriceBook, bench_col: int, entry_idx: int, ex: Exit) -> float:
    """R10: SPY over the identical window: SPY open on entry day -> SPY open/close on
    the exit day (same price type as the trade's exit).  No costs on SPY."""
    p0 = pb.open[entry_idx, bench_col]
    p1 = pb.open[ex.idx, bench_col] if ex.kind == "open" else pb.close[ex.idx, bench_col]
    return float(p1 / p0 - 1.0)
