"""PRE-REGISTRATION for the risk-overlay follow-up test (src/backtest/overlays.py).

THIS FILE WAS WRITTEN BEFORE ANY OVERLAY RESULT (IN- OR OUT-OF-SAMPLE) WAS
COMPUTED, and before src/backtest/overlays.py existed.  Its SHA-256 is recorded
in results/overlays/recommendation.json at every run, so later edits are
detectable (there is no version control; the hash does not prove the order to an
outsider).

Question: do simple, pre-specified RISK OVERLAYS on the locked v1 strategy raise
its Sharpe ratio, or lower its risk without hurting its return much?

The v1 SIGNAL IS NOT CHANGED.  Every v1 rule is reused unchanged from
src/backtest/config.py and src/backtest/run.build_signal_table:
  * House buys (include_v1, stocks + ETFs, all owners), all v1 exclusions
    (short-related, duplicates, non-discretionary, no price, missing reference);
  * entry at the OPEN of the first trading day after filing_date;
  * skip iff the price rose more than X = 10 % since the member's trade (v1 X);
  * exit hold_6m (close of the first trading day on/after entry + 6 months);
  * 0.1 % cost per side on every entry and exit;
  * calendar-time book (portfolio.py), rf = 0, SPY as benchmark.
Only the PORTFOLIO CONSTRUCTION is overlaid.

PSEUDO-OUT-OF-SAMPLE: 2022-01-03..2026-11-16 has already been examined (v1,
member-selection test, earlier exploratory work).  Anything tested here is at
best pseudo-OOS.  The only clean test is the live paper fund going forward.
"""
from . import config

OUT_DIR = config.RESULTS / "overlays"

# ---------------------------------------------------------------------------
# Locked v1 signal (not a grid dimension)
# ---------------------------------------------------------------------------
V1_EXIT_RULE = "hold_6m"
V1_X = 10.0
V1_WEIGHTING = "amount"

# ---------------------------------------------------------------------------
# GRID (pre-registered): 3 x 3 x 2 = 18 combinations.  No other values, no tuning.
# ---------------------------------------------------------------------------
# 1. Weighting = starting notional w_i of trade i
#      equal        w_i = 1
#      amount       w_i = amount midpoint (v1, engine.amount_weight)
#      sqrt_amount  w_i = sqrt(amount midpoint)
WEIGHTINGS = ["equal", "amount", "sqrt_amount"]

# 2. Concentration cap, applied EVERY DAY to the calendar-time book's weights
#      none      no cap (v1)
#      position  no single position may exceed POSITION_CAP of the book
#      member    one member's open positions together may not exceed MEMBER_CAP
CAPS = ["none", "position", "member"]
POSITION_CAP = 0.05
MEMBER_CAP = 0.15

# 3. Volatility targeting
#      off  exposure 1
#      on   exposure_t = min(1, VT_TARGET / sigma_t), sigma_t = annualised (sqrt 252)
#           sample std (ddof 1) of the underlying book's daily net returns over the
#           VT_WINDOW trading days t-63..t-1 (never day t or later); rebalanced daily;
#           no leverage (exposure <= 1); the rest is cash at 0 % (no T-bill series
#           is available and no data source may be added -> this PENALISES the
#           overlay); cost VT_COST on |exposure_t - drifted exposure_{t-1}|.
VOL_TARGETS = ["off", "on"]
VT_TARGET = 0.15
VT_WINDOW = 63
VT_COST = config.COST            # 0.1 % on the change in exposure
VT_ANNUALIZATION = config.ANNUALIZATION

N_COMBINATIONS = len(WEIGHTINGS) * len(CAPS) * len(VOL_TARGETS)
assert N_COMBINATIONS == 18

# ---------------------------------------------------------------------------
# Implementation rules fixed in advance (interpretations; most conservative
# reading chosen where the brief left room):
#
# Natural weights.  On day t the natural (v1) weight of open position i is
#   omega_i,t = S_i,t / sum_j S_j,t,  S_i,t = w_i on the entry day (bought at the
#   open) and h_i * close_{t-1} afterwards (portfolio.py), and its day-t return
#   rho_i,t = E_i,t / S_i,t - 1 includes the 0.1 % entry/exit costs.  v1's
#   return is r_t = sum_i omega_i,t * rho_i,t.  An overlay replaces omega by
#   capped weights omega'; with cap = none and vol targeting off the book is
#   exactly v1 (checked at run time against results/grid_summary.csv).
#
# Caps ("water-filling").  Every day, on the natural weights (which use only the
#   previous close and today's entry open):
#   * position cap: every weight above 5 % is set to 5 % and the excess is
#     redistributed to the uncapped positions in proportion to their weights;
#     repeated until no weight exceeds 5 %.
#   * member cap: the same on the members' summed weights at 15 %; inside a
#     member the positions are scaled pro rata.
#   * If fewer than 1/cap positions (members) are open, the remainder that cannot
#     be placed stays in cash at 0 %.  Sum of weights <= 1 always.
#   * The cap applies on EVERY day, not only on the entry day ("no single position
#     may exceed 5 % of the book's value at entry" is satisfied, and a capped
#     position cannot drift above the cap afterwards) -- the stricter reading.
#   * Cost of the cap (conservative; v1 charges nothing for its implicit pro-rata
#     rebalancing, review F9): on day t, for positions open on both t-1 and t,
#     pi'_i,t = omega'_i,t-1 (1 + rho_i,t-1) / (1 + r'_t-1) is the drifted weight;
#     the cap-induced two-sided reshuffling gross - |net|, with
#     gross = sum |omega'_i,t - pi'_i,t| and net = sum (omega'_i,t - pi'_i,t),
#     is charged at 0.1 %.  The net part is the pro-rata funding of entries and
#     exits that v1 treats as free (F9); for the uncapped book gross = |net|, so
#     its cap cost is exactly zero.
#
# Vol targeting.
#   * Underlying = the weighted (and capped) book's daily net returns.
#   * Warm-up: until 63 past returns exist, exposure = 1 (the overlay has no
#     information and does nothing -- it gets no credit for sitting out the start).
#   * sigma_t = 0 -> exposure 1.
#   * Exposure is set before day t from returns up to t-1 and earns day t's
#     return: r_vt_t = e_t * r_book_t - VT_COST * |e_t - e~_t-1|, where
#     e~_t-1 = e_t-1 (1 + r_book_t-1) / (1 + e_t-1 r_book_t-1) is yesterday's
#     exposure after drift (the actual trade needed).  e~ before the first day = 1
#     (the initial purchase is already costed by the positions' entry costs).
#   * The continuous OOS book measures sigma on its own pre-2022 returns
#     (information available at the time).
#
# SPY + vol targeting sanity baseline: the same rule applied to SPY's daily
#   returns on the same days (SPY bought at the open of the book's first day),
#   evaluated on the same windows, to separate a generic vol-targeting effect
#   from anything specific to the strategy.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# SELECTION CRITERION (same protocol as v1):
#   * evaluated ONLY on the sample in_sample_cut2021 (signals filed 2016-2021,
#     every position force-closed at the 2021-12-31 close, so no post-2021 price);
#   * eligibility: n_trades >= 200 (as v1; all 18 share v1's trades);
#   * PRIMARY PICK: highest annualised Sharpe (rf = 0, 252 days) of the book;
#   * ties (1e-12) broken by the fixed grid order WEIGHTINGS, CAPS, VOL_TARGETS
#     (first wins).
#
# SECONDARY "RISK-REDUCTION" PICK (also fixed now):
#   * candidates: combinations whose in-sample CAGR is within 1 percentage point
#     of v1's in-sample CAGR (v1 = amount / none / off), literal two-sided reading
#     |CAGR - CAGR_v1| <= 0.01 (v1 itself is a candidate);
#   * pick the one with the LOWEST in-sample annualised volatility;
#   * ties broken by the same grid order.
#   * Check only (not a pick): the one-sided reading CAGR >= CAGR_v1 - 0.01 is
#     reported next to it.
#
# PRIMARY OOS: the continuous book of all signals (2016->) with the locked
#   combination, daily returns evaluated 2022-01-03..end of data, vs SPY and vs v1.
#   Report total return, CAGR, volatility, Sharpe, max drawdown, beta, alpha with
#   Newey-West t (5 lags), IR, Memmel Sharpe-difference z vs SPY and vs v1.
# MULTIPLE TESTING: deflated Sharpe ratio (Bailey & Lopez de Prado 2014) with
#   N = 18 (raw return, active return vs SPY, active return vs v1), plus how many
#   of the 18 beat v1 and SPY on Sharpe in-sample and OOS.
# ---------------------------------------------------------------------------
SELECTION_SAMPLE = config.SELECTION_SAMPLE
SELECTION_METRIC = "sharpe"
SELECT_MIN_TRADES = 200
RISK_PICK_CAGR_TOL = 0.01
SELECTION_TEXT = (
    "Among the 18 overlay combinations weighting{equal, amount, sqrt_amount} x cap{none, position 5 %, member 15 %} "
    "x vol-target{off, on (15 %, 63 days, no leverage, cash at 0 %)} on the locked v1 signal (hold_6m, X = 10 %, "
    "0.1 % per side), choose the one with the highest annualised Sharpe (rf=0, 252 days) of the calendar-time book of "
    "in-sample signals (filed 2016-2021) with every position force-closed at the 2021-12-31 close; n_trades >= 200; "
    "ties broken by the fixed grid order. Secondary risk-reduction pick: among combinations with |in-sample CAGR - "
    "v1 in-sample CAGR| <= 1 pp, the lowest in-sample volatility. Fixed in src/backtest/overlay_config.py before any "
    "overlay result was computed."
)
