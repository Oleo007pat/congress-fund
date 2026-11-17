"""PRE-REGISTRATION for the member-selection follow-up test
(src/backtest/member_selection.py).

THIS FILE WAS WRITTEN BEFORE ANY MEMBER-SELECTION RESULT (IN- OR OUT-OF-SAMPLE)
WAS COMPUTED.  Its SHA-256 is recorded in results/member_selection/recommendation.json
at every run, so later edits are detectable (there is no version control).

All v1 rules (entry, skip rule, costs, exclusions, gaps, portfolio, samples) are
reused unchanged from src/backtest/config.py; only the member filter is new.
"""
from . import config

OUT_DIR = config.RESULTS / "member_selection"

# ---------------------------------------------------------------------------
# Track record (no look-ahead).  For a buy signal with entry day t (open), the
# member's track record consists of that member's OWN v1-eligible buy signals
# (drop_reason == "", i.e. same R1/F2/F3/duplicate/no-price rules as v1),
# regardless of X and regardless of whether they were selected at the time,
# evaluated with a FIXED reference exit (hold 6 months, independent of the
# strategy's own exit), whose realised result was KNOWN before t:
#   * horizon / member exits: exit day index < t  (the exit close is known after
#     the close of the exit day, our entry is at the open of t);
#   * delisted exits (piece ended): the piece break is only certain once the data
#     have shown it (F6 / review round 2 F2): known on day exit_idx +
#     DELISTED_KNOWN_LAG (> PIECE_MAX_GAP trading days without a bar), so the
#     trade counts only if exit_idx + DELISTED_KNOWN_LAG < t  (conservative);
#   * open_at_end positions never count.
# Per-trade value: hold_6m net return (after 0.1 % per side) minus SPY over the
# identical window (v1 column hold_6m_net_ret - hold_6m_spy_ret).
# Trailing window: track-record trades whose EXIT date lies in [t - W, t).
# ---------------------------------------------------------------------------
TRACK_EXIT_RULE = "hold_6m"
DELISTED_KNOWN_LAG = config.PIECE_MAX_GAP + 1

# Score = mean excess * n / (n + K_SHRINK)
K_SHRINK = 10
K_SHRINK_SENSITIVITY = [0, 30]          # reported only, never selectable

# ---------------------------------------------------------------------------
# Grid (pre-registered): 2 x 2 x 2 x 4 x 2 x 2 = 128 combinations.
# ---------------------------------------------------------------------------
W_GRID = [24, None]                     # months; None = all history
K_GRID = [5, 15]                        # minimum completed trades in the window
SELECTION_RULES = ["score_pos", "top20"]
TOP_FRACTION = 0.20
EXIT_RULES = list(config.EXIT_RULES)    # member_sells, hold_3m, hold_6m, hold_12m
WEIGHTINGS = list(config.WEIGHTINGS)    # equal, amount
X_GRID = [10.0, None]
N_COMBINATIONS = len(W_GRID) * len(K_GRID) * len(SELECTION_RULES) * len(EXIT_RULES) * len(WEIGHTINGS) * len(X_GRID)
assert N_COMBINATIONS == 128

# "top20": among members with >= K completed track-record trades in the window
# at time t (ALL such members, whether or not they still trade), rank by score
# (1 = best, ties share the best rank); a member passes if rank <= ceil(0.2 * N).
# "score_pos": score > 0  (note: k_shrink does not change the sign of the score).

# ---------------------------------------------------------------------------
# SELECTION CRITERION (same protocol as v1):
#   * evaluated ONLY on the sample in_sample_cut2021 (signals filed 2016-2021,
#     every position force-closed at the 2021-12-31 close);
#   * eligibility: n_trades >= 200;
#   * choose the highest annualised Sharpe (rf = 0) of the calendar-time book;
#   * ties (1e-12) broken by the fixed grid order W, K, selection, exit,
#     weighting, X (first wins).
# PRIMARY OOS: continuous book of all signals with the locked config, evaluated
# 2022-01-03 .. end of data, vs SPY.
# Multiple testing: Bonferroni on the alpha t-stat over 128 trials, and the
# deflated Sharpe ratio (Bailey & Lopez de Prado 2014) of the active return
# (strategy - SPY) and of the raw return, N = 128 trials.
# ---------------------------------------------------------------------------
SELECTION_SAMPLE = config.SELECTION_SAMPLE
SELECTION_METRIC = "sharpe"
SELECT_MIN_TRADES = 200
BONFERRONI_ALPHA = 0.05
SELECTION_TEXT = (
    "Among the 128 combinations W{24m,all} x K{5,15} x selection{score>0, top 20%} x exit{member_sells, hold_3m, "
    "hold_6m, hold_12m} x weighting{equal, amount} x X{10, none}, choose the one with the highest annualised Sharpe "
    "(rf=0, 252 days) of the calendar-time book of in-sample signals (filed 2016-2021) with every position force-closed "
    "at the 2021-12-31 close; require n_trades >= 200; ties broken by the fixed grid order. Track record: member's own "
    "v1-eligible signals with hold_6m exit known before the entry day, excess vs SPY after costs, trailing window on "
    "exit date, score = mean * n/(n+10). Fixed in src/backtest/ms_config.py before any member-selection result was "
    "computed."
)
