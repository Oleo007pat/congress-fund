"""Fixed parameters of the backtest.  Every rule number (R1..R12) refers to the
"Fixed decisions" list in the task brief and is repeated in results/ASSUMPTIONS.md.

THE SELECTION CRITERION BELOW WAS WRITTEN DOWN BEFORE ANY RESULT (IN- OR
OUT-OF-SAMPLE) WAS COMPUTED, and is applied mechanically by
``portfolio.select_configuration`` which only ever receives in-sample rows.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
TRADES_CSV = DATA / "processed" / "trades.csv"
PRICE_DIR = DATA / "prices" / "daily"
TICKER_MAP_CSV = DATA / "prices" / "ticker_map.csv"
BENCH_SYMBOL = "SPY"
RESULTS = ROOT / "results"

# R9: transaction cost per side (entry and exit), multiplicative.
COST = 0.001

# R4: skip-threshold grid in percent; None = "none" (never skip).
X_GRID = [float(x) for x in range(0, 31, 2)] + [None]

# Float tolerance for the skip comparison (rise > X).  A rise that equals X up to
# floating-point noise is NOT skipped (boundary belongs to "take").
SKIP_EPS = 1e-9

# R5: exit rules.  member_sells + fixed holding periods in calendar months.
EXIT_RULES = ["member_sells", "hold_3m", "hold_6m", "hold_12m"]
HOLD_MONTHS = {"hold_3m": 3, "hold_6m": 6, "hold_12m": 12}

# R8: weightings.
WEIGHTINGS = ["equal", "amount"]

# R11: split on filing_date.
IS_START, IS_END = "2016-01-01", "2021-12-31"
OOS_START = "2022-01-01"

# Data-quality parameters (see ASSUMPTIONS.md section "Price data").
# A bar is "valid" (tradable) only if volume > 0 and open, close > 0.
# A run of more than GAP_BREAK consecutive calendar trading days without a valid
# bar splits a symbol's history into separate segments (delisting / reused ticker);
# a position never crosses a segment boundary.
GAP_BREAK = 10
# The reference close (R3) must be at most this many calendar days older than the
# member's transaction date, otherwise it is treated as missing (stale).
REF_MAX_STALENESS_DAYS = 7
# Round 2 (F6): a gap longer than GAP_BREAK only ends a piece if the price is not
# continuous on resumption (first open / last close outside [1/2, 2]) or the gap
# is longer than PIECE_MAX_GAP trading days (one quarter).  Otherwise a held
# position waits through the gap and exits on the first bar after it.
PIECE_RATIO = 2.0
PIECE_MAX_GAP = 63
# Data-quality flags only (never used by a trading rule; sensitivities only):
# one-day close/close move inside the holding window.
EXTREME_UP, EXTREME_DOWN = 2.0, 0.5          # round-1 flag: > +100 % or < -50 %
JUMP15_UP, JUMP15_DOWN = 1.15, 1 / 1.15      # F5: > +15 % or < -13.04 % (catches DD +17.8 %)
# F5: one-day moves that the reviewer identified as corporate-action artefacts of the
# adjusted series (chained tickers, spin-offs, ADR ratio changes).  Each is checked to
# exist as a > 15 % move in the price file at run time.
KNOWN_ARTEFACTS = [("DOW", "2019-06-03"), ("AZN", "2026-02-02"), ("NVS", "2019-04-09"),
                   ("DLPH", "2017-12-05"), ("DDR", "2018-07-02"), ("SRC", "2018-06-01"),
                   ("RTX", "2020-04-03"), ("DD", "2019-06-03")]
# F7: dividend size used to bound skip decisions that could flip on raw prices.
DIV_FLIP_MAX = 0.02
# F4: Newey-West lags for daily regressions.
NW_LAGS = 5

ANNUALIZATION = 252  # trading days per year for vol / Sharpe; rf = 0 (R12)

# ---------------------------------------------------------------------------
# R11 SELECTION CRITERION (fixed before computing any result):
#   * Candidates: every (exit rule x weighting x X) combination, 4 x 2 x 17 = 136.
#   * Evaluated ONLY on the sample "in_sample_cut2021": in-sample signals
#     (filing_date 2016-01-01..2021-12-31) whose entry is on/before the last
#     trading day of 2021; every position still open at the close of that day is
#     force-closed (marked) at that close incl. exit cost.  Hence the selection
#     uses no price observed after 2021-12-31 (the plain "in_sample" rows let
#     in-sample positions run into 2022-2026, which would leak OOS-period prices
#     into the choice).
#   * Eligibility: n_trades >= SELECT_MIN_TRADES.
#   * Choose the combination with the highest annualised Sharpe ratio (rf = 0)
#     of the calendar-time portfolio.  Ties (equal to 1e-12) are broken by the
#     fixed order of EXIT_RULES, WEIGHTINGS and X_GRID (first wins).
#   * The chosen combination is then reported out-of-sample unchanged.
#   * Robustness: Sharpe of neighbouring X values (X-2, X+2 and "none") for the
#     same exit/weighting is reported next to it, in-sample only for choosing.
# ---------------------------------------------------------------------------
SELECTION_SAMPLE = "in_sample_cut2021"
SELECTION_METRIC = "sharpe"
SELECT_MIN_TRADES = 200
SELECTION_TEXT = (
    "Among all 136 combinations of exit rule x weighting x X, choose the one with the "
    "highest annualised Sharpe ratio (rf=0, 252 days) of the calendar-time portfolio "
    "built from in-sample signals (filing_date 2016-01-01..2021-12-31), with every "
    "position force-closed at the close of the last trading day of 2021 so that no "
    "post-2021 price enters the choice; require n_trades >= 200; ties broken by the fixed "
    "grid order. Fixed in src/backtest/config.py before any result was computed."
)
