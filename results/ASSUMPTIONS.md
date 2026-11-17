# Backtest v1 (round 2): assumptions and interpretations

This backtest copies US House members' stock and ETF purchases from their PTRs.
- **Regenerate** everything with `python3 -m src.backtest.run` from `/home/claude/v1` (about 4 minutes).
- **Run the tests** with `python3 -m unittest tests.test_backtest` (42 tests).
- **Code** lives in `src/backtest/`: `config.py`, `prices.py`, `engine.py`, `portfolio.py`, `analysis.py`, `run.py`. Each rule is implemented in a function whose docstring names the rule (R1..R12 = the fixed decisions in the brief, F1..F13 = findings in `review/review_round1.md`).
- **Inputs:** nothing under `data/` is modified, and no other data source is used.
- **Ambiguity:** where a rule was ambiguous, I chose the conservative reading and say so below.

## 0. Headline (read this first)
- **The pre-registered selection picks hold_6m / amount weighting / X = 10 %.**
  - In-sample (in_sample_cut2021, 2016-01-06..2021-12-31) its Sharpe is 1.0147 against SPY's 1.0142, so **in-sample there is no edge over SPY**.
  - Only 2 of the 136 combinations have a Sharpe above SPY's.
  - Its regression alpha is +1.7 %/yr (Newey-West t = 0.55).
- **Primary out-of-sample (continuous book, 2022-01-03..2026-11-16):** +63.9 % against SPY +66.0 %. CAGR 10.7 % against 11.0 %, Sharpe 0.56 against 0.69.
  - Alpha is −0.3 %/yr (Newey-West t = −0.06), beta 1.10, information ratio 0.07.
  - The Sharpe-difference test (Memmel) gives z = −0.49.
  - **The strategy does not beat SPY out-of-sample, and nothing is statistically significant.**
- **Equal weighting with X = none (same exit), in the same OOS window:** +50.2 % against +66.0 %, alpha −1.6 %/yr (t = −0.71), IR −0.43.
- **The round-1 headline (+106 % against +66 %) was an artefact.** It came from the OOS-only book starting empty in a 2022 energy rally (review F1).
- **The OOS period is pseudo-OOS.** It had been examined in earlier exploratory work (see F8).

## R1 Signals and round-2 cleaning
- **Base set:** `include_v1 == True` and `type == 'buy'` gives 20,567 rows. All owners are included.
- **Exclusions:** each row gets at most one `drop_reason` (column in `trades_backtest.csv`), the first that applies, in this order:

| reason | rows | note |
|---|---|---|
| `short_related_excluded` | 67 | short covers, not new long positions (R1) |
| `exact_duplicate_row` | 150 | identical to an earlier buy row in every column of trades.csv (round 2) |
| `cross_doc_duplicate` | 4 | same (canonical member, ticker, transaction date, owner, amount) already in an earlier-filed document; the earliest filing is kept (round 2, review F11) |
| `nondiscretionary_*` | 933 | see F3 below |
| no price at entry (6 reasons) | 1,065 | see R6 |
| `ref_missing_*` | 280 | see F2 below |
| **kept** | **18,068** | 10,700 in-sample, 7,368 OOS |

- **Several rows in one filing with different `sub_holding` values** are kept as separate signals: they are separate accounts.

## F2 (round 2): signals without a usable reference price are removed from EVERY configuration
- **Change from round 1:** previously such a signal was skipped for finite X but taken for X = none. That gave every finite X a mechanical in-sample handicap: 277 of the 280 cases are in-sample, mostly transactions in 2015 with filings in 2016, before the price history starts.
- **Now:** these 280 signals get `drop_reason = ref_missing_<status>` and are excluded from all 17 X values, so every X is compared on the same universe:
  - 269 transaction before 2016-01-04;
  - 10 transaction before the symbol's data;
  - 1 reference in a different price piece.
- They are counted as `n_dropped_ref_missing`. A unit test checks that the universe is identical for every X.
- **Effect on the selection:** the selection was re-run with the unchanged criterion, and **the choice changed** from hold_12m/amount/X=none to **hold_6m/amount/X=10**.
  - The cause is F2, F3 (non-discretionary rows, among them a $5–25 M "transfer in of vested stock") and the deduplication together.
  - The margin is tiny: X=8 gives 1.0143, X=none 1.0015 and X=12 1.0013, against 1.0147 for X=10.
  - The honest reading is "no X is reliably better", not "X = 10 works".

## R2 Entry
- Entry is the **OPEN of the first SPY trading day strictly after `filing_date`** (`engine.find_entry`). The transaction date is never used.
- The SPY date list is the exchange calendar, so finding the next trading day uses no price information.
- If the symbol has no valid bar on exactly that day, the signal is dropped with a reason.

## R3 Reference price and skip rule
- **Reference:** the close on `transaction_date`, or if that is not a trading day or the symbol did not trade, the last valid close on or before it (`engine.find_reference`). Never a later close.
- **Validity checks:** the reference must lie in the same price piece as the entry, and be at most 7 calendar days older than the transaction date.
- **Rise and skip:** rise = `entry_open / reference − 1`. **Skip iff rise > X** (`engine.skip_decision`).
  - A fall, or a rise exactly at X, is always taken. A 1e-9 tolerance absorbs float noise at the boundary.
  - The decision uses only `entry_open` and closes before the entry day. A test perturbs every later price and checks that the decision is unchanged.
- **F7, known limitation (no fix possible with these inputs):** the prices are adjustment=all, i.e. split- and dividend-adjusted. The "rise" is therefore a **total-return rise**.
  - If an ex-dividend date lies between the reference and the entry, a raw price fall smaller than the dividend can show up as a positive rise and be skipped.
  - That conflicts with "a fall never causes a skip" when read on raw prices.
  - Unadjusted or split-only prices are not in the inputs, and no new source was added.
  - **Quantified (`skip_dividend_flip.csv`):** at the chosen X = 10 %, the skips with an adjusted rise in (10 %, 12.2 %] are the ones a dividend of up to 2 % could flip to "take". There are 403 such skips in-sample (of 1,981) and 268 OOS (of 1,349). A dividend of 0.5 %, a typical quarterly payment, could flip 101 / 67.
  - These are upper bounds. Not every window contains an ex-dividend date, and the ref→entry window is usually 2–6 weeks.
  - In the other direction, a split is handled correctly by the adjustment, whereas raw prices would give nonsense.

## R4 Grid
- X = 0, 2, …, 30 % and "none". Paths are computed once, and X only filters (`engine.skip_mask`).

## R5 Exits
- **member_sells:**
  - **What triggers an exit:** an `include_v1` sale (`sell` or `partial_sale`) by the same member, of the same filing ticker, with `transaction_date >= buy transaction_date`.
    - **A partial sale is treated as a full exit.**
    - Short-related sales do not count.
    - The owner is not matched.
  - **Matching:** all open lots exit on the first such sale. The earliest-filed qualifying sale wins.
  - **Exit price:** the OPEN of the first trading day strictly after the sale's `filing_date`, never before our entry. If the sale was public when we entered, the exit is a round trip at the entry open (gross 0, both costs).
  - **Round 2:** the member is matched on a **canonical name**.
    - `engine.member_key` takes the first and last name tokens, drops honorifics (Hon, Honorable, Dr, Mr, Mrs, Ms, Jr, Sr, MD, FACS, …) and single-letter initials, and adds the two-letter state.
    - The key is derived only from the names in trades.csv. The most frequent spelling becomes the canonical name.
    - This merges 39 spellings of 17 people, e.g. "Mark Dr Green"/"Mark Green" TN, "Carlos Mr Curbelo"/"Carlos Curbelo", the five spellings of Neal Dunn, and "C. Scott Franklin"/"Scott Scott Franklin"/"Scott Franklin" FL15/FL18.
    - The full mapping is in `member_name_map.csv`. The state guard prevents merging two different people who share a name.
  - **Known gap:** a ticker change between buy and sale (FB → META) is still not matched.
- **hold_3m / 6m / 12m:** exit at the CLOSE of the first trading day on/after `entry_date + N calendar months`.

## R6 / F6 Price pieces, data gaps and delistings (round 2 changed)
- **Valid bar:** volume > 0 and open, close > 0. The 59,560 zero-volume filler bars are treated as "no trade".
- **Pieces (round 2, removes the round-1 look-ahead):** round 1 split a symbol at any run of more than 10 trading days without a valid bar and exited at the last close before the gap. That exit presumed knowing the gap was coming.
  - Now a long run splits a symbol into separate pieces only if one of these holds:
    - the price on resumption is not continuous (first open / last close outside [½, 2]); this indicates a different security, a reused ticker, or post-bankruptcy new equity such as GPOR (×448) or CBL (×294);
    - the gap is longer than 63 trading days (one quarter).
  - Otherwise the symbol stays one piece, e.g. a trading halt or an illiquid stretch with a continuous price. A held position simply waits: it is valued at its last close and **exits on the first valid bar after the gap** if its planned exit fell inside the gap.
  - 102 long gaps are now kept inside a piece. 88 symbols are still split.
  - **Exits delayed by a gap** (`gap_exits.csv`, column `{rule}_exit_delay_tdays`): hold_3m 2, hold_6m 2, hold_12m 4, member_sells 0. Most are two NVVE trades, which in round 1 were exited at the last close before the gap and are now held through it.
- **A piece that truly ends:** the position exits at its last valid close (`exit_reason = delisted`, counted as `n_delisted_exit`).
  - **This is optimistic for bankruptcies and failed banks.** SIVB positions exit at 106.04, the last close on 2023-03-09 (2 OOS trades under hold_12m, 1 under member_sells), although the equity became ~worthless. FRC exits at 3.51 on 2023-04-28.
  - Delisted exits ending below 25 % of the entry price: member_sells 41, hold_12m 7, hold_6m 2, hold_3m 0.
  - Stress test (`sensitivities.csv`), valuing **every** delisted exit at −100 %:
    - chosen configuration OOS: +49.8 %, Sharpe 0.48 (instead of +63.9 %, 0.56);
    - selection Sharpe 0.944 instead of 1.015.
- **Ticker changes treated as delistings (not fixed, listed):** `ticker_change_candidates.csv` finds ended pieces whose last 20 daily returns another loaded symbol reproduces to within 0.2 % per day, and which keeps trading.
  - Flat, pinned series are excluded so they cannot match cash ETFs.
  - 34 probable renames are found: FB→META, PCLN→BKNG, UTX→RTX, SQ→XYZ, HCP→DOC, PEAK→DOC, DWAC→DJT, IIVI→COHR, GPS→GAP, ABC→COR, FLT→CPAY, RBS→NWG, MMC→MRSH, and others.
  - The reviewer's CCXX→MPLN is not detected because MPLN is not among the loaded symbols.
  - These positions exit at the old ticker's last close. That is a valid market price at that moment, but holding periods are cut short.
  - Affected delisted exits: hold_6m 22, hold_12m 67, member_sells 102, hold_3m 6.
  - Chaining them would need a mapping table from the data stage; that is out of scope here.
- **Dropped at entry (no price):** 895 no symbol mapping, 84 data starts after entry, 73 data ended before entry, 10 entry inside a gap between pieces, 3 no trade on the entry day.
- **Entries in a later piece:** 50 entries fall in a later piece. Some are legitimate re-listings; one is the reviewer's "Meta Platforms (FB)" from 2025-12 mapped to the new FB security. They are kept. Excluding them changes nothing material (`sensitivities.csv`).

## R7 End of data
- Positions not exited by 2026-11-16 are marked at the last close, with the exit cost charged, and flagged `open_at_end`.
- For hold_6m that is 483 of the 6,019 OOS trades.
- **Round 2 (F13):** per-trade statistics are also reported over completed trades only (`*_completed` columns).

## R8 Weighting
- **equal:** 1 per trade.
- **amount:** the midpoint of the range; `exact` uses the amount; `open_over` uses `amount_low` (none in the data).
- **Amount weighting is very concentrated** (`concentration.csv`):
  - The top 1 % of trades hold 32 % of the weight both in-sample and OOS.
  - In-sample, Suzan DelBene holds 19 % of the weight, mostly her spouse's MSFT acquisitions. Their pattern (quarterly small amounts plus large amounts in late Aug/Feb) suggests vesting, but only one row says so in text, and only that row is excluded under F3.
  - OOS (continuous book), the top member is Cleo Fields with 11 % of the weight (180 trades).

## R9 Costs
- 0.1 % per side, multiplicative: `net = (1 − 0.001)² · P_exit/P_entry − 1`.
- Charged on every entry and exit, including delisted, marked-to-market and round-trip exits.
- No cost on SPY.
- **F9 (small, documented):** the calendar-time construction implies free rebalancing. Each new position is funded by selling the existing ones pro rata, at no cost. Costs are understated by about one extra 0.1 % per side on that turnover, roughly 0.1–0.2 %/yr.

## R10 Benchmark
- SPY (adjustment=all) is treated as a total-return series. This is an approximation.
- **Portfolio level:** SPY over exactly the same days, with no costs.
- **Per trade:** SPY from the entry open to the exit open or close, matching the trade's exit price type.
- `mean_excess_vs_spy` is the mean of (net trade return − gross SPY window return).

## R11 / F1 Samples, selection and the primary OOS measure (round 2 changed)
**Samples** (column `sample` in `grid_summary.csv`):

| sample | content | role |
|---|---|---|
| `in_sample_cut2021` | in-sample signals (filed 2016-01-01..2021-12-31), every position force-closed at the 2021-12-31 close | **the only input to the selection** (no post-2021 price) |
| `in_sample_full_hold` | in-sample signals held to their real exits | informational; **contains post-2021 prices; not used for selection** |
| `oos_continuous` | **PRIMARY OOS:** one continuous book of ALL signals since 2016 with the configuration fixed in-sample; daily returns evaluated 2022-01-03..2026-11-16, so 2016–2021 positions still held in 2022 are included | the realistic live result; no leak, because the configuration is fixed before 2022 |
| `oos_signals_only` | **SECONDARY OOS:** book of 2022+ signals only, starting empty on 2022-01-03 | kept for comparison; distorted by the empty start (F1) |

- **Criterion:** unchanged and pre-registered (`config.SELECTION_TEXT`). Across all 136 combinations, take the highest Sharpe of `in_sample_cut2021`, with n_trades ≥ 200. `portfolio.select_configuration` refuses any other sample.
- **Primary OOS per-trade statistics:** in `oos_continuous` they are computed over the OOS-filed trades (`per_trade_universe`).
- **Truncation in the selection sample (F13):** positions entered in 2021 are cut at year-end, which makes the 3/6/12-month exits less distinct in the final year.
- **Warm-up (F1):** the selection sample also starts empty, on 2016-01-05. Evaluating the selection Sharpe only from 2017-01-03 leaves the choice unchanged (`selection_robustness.csv`).
- **Concentration and robustness (F3; `concentration.csv`, `selection_robustness.csv`), for the chosen configuration:**
  - **Without the in-sample top member (DelBene):** in-sample Sharpe falls from 1.015 to 0.830 (SPY 1.014). The re-run selection then picks hold_3m/amount/X=30 (Sharpe 0.872, below SPY). In the OOS continuous book that choice gives +88.3 %, Sharpe 0.65, against SPY +66.0 %, 0.69.
  - **Without the in-sample top 1 % of trades by amount:** the selection picks member_sells/equal/X=10 (Sharpe 0.847). OOS continuous: +60.5 %, 0.67.
  - **OOS without the top member (Cleo Fields):** +57.8 %, Sharpe 0.53.
  - **Conclusion:** the selection is unstable and every variant's in-sample Sharpe is at or below SPY's.

## F4 Significance (`significance.csv`)
- **Method:** daily OLS of the strategy return on SPY's return, with Newey-West (Bartlett, 5 lags) standard errors.
  - Also reported: the mean daily excess return with a Newey-West t-statistic, tracking error, information ratio, and the Memmel (2003) z for the Sharpe difference.
  - Measures covered: PRIMARY, SECONDARY and the selection sample, for the chosen configuration, its X=none variant, and equal/X=none.
- **Results for the chosen configuration:**

| measure | alpha %/yr (t) | beta | IR | Memmel z |
|---|---|---|---|---|
| PRIMARY OOS | −0.33 (−0.06) | 1.10 | 0.07 | −0.49 |
| SECONDARY OOS | +3.8 (0.61) | — | 0.33 | — |
| selection sample | +1.7 (0.55) | — | — | 0.003 |

- **Nothing is significant.** This is before any correction for the 136 combinations tried.

## F5 Price artefacts (sensitivities only; price files unchanged)
- **Flags** (`trades_backtest.csv`; computed over the whole series, so they are not used by any trading rule):
  - `{rule}_extreme_move_flag`: a one-day move > +100 % or < −50 %;
  - `{rule}_jump15_flag`: > +15 % or < −13 %;
  - `{rule}_known_artefact_flag`: the trade is held across one of the reviewer's identified corporate-action artefacts: DOW 2019-06-03 (+149 %), AZN 2026-02-02 (+103 %), NVS 2019-04-09 (−82 %), DLPH 2017-12-05 (−82 %), DDR 2018-07-02 (−92 %), SRC 2018-06-01 (−91 %), RTX 2020-04-03 (−71 %), DD 2019-06-03 (+17.8 %). Each is verified in the data at run time.
- **Threshold choice:** a 15 % threshold is the lowest round number that catches DD's +17.8 %. But it also flags every ordinary earnings jump: it removes 2,686 of 14,738 trades in the OOS book. That makes it an outcome-selected subset (volatile stocks out), not a clean artefact correction. The **known-artefact list is therefore the targeted sensitivity**, and the 15 % run is reported as an upper bound.
- **Effect on the chosen configuration (OOS continuous; Sharpe in brackets):**

| run | total return (Sharpe) |
|---|---|
| baseline | +63.9 % (0.56) |
| without known artefacts | +63.7 % (0.56) |
| without > 100 % / −50 % moves | +63.7 % (0.56) |
| without > 15 % moves | +102 % (0.85) |

- **Effect on the selection Sharpe:** 1.015 baseline, 1.026 without known artefacts, 1.061 without > 15 % moves.

## F8 Pre-registration caveat
- Earlier exploratory backtests (`README.md`, `backtest/backtest_delay.py`) had already examined 2021–2026 data, i.e. the OOS period, before this pre-registration. Among other things, they found that the skip rule did not help and that 3-month copying did not beat the market.
- The grid design was set up afterwards. The OOS here is therefore **pseudo-OOS**.
- The selection rule in `config.py` was written before any result of this engine. That cannot be externally verified: there was no version control. The coordinator commits `config.py` to git after round 1.

## R12 Sharpe
- rf = 0, because there is no T-bill series in the inputs. SPY is treated the same way.
- **F10:** absolute Sharpe ratios are overstated in 2023–2026, and because the strategy's volatility is higher than SPY's, rf > 0 would also slightly widen SPY's Sharpe lead.

## Portfolio construction (`portfolio.py`)
- **Calendar-time book with buy-and-hold weights:** r_t = Σ(End − Start) / ΣStart over open positions.
  - Start = w on the entry day, otherwise h × previous close.
  - End = h × close, or h × exit price × (1 − c) on the exit day.
  - h = w(1 − c) / entry open.
- Cash days earn 0.
- The engine is checked against a naive loop in the tests, and independently by the reviewer to 6 decimals.
- `oos_continuous` is the same book built from all signals and sliced to the OOS window; a unit test checks that it equals the slice of the full series.

## Other notes
- **F12:** 17 buys excluded upstream because a deletion was filed later (sometimes months later) would have been traded live. This is upstream look-ahead in the data stage, and the effect is negligible.
- **F13:** the round-1 statement "Mark Green in two spellings OOS" was wrong. OOS has only "Mark Dr Green"; the names are now merged.
- **Rows that remain:** non-discretionary acquisitions without explanatory text remain (e.g. most of DelBene's MSFT rows). The leave-top-member-out rows show their influence.

## Output files (results/)
- **`trades_backtest.csv`:** one row per include_v1 buy (20,567), with `drop_reason`, `member_canon`, `nondiscretionary_category`, entry and reference fields, `rise_pct`, the weights, `entry_piece`, and per exit rule `{rule}_exit_idx/_exit_date/_exit_price_type/_exit_price/_exit_reason/_planned_exit/_exit_delay_tdays/_gross_ret/_net_ret/_spy_ret/_extreme_move_flag/_jump15_flag/_known_artefact_flag`. The `cut_{rule}_*` columns are the selection-sample exits.
- **`grid_summary.csv`:** 4 samples × 136 combinations. Each row has counts, portfolio metrics and per-trade statistics, and `spy_*` for the same days.
- **`recommendation.json`:**
  - the criterion and the choice;
  - the selection metrics and the edge statement;
  - PRIMARY and SECONDARY OOS results and the PRIMARY significance;
  - equal/X=none, neighbouring X values, the best X per exit/weighting, and selection robustness;
  - data counts.
- **`oos_continuous.csv`:** primary, secondary, excluding 2022, excluding the top member, and the selection sample, for the three reported configurations.
- **`yearly.csv`:** calendar-year returns of the continuous all-signal book against SPY, 2016–2026.
- **`significance.csv`, `concentration.csv`, `selection_robustness.csv`, `sensitivities.csv`, `skip_dividend_flip.csv`.**
- **`excluded_nondiscretionary.csv`, `member_name_map.csv`, `ticker_change_candidates.csv`, `gap_exits.csv`.**
- **`equity_curves.csv`:** daily equity of the strategy and SPY for the reported configurations in each sample (`all_continuous`, `oos_continuous`, `oos_signals_only`, `in_sample_cut2021`, `in_sample_full_hold`).

## F3 Non-discretionary buys (keyword list)
`engine.NONDISCRETIONARY_PATTERNS` is applied case-insensitively to description + comments. Most conservative reading: **any** match excludes the row, and a row with no text is kept. Excluded rows with their text are in `excluded_nondiscretionary.csv`.

| category | regex (abridged) | rows |
|---|---|---|
| vesting_compensation | vest(ed/ing), RSU, restricted stock, compensation, employee stock, stock plan, ESPP, grant, award, option exercise | 9 |
| dividend_reinvestment | re-invest/reinves, DRIP, stock dividend, dividend received, div reinv | 638 |
| automatic_plan | automatic, auto-invest/purchase, periodic, systematic, recurring, scheduled purchase, payroll | 4 |
| gift_inheritance | gift, inherit, bequest, estate of, from estate, decedent | 0 |
| transfer_distribution_corporate_action | transfer(red) in, distribution, spin-off, merger, received as a result, acquired through, rollover | 17 |
| third_party_managed | rebalanc, managed account, asset/account manager, advisor/adviser, without (my) input/knowledge, in error, "purchased by … management", do not control | 265 |

- **Decision:** "third_party_managed" is beyond the four categories named in the brief. It is included because the text says the trade was not the member's decision (e.g. "transaction taken by advisor without my input", "portfolio rebalance"). This is the most conservative reading.
- **Kept on purpose:** "subsequent investment", "unsolicited trade" (client-initiated), "new purchase".
