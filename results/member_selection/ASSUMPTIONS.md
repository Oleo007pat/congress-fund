# Member-selection test: assumptions and interpretations (append-style)

Follow-up to backtest v1: does copying ONLY members with a strong past track record beat SPY?
- **Regenerate:** `python3 -m src.backtest.member_selection` from `/home/claude/v1` (about 2 minutes), then `python3 reports/make_figures_ms.py`.
- **Tests:** `python3 -m unittest tests.test_member_selection` (19 tests). v1: `python3 -m unittest tests.test_backtest` (42 tests, unchanged).
- **Code:** `src/backtest/ms_config.py` (pre-registration) and `src/backtest/member_selection.py`. The v1 modules are imported, not changed. No v1 file in `results/`, `src/backtest/` or `tests/` was modified (mtimes and checksums verified; nothing in v1 had to be refactored, so v1 was not regenerated).

## 2026-11-17: first and only run of the pre-registered grid

### M0 Headline
- **Chosen (pre-registered criterion):** W = 24 months, K = 5, top 20 %, hold 6 m, amount weighting, X = none.
- **In-sample (2016-09-07..2021-12-31, cut at 2021 close):** Sharpe 1.24 against SPY 1.00 on the same days, alpha +9.1 %/yr (NW t = 1.84), 1,354 trades.
- **PRIMARY OOS (continuous book, 2022-01-03..2026-11-16):** +89.6 % against SPY +66.0 % and v1 +63.9 %. Sharpe 0.72 against 0.69. Alpha +4.9 %/yr (t = 0.69), IR 0.23, Memmel z 0.08. **Not significant.**
- **It is two members:**
  - OOS: Mark Green (energy MLPs bought in 2021–22, 40 % of the weight) and Suzan DelBene (3 spouse MSFT rows of $5–25 M, 35 %) hold 75 % of the weight.
  - Without Mark Green: OOS +19.2 %, Sharpe 0.27. Without both: +14.1 %, Sharpe 0.23.
- **Equal weighting of the same filter:** OOS +29.6 %, Sharpe 0.37.
- **Per trade:** selected signals did WORSE OOS than eligible non-selected ones (mean hold-6m excess −1.8 % against −0.9 %).

### M1 Pre-registration
- `ms_config.py` was written (19:55, 2026-11-17) before `member_selection.py` existed and before any member-selection number was computed. The first result was produced at 22:29.
- Its SHA-256 is stored in `recommendation.json → pre_registration.sha256`. There is no git, so this only detects later edits; it does not prove the order to an outsider.
- The unit tests were written and passed before the first real-data run.
- **Pseudo-OOS (as v1 F8):**
  - 2022–2026 had already been examined, and the brief itself states that v1 failed OOS.
  - The README notes an earlier informal finding that "top members are not persistent".
  - The grid values (W, K, rules, k_shrink = 10) were given in the brief. They were not tuned here.

### M2 Universe and v1 rules (unchanged)
- **Signals:** v1's `build_signal_table`, unchanged.
- **Entry:** open of the first trading day after `filing_date`.
- **Skip rule:** skip only on a rise > X (dividend-adjusted rise, F7).
- **Costs:** 0.1 % per side.
- **Exclusions:** non-discretionary rows (incl. third_party_managed), duplicates and missing-reference signals are excluded exactly as in v1.
- **Gaps:** the F6 gap and piece rules are unchanged.
- **Portfolio and samples:** the calendar-time portfolio and the `in_sample_cut2021` / `oos_continuous` samples are those of v1's `Evaluator`. The member filter is applied as an `extra_mask` on row_id.
- **Selection before the skip rule:** a signal must pass the member filter AND survive the skip rule.

### M3 Track record (the look-ahead-critical part)
- **Which trades:** the member's own v1-eligible signals (`drop_reason == ""`).
  - **Interpretation:** the record includes every eligible signal, regardless of X and regardless of whether the member was selected at the time. It is the "would-have-copied" record.
  - **Why:** an X-dependent or selection-dependent record would make the record depend on the strategy's own choices, and a member who was never selected could never build one.
- **Member identity:** v1's canonical key (first name + last name + state). The spelling map uses all of trades.csv, but it is only a label for a time-invariant key, so it carries no information about returns.
- **Value per trade:** `hold_6m_net_ret − hold_6m_spy_ret`.
  - This is the realised net return after 0.1 % per side, minus SPY over the identical window (SPY open at entry to SPY close at exit).
  - The reference exit is hold 6 months for every configuration, as pre-registered.
- **"Known before t":** our entry is at the open of day t.
  - **Horizon exits:** a track-record trade counts iff `exit_idx < t`. Its exit close is known after the close of the exit day, so a trade that exits on day t itself does not count.
  - **Delisted exits (155 trades), conservative interpretation:**
    - Whether a long gap ends a price piece is decided by v1's F6 rule using the resumption price or a gap longer than 63 trading days (review round 2, F2).
    - Such a trade therefore counts only once the break is certain from past data: `exit_idx + 64 < t`.
    - (A discontinuous resumption would have revealed it earlier; the later date is used.)
    - The return itself is v1's last-close exit, optimistic for bankruptcies (v1 R6).
  - **Open positions:** `open_at_end` positions never count, and neither do positions that are still open at t (their exit is ≥ t).
  - **Filed later:** a counted trade has entry < exit < t, so it was filed before t. This is asserted in a test.
- **Missing prices:** track-record trades follow v1's rules. A buy without an entry price or a reference price is not v1-eligible and is not in the record. Gap exits wait (F6); delistings exit at the last close (R6).
- **Trailing window W:**
  - Interpretation: a track-record trade is in the window iff its EXIT date is in [t − W calendar months, t). The exit date is when the result became known.
  - W = all uses every completed trade since 2016.
- **Score:** `mean × n/(n + k_shrink)` = `sum/(n + k_shrink)`, with k_shrink = 10.
  - Note: shrinkage never changes the sign, so k_shrink has **no effect on the "score > 0" rule**. It matters only for the top-20 % ranking.
- **Eligibility:** n ≥ K completed trades in the window at t.

### M4 Selection rules at time t
- **score_pos:** eligible and score > 0.
- **top20:**
  - **Ranking universe:** all members with ≥ K completed trades in the window at t, whether or not they still trade. This is the literal reading; departed members still have records within the window.
  - **Rank:** rank = 1 + the number of eligible members with a strictly higher score, so tied members share the best rank and all pass.
  - **Pass rule:** pass iff rank ≤ ceil(0.2 · N). This is computed in integer arithmetic, because 0.2·15 = 3.0000000000000004 in floating point.
  - A member can pass with a negative score if few members are positive.
- **Timing:** scores are computed separately for every distinct entry day.
  - Two signals of the same member on the same day get the same decision.
  - Same-day signals never enter each other's record.

### M5 Grid, criterion and samples
- **Grid:** 2 W × 2 K × 2 rules × 4 exits × 2 weightings × 2 X = **128 combinations tested**. Every combination has ≥ 200 trades in-sample.
- **Criterion:** the highest Sharpe (rf = 0) on `in_sample_cut2021` with n_trades ≥ 200, ties broken by grid order. `select_ms_configuration` refuses any other sample.
- **Self-check in the run:**
  - All in-sample decisions are recomputed from a track-record table with every result known after 2021-12-31 removed, and from in-sample signals only.
  - They are identical for all 8 filters (`recommendation.json → self_check_…`). Any difference aborts the run.
- **Late start in the selection sample:** track records start empty in 2016, so the selection sample's book starts on 2016-09-07 instead of 2016-01-06.
  - SPY is measured on the same days (SPY Sharpe 1.00 here against 1.01 in v1's window).
  - Eligible and selected signals per filing year are in `eligibility_by_year.csv`. For the chosen filter, 88 of 1,070 signals were selected in 2016 and 506 of 2,191 in 2021.
- **PRIMARY OOS:** the continuous book of all signals with the locked config, evaluated 2022-01-03..2026-11-16.
  - Positions from 2021 still held in 2022 are included, among them DelBene's 3 MSFT rows (to Feb/Mar 2022) and Mark Green's 2021 energy buys.
  - **Interpretation:** this matches v1's primary measure exactly. The empty-start book (SECONDARY) is reported but not used.
- **Comparisons:**
  - "v1 chosen" = all members, hold_6m/amount/X=10. Reproduced here exactly: +63.90 %, t −0.058.
  - "All members, same exit/weighting/X" = all members, hold_6m/amount/X=none.

### M6 Multiple testing
- **Bonferroni:**
  - **In-sample:** the chosen config's alpha t is 1.84 (one-sided p 0.033). ×128 gives p = 1. The one-sided 5 % critical t for 128 tests is 3.36.
  - **OOS:** the chosen config is a single pre-registered test, so no correction is needed; its p = 0.24.
  - **Best OOS combination:** chosen after the fact, its alpha t is 2.36 (W=24m;K=15;score>0;hold_3m;amount;X=10). Bonferroni p = 1.
- **Deflated Sharpe ratio (Bailey & López de Prado 2014):**
  - **Formula:**
    - SR0 = √V · ((1−γ)Φ⁻¹(1−1/N) + γΦ⁻¹(1−1/(Ne))).
    - V is the variance of the 128 trials' daily in-sample Sharpe ratios.
    - DSR = PSR(SR0), with the skewness/kurtosis correction.
  - **Applied to the ACTIVE return (strategy − SPY):** the relevant question is beating SPY. DSR = 0.27 at N = 128, and 0.74 at N = 8 (a lenient lower bound: the number of distinct member filters). Neither reaches 0.95.
  - **Raw-return DSR:** 0.98. It is reported but **not relevant**: it tests SR > 0, which SPY itself passes (PSR 0.99 on the same days).
  - **Limitation:** the trials are highly correlated and cover slightly different date ranges. V is computed from each trial's own sample.

### M7 Concentration, members, persistence
- **Top members:** by summed weight of the executed trades. In-sample this uses the selection-sample trades; OOS it uses the continuous-book trades active on or after 2022-01-03 (as v1).
- **"Without the top member":** that member's trades are removed, and the other members' selection decisions are kept as they were. The member is not removed from the ranking universe.
- **Stability (`selected_members.csv`, section yearly_stability):**
  - (a) the members qualified on the first trading day of each year;
  - (b) the members with ≥ 1 executed selected trade entered in that year;
  - for both: overlap and Jaccard against the previous year.
- **Persistence (`persistence.csv`, added as a diagnostic, not pre-registered):**
  - **Comparison:** mean completed hold-6m excess of selected signals against track-eligible non-selected signals, in each sample.
  - **In-sample rows:** these use uncut exits. They are descriptive and never feed the selection.
  - **Welch t:** it ignores overlap between trades, so it overstates significance.

### M8 Robustness (sensitivity only, nothing here can change the chosen config)
- **Neighbours:** the 7 other (W, K, rule) with hold_6m/amount/X=none.
- **Shrinkage:** k_shrink ∈ {0, 30}.
- **Weighting:** the same filter with equal weighting.
- **Warm-up:** the selection re-run with the Sharpe measured from 2018-01-02 picks the same config.
- **Additions not in the brief:** the equal-weight row, the warm-up re-run, persistence.csv, concentration.csv (incl. excluding the top 2 members), eligibility_by_year.csv, multiple_testing.csv and decisions_chosen.csv.

### M9 Known limitations carried over from v1
- **Data:**
  - scanned filings (24 %) are missing;
  - the Senate is not covered;
  - the non-discretionary filter is post hoc (round 2, F1);
  - DelBene's unlabelled "$5–25 M" spouse MSFT rows (vesting pattern, F5) remain. They drive 55 % of the in-sample weight here (v1: 19 %).
- **Prices and returns:**
  - delisted exits at the last close;
  - ticker changes are treated as delistings;
  - rf = 0;
  - SPY is treated as total return;
  - the costs of rebalancing are understated (F9).
- **The member filter amplifies the amount-weighting concentration of v1:** 29 members in-sample and 27 OOS. The top 1 % of trades hold 58 % of the weight in-sample and 46 % OOS.
