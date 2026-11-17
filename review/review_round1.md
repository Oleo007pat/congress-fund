# Bias review, round 1: Congress-buys backtest v1

Reviewer: independent bias reviewer. I did not edit `src/`, `data/` or `results/`.
Scratch scripts are in `review/`. Each one runs from `/home/claude/v1` with `python3 -m review.<name>`:
- `recompute_portfolio`: an independent naive portfolio loop built from the raw price CSVs;
- `hand_check`: the 10 random trades;
- `full_check`: all 19,425 priced rows re-derived;
- `delisted`, `jumps`, `dups`: data checks;
- `sensitivity`, `sensitivity2`, `continuous`, `sens_nondisc`: what-ifs. These reuse the author's `Evaluator` read-only, after its math was verified independently.

## What I verified and found correct

The mechanics are correct. The problems are in what the headline numbers mean, and in one rule interpretation that biases the selection.

- **Entry:** the open of the first SPY trading day strictly after `filing_date`. All 19,425 priced rows match, with 0 mismatches. No row has `filing_date < transaction_date`.
- **Reference price:** the last valid close on or before the transaction date. 0 mismatches. The reference is never on or after the entry, and never after the transaction date.
- **Rise and skip rule:** 0 mismatches. Grid skip counts equal `count(rise > X)` exactly for X = 0/2/10/30, in-sample and OOS.
- **member_sells exit:**
  - It uses the SALE's `filing_date`, never its transaction date.
  - The exit is never on or before the sale filing, and never before the entry.
  - The first-sale filing date was re-derived independently: 0 mismatches.
- **hold_12m exits:**
  - 0 exits fall before the 12-month target.
  - 2 exits fall on a later day because the symbol had no valid bar on the target day, which matches the rule.
  - Exit prices, net returns and SPY window returns: 0 mismatches.
- **Costs:** net = (1 − 0.001)² · P_exit/P_entry − 1, with entry and exit costs also charged inside the portfolio.
- **Portfolio metrics:** an independent per-trade loop from the raw CSVs reproduces total return, CAGR, volatility, Sharpe and max drawdown to 6 decimals. This holds for hold_12m/amount/none in in_sample, out_of_sample and in_sample_cut2021, and for SPY. The final values in `equity_curves.csv` equal 1 + total return.
- **Force-close at 2021-12-31:**
  - The cut uses `close_ff` at the 2021 close.
  - Segment boundaries detected with post-2021 data cannot change a cut exit price: a segment ending in late 2021 exits at its last close, and the carried-forward close on 2021-12-31 is that same close.
  - `select_configuration` only sees the cut rows.
- **Unit tests:** `python3 -m unittest tests.test_backtest` passes, 30/30.

---

## Findings

### F1 — ALVORLIG: the OOS headline (+106 % vs SPY +66 %) comes from an empty-portfolio start plus one member's 34 trades
**Where:**
- `src/backtest/run.py` l.115-117 (`sample_frame`): each sample's portfolio holds only its own signals.
- `src/backtest/portfolio.py` l.57 (`first = e.min()`): the series starts on the first entry day.
- `src/backtest/engine.py` l.57-63: absolute-dollar amount weights.

**What is wrong:**
- The OOS portfolio starts empty on 2022-01-03.
- Its first positions are Mark Green's ("Mark Dr Green") USD 0.5–1 M energy-MLP buys of 3–4 Jan 2022.
- In a calendar-time book with absolute dollar weights, these few trades are almost the whole portfolio during 2022.
- 2022 was a year when energy rose and SPY fell 18 %. The whole OOS outperformance comes from this warm-up artefact.

**Evidence** (recommended configuration hold_12m / amount / X=none; `review/sensitivity2.py`, `review/continuous.py`, `review/sensitivity.py`):

Mark Green's share of the OOS portfolio value was:
- 98.7 % on 2022-01-05;
- 68 % at 2022-03-31;
- 57 % at 2022-06-30;
- 38 % at 2022-12-31;
- below 8 % from 2023 on.

| variant | strategy total | Sharpe | SPY total | SPY Sharpe |
|---|---|---|---|---|
| author's OOS (2022-01-03..2026-11-16) | +106.1 % | 0.816 | +65.5 % | 0.685 |
| same, calendar 2022 only | +16.7 % | 0.73 | −18.4 % | −0.72 |
| same, 2023-01-03..2026-11-16 | +76.6 % | 0.86 | +102.9 % | 1.29 |
| OOS excluding Mark Green's 34 trades (of 8,088) | +44.5 % | 0.47 | +65.5 % | 0.68 |
| OOS excluding the 10 trades with the largest USD excess | +69.4 % | 0.69 | +65.5 % | 0.68 |
| OOS, amount weight capped at USD 32,500 | +64.4 % | 0.68 | +65.5 % | 0.68 |
| OOS, equal weight | +62.3 % | 0.68 | +65.5 % | 0.68 |
| **live-like continuous book** (all signals 2016→, configuration fixed, window 2022-01-03..2026-11-16) | **+64.8 %** | **0.60** | +66.0 % | 0.69 |
| continuous book, excluding Mark Green | +36.8 % | 0.41 | +66.0 % | 0.69 |

- The 10 most favourable trades produce 121 % of the OOS amount-weighted USD excess.
- Mark Green's 34 trades produce 112 % of it.
- The mean trade return relative to SPY is −1.4 %.
- In a daily regression on SPY, alpha is +4.8 %/yr with t = 0.98. Excluding Mark Green, alpha is −3.3 %/yr.

**Consequence:** the conclusion "the recommended configuration beat SPY out-of-sample" does not hold.
- With a realistic live book, the strategy is roughly equal to SPY on return and worse on Sharpe.
- Removing one member, or the 2022 warm-up, turns it into clear underperformance.

The author mentions "concentration in a few huge trades". That understates it: this is no longer a qualitative caveat; the sign of the result depends on it.

**Fix:**
1. Report the continuous book over the calendar window 2022-01-03→ as the main OOS figure. The configuration is fixed, so this leaks nothing. Alternatively, exclude a warm-up of at least one holding period.
2. Report a per-member cap or a capped-amount variant next to it, plus equal weighting.
3. Put the "excluding the largest member" and "excluding 2022" rows in `recommendation.json`.
4. Apply the same warm-up treatment to the in-sample selection sample, which also starts empty on 2016-01-05.

### F2 — ALVORLIG: skipping signals with a missing reference price biases the X comparison, and the claim "X = none best everywhere" is an artefact
**Where:**
- `src/backtest/engine.py` l.143-144 (`skip_decision`) and l.153 (`skip_mask`);
- `src/backtest/run.py` l.133-136;
- `results/ASSUMPTIONS.md` l.35 and l.121.

**What is wrong:**
- For every finite X, a signal without a reference price is skipped. With X = none, it is taken.
- The spec says to skip "ONLY if the price ROSE more than X %". A missing reference is not a rise.
- The missing references are almost all an artefact of the price history starting on 2016-01-04: the transaction happened in 2015 and the filing in 2016. The split is 280 in-sample against 3 OOS.
- So every finite X carries a mechanical in-sample handicap that has nothing to do with the rule and is absent OOS.

**Evidence** (`review/sensitivity2.py`, selection on in_sample_cut2021):

| handling of missing reference | chosen configuration | pairs where X=none is best | OOS total / Sharpe of the choice (author's per-sample method) |
|---|---|---|---|
| A: skip (author) | hold_12m/amount/**none** (Sharpe 1.0020) | 8 of 8 | +106.1 % / 0.816 |
| B: take | hold_12m/amount/**X=4** (1.0201) | 2 of 8 | +89.0 % / 0.701 |
| C: drop these signals for every X, including none | hold_12m/amount/**X=6** (0.9485) | 1 of 8 | +92.3 % / 0.729 |

With hold_12m/amount and missing references taken:
- X=10 gives Sharpe 1.0036;
- X=30 gives 0.9900 instead of 0.9270.

**Consequence:**
- The chosen X and the documented conclusion that the skip rule is useless both depend on this interpretation.
- The neutral like-for-like comparison is C, and under it a finite X wins.
- The Sharpe differences between X values are small, so the honest conclusion is "no X is reliably better", not "none wins in every case".

**Fix:**
1. Exclude signals without a reference from all grid cells, including X = none (option C), so that every X is compared on the same signal set.
2. Remove the sentence in ASSUMPTIONS l.121, or qualify it.
3. Re-run the selection.

### F3 — MIDDEL: the selection is fragile and driven by non-discretionary "buys"; in-sample shows no edge over SPY
**Where:**
- `src/backtest/engine.py` l.31-36 (all include_v1 buys are taken);
- `src/backtest/config.py` l.64-67 (absolute Sharpe; the benchmark is ignored).

**Evidence** (`review/sensitivity.py`, `review/sens_nondisc.py`):
- **Suzan DelBene's rows:**
  - Her spouse's 4 MSFT rows of "$5,000,001–25,000,000" (weight 15 M each) make up 20 % of the in-sample amount weight.
  - Row 22598 says "Transfer in of vested stock.", i.e. RSU vesting, not a purchase. The other three fall on the same vesting-type dates (31 Aug / 3 Sep / 28 Feb).
  - Excluding DelBene (45 rows): hold_12m/amount Sharpe falls from 1.002 to 0.840, below equal weighting (0.845). The selection then picks hold_3m/amount (OOS +95.6 %, Sharpe 0.690 vs SPY 0.685).
- **Rows whose description says dividend reinvestment / "Compensation" / "Employee stock plan" / vested** (about 800 buy rows, e.g. Kevin Hern's "Dividend Reinvestment" in DVN/PXD): excluding them makes the selection pick hold_6m/amount (OOS +90.3 %, Sharpe 0.699).
- **The criterion ignores SPY.** The chosen configuration's in-sample Sharpe is 1.002 against SPY's 0.999. Every one of the other 135 combinations is below SPY's Sharpe. In-sample therefore gives no evidence of an edge (ASSUMPTIONS l.123 acknowledges this but draws no consequence).

**Fix:**
1. Exclude non-purchase acquisitions (vesting, DRIP, compensation, transfers) from the signals, or flag them and run a sensitivity.
2. Report the selection's stability under leave-one-member-out.
3. State in the recommendation that in-sample shows no edge over SPY.

### F4 — MIDDEL: no statistical significance is reported
**Where:** `results/recommendation.json`, which has no uncertainty measures at all.

**Evidence:**
- OOS, daily excess against SPY: tracking error 10.8 %/yr, information ratio 0.48, t ≈ 1.06.
- Regression alpha is 4.8 %/yr with t = 0.98, beta 1.04.
- The Sharpe difference of 0.13 over 4.9 years is far inside its standard error (about 0.45).
- This is before correcting for the 136 combinations tried and for F1.

**Fix:** report alpha/beta with t-statistics, the IR, and a bootstrap or Jobson–Korkie test of the Sharpe difference. State plainly that the OOS difference is not significant.

### F5 — MIDDEL: artefacts in the price series (ticker chains, ADR ratio changes, spin-offs) that the "adjustment=all" data does not handle
**Where:**
- `src/backtest/prices.py` (no correction);
- `src/backtest/config.py` l.48-50 (flag only, thresholds +100 % / −50 %);
- `results/ASSUMPTIONS.md` l.158-160.

**Evidence** (`review/jumps.py`; one-day close/close moves in held symbols):

| symbol | date | one-day move | likely cause |
|---|---|---|---|
| DOW | 2019-06-03 | +149 % | the series chains the DowDuPont history onto Dow Inc. Before the jump it trades around 22–27 while Dow Inc traded around 55; after the jump it continues as Dow Inc |
| AZN | 2026-02-02 | +103 % | ADR ratio change; OOS |
| NVS | 2019-04-09 | −82 % | Alcon spin-off; the previous close of 401 is not a real NVS price |
| DLPH | 2017-12-05 | −82 % | Delphi → Aptiv / Delphi Technologies |
| DDR | 2018-07-02 | −92 % | |
| SRC | 2018-06-01 | −91 % | |
| RTX | 2020-04-03 | −71 % | the series chains UTX onto RTX |
| DD | 2019-06-03 | +17.8 % | the Corteva / 1:3 reverse-split day; below the flag threshold, so not flagged |

Effect on the trades:
- **hold_12m:** 62 trades are held across such a jump. They contribute −1.18 M USD (in-sample) and +19 k USD (OOS) to the amount-weighted USD excess (15.7 M and 3.9 M in total).
- **member_sells:** the three DOW buys from 2016–2017 show +388 % to +566 %, inflated by the 2019 jump.

The author's sensitivity "excluding extreme-move trades" removes some trades but misses unflagged artefacts like DD. It does not change the recommended conclusion, but it does bias the in-sample comparison between exit rules: longer holds are more exposed to these jumps.

**Fix:**
1. Maintain a list of known corporate actions (spin-offs, ADR ratio changes, chained tickers), or split segments on any one-day move above about 60 % that coincides with a known event.
2. As a minimum, treat DOW before 2019-04-02, RTX before 2020-04-03 and AZN across 2026-02-02 as separate segments.

### F6 — MIDDEL: delisting/segment exits — ticker changes treated as delistings, bankruptcies valued at their last close, small segment look-ahead
**Where:**
- `src/backtest/engine.py` l.168-175 (`_terminal_exit`);
- `src/backtest/prices.py` l.79-89 (segments with `GAP_BREAK = 10`).

**Evidence** (`review/delisted.py`). hold_12m has 432 "delisted" exits: 308 in-sample and 124 OOS. They carry 2.5 % / 1.8 % of the amount weight. Mean net return is +24.9 % against SPY +8.8 %.

- **Ticker changes counted as delistings:** the position is closed at the last close although the security continued under a new ticker. Examples:
  - FB → META (every FB position still open on 2022-06-08 is closed at 192.24, while META kept falling to about 90 afterwards);
  - CCXX → MPLN (2020-10-08; the hand-check trade 22252);
  - PCLN, UTX, RTN, SQ.

  The direction of the error depends on the case. A related mapping error: a 2025-12-16 trade whose asset is "Meta Platforms (FB)" is mapped to the different security that has traded as FB since 2025-06-26, at about 39.
- **Bankruptcies valued at the last close:**
  - SIVB exits at 106.04 on 2023-03-09 (2 OOS trades); the real value went to about 0.
  - FRC exits at 3.51 on 2023-04-28.
  - 8 hold_12m trades end below 25 % of their entry price; in member_sells there are 44.

  This is upward-biased, but the size is negligible: weights of 8,000–75,000 USD.
- **Look-ahead in the segment logic:** the exit at the last close before a gap of more than 10 days presumes knowledge that a gap is coming.
  - 45 delisted exits are in symbols that trade again later. Only 3 resume inside the planned holding window: NVVE ×2 (exit at the pre-gap close, which avoids a −22 % reopening) and BRKL ×1.
  - The effect is negligible.
  - Piece detection never decides whether a trade is eligible. A drop needs an invalid bar on the entry day either way, and the reference/segment check only looks backwards from the entry.

**Fix:**
1. Map ticker changes (an old→new chain) as the continuation of the same position.
2. For symbols whose last bar is followed by a bankruptcy (a Q-suffix successor, or a price below a set level), set the exit to −100 % or run a sensitivity with that.
3. Document that the segment exit uses the future gap.

### F7 — MIDDEL: the "rise" uses dividend-adjusted prices, so a raw price FALL can lead to a skip
**Where:**
- `src/backtest/engine.py` l.111-131 / `src/backtest/run.py` l.73 (the rise is computed on adjustment=all prices);
- `results/ASSUMPTIONS.md` l.41.

**What is wrong:**
- With back-adjusted dividends, the ratio entry/reference is a total-return ratio.
- If an ex-dividend date falls between the reference and the entry, a raw price drop smaller than the dividend becomes a positive "rise". With X = 0 (or at any X boundary) the trade is then skipped, although the price fell.
- That breaks the hard rule "a fall must NEVER cause a skip".
- ASSUMPTIONS describes this only as "marginally more often".
- The scope cannot be counted from the input data (there is no raw or split-only series).

**Effect:** none on the recommendation, since X = none. It does affect the grid cells for small X, and so the selection under F2 option B/C.

**Fix:** fetch split-only adjusted prices (adjustment=split) for the skip decision. Alternatively, document the rule as a total-return rise and add a test showing that a raw fall is never skipped.

### F8 — MIDDEL: earlier exposure to the OOS period; the "pre-registration" cannot be verified
**Where:**
- `README.md` ("Results so far", backtests on 2021–2026 data including a "pure test year");
- `backtest/backtest_delay.py`;
- `src/backtest/config.py` l.4-6.

**What is wrong:**
- The project had already seen results on 2021–2026, i.e. the OOS period: the skip rule did not help, and 3-month results did not beat the market.
- The grid design (X, exits, weightings) was set up after that.
- The claim that the criterion was "written down before any result" cannot be verified: there is no version control.

**Fix:**
1. Put the config under version control with a timestamp before any run.
2. In the report, describe the OOS as "pseudo-OOS: the period has been looked at before".

### F9 — LILLE: the calendar-time construction implies free rebalancing
**Where:** `src/backtest/portfolio.py` l.33-62.

**What is wrong:**
- New positions enter with an absolute notional w, while existing positions are valued at their current value.
- Chaining the daily returns therefore matches a fund that sells existing positions pro rata to fund each new one, without paying for it.
- Costs are understated by roughly one extra 0.1 % per side on the entry turnover, about 0.1–0.2 %/yr for hold_12m.
- It is also the mechanism behind F1.

**Fix:** document it, or simulate a self-financing fund with cash and an explicit position size.

### F10 — LILLE: Sharpe with rf = 0
**Where:** `src/backtest/portfolio.py` l.100.

- Documented. Absolute Sharpe ratios are overstated in 2023–2026.
- Because the strategy's volatility is higher than SPY's (21.0 % vs 17.4 % OOS), any rf > 0 also narrows the Sharpe gap slightly (by about 0.035 at an illustrative rf of 3.5 %).

**Fix:** add a T-bill series when one is available.

### F11 — LILLE: double counting is small
**Evidence** (`review/dups.py`):
- **Identical rows:** 269 buy rows in 119 groups are identical in every column. 150 of them are the extra copies the author flags. They cannot be resolved without the PDFs, and the sensitivity shows a small effect.
- **Same row in several filings:** only 4 extra copies exist across documents:
  - Wasserman Schultz CMTL;
  - Sessions CCI;
  - C. Scott Franklin / "Scott Scott Franklin" ITW and MCD.
- **Several owners on one trade:** 530 rows show the same member/ticker/date/amount under several owners. 99 % of them are in the same document with different `sub_holding` values (separate accounts), so they are probably legitimate.
- **Amendments:** no Amended row duplicates an included New row.
- **Duplicates kept:** for duplicates, the earliest filing is always the one kept (435 later, 79 same day, 0 earlier).

**Fix:** remove the 4 cross-document copies, and normalise names (11 members have several spellings, e.g. Mark Green, Scott Franklin, Neal Dunn).

### F12 — LILLE: data look-ahead through later deletions
17 include_v1-excluded buys were removed because the member later filed a deletion, sometimes months later (e.g. row 25106: filed 2021-02-13, deleted 2022-08-13). A live system would have traded them. The effect is negligible.

**Fix:** note it in ASSUMPTIONS.

### F13 — LILLE: documentation and statistics details
- **ASSUMPTIONS l.90:** "Mark Green (in two name spellings)" OOS is wrong. OOS has only "Mark Dr Green" (34 trades); "Mark Green" occurs only in 2020.
- **Per-trade statistics:** `mean_excess_vs_spy` and `hit_rate` mix full 12-month holds with the 1,824 truncated `open_at_end` positions (22.6 % OOS). Report them separately.
- **Truncated exit comparison in the selection sample:** in in_sample_cut2021, all exits of positions entered in 2021 are truncated at year-end. That makes the 3/6/12-month comparison less distinct in the final year. This is acceptable, but should be mentioned.
- **Selection-sample label:** `recommendation.json` labels the selection sample metrics as "in_sample" in the notes. That is correct, but in the summary table the full `in_sample` row (with 2022 prices) should be marked "contains OOS-period prices, not used for selection".

---

## 10 hand-recomputed trades

**Method:** seed `20261117` with `numpy.random.default_rng`, used to pick from the priced rows of `results/trades_backtest.csv`:
- 8 random priced signals;
- 1 random hold_12m `delisted` exit;
- 1 random member_sells `member_sale` exit.

Two more member-sale exits came up among the 8 random ones.

Everything was derived from `data/processed/trades.csv`, `data/prices/ticker_map.csv` and `data/prices/daily/*.csv` without using `src/`. Script: `review/hand_check.py`; full output in `review/_hand_check.csv`, which contains both the expected and the file value for each field.

| row_id | member / ticker | txn / filing | entry (date @ open) | ref (date @ close) | rise % | hold_12m exit (reason) | h12 gross / net | h12 SPY | member_sells: sale filing → exit, reason @ price | ms net / SPY | match |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 12662 | Greg Gianforte / F | 2018-12-21 / 2019-01-23 | 2019-01-24 @ 5.564 | 2018-12-21 @ 5.383 | +3.36 | 2020-01-24 (horizon) | +0.1535 / +0.1512 | +0.2727 | none → 2026-11-16 open_at_end @ 11.57 | +1.0753 / +2.1505 | yes |
| 37355 | Greg Stanton / NOW | 2023-09-26 / 2023-11-09 | 2023-11-10 @ 123.95 | 2023-09-26 @ 108.93 | +13.79 | 2024-11-11 (horizon) | +0.6716 / +0.6683 | +0.3919 | 2023-11-09 → 2023-11-10 member_sale_at_entry @ 123.95 | −0.0020 / 0.0000 | yes |
| 13994 | James R. Langevin / TSLA | 2019-03-15 / 2019-03-29 | 2019-04-01 @ 18.84 | 2019-03-15 @ 18.36 | +2.61 | 2020-04-01 (horizon) | +0.7038 / +0.7004 | −0.1181 | 2019-08-07 → 2019-08-08 member_sale @ 15.63 | −0.1720 / +0.0222 | yes |
| 9577 | Earl Blumenauer / CSCO | 2018-05-17 / 2018-06-26 | 2018-06-27 @ 33.55 | 2018-05-17 @ 34.12 | −1.67 | 2019-06-27 (horizon) | +0.3419 / +0.3392 | +0.0919 | 2022-04-01 → 2022-04-04 member_sale @ 48.65 | +0.4472 / +0.7742 | yes |
| 35397 | Dan Newhouse / TFC | 2023-04-26 / 2023-05-26 | 2023-05-30 @ 26.08 | 2023-04-26 @ 25.22 | +3.41 | 2024-05-30 (horizon) | +0.2715 / +0.2689 | +0.2562 | 2025-05-16 → 2025-05-19 member_sale @ 38.44 | +0.4710 / +0.4317 | yes |
| 41762 | Jefferson Shreve / HEI.A | 2025-03-13 / 2025-04-11 | 2025-04-14 @ 203.29 | 2025-03-13 @ 197.49 | +2.94 | 2026-04-14 (horizon) | +0.1106 / +0.1084 | +0.2912 | 2025-05-08 → 2025-05-09 member_sale @ 212.06 | +0.0411 / +0.0412 | yes |
| 42166 | Tim Moore / AAL | 2025-03-13 / 2025-04-14 | 2025-04-15 @ 9.585 | 2025-03-13 @ 10.67 | −10.17 | 2026-04-15 (horizon) | +0.2697 / +0.2672 | +0.3119 | 2025-04-14 → 2025-04-15 member_sale_at_entry @ 9.585 | −0.0020 / 0.0000 | yes |
| 49797 | Sheri Biggs / IBIT | 2026-03-04 / 2026-04-16 | 2026-04-17 @ 43.45 | 2026-03-04 @ 41.44 | +4.85 | 2026-11-16 (open_at_end) | −0.1533 / −0.1550 | +0.0532 | none → 2026-11-16 open_at_end @ 36.79 | −0.1550 / +0.0532 | yes |
| 22252 (delisted) | Susie Lee / CCXX | 2020-07-13 / 2020-09-03 | 2020-09-04 @ 11.18 | 2020-07-13 @ 11.21 | −0.27 | 2020-10-08 (delisted) | −0.1199 / −0.1216 | −0.0032 | none → 2020-10-08 delisted @ 9.84 | −0.1216 / −0.0032 | yes |
| 33220 (member sale) | Christopher L. Jacobs / CVX | 2022-10-12 / 2022-11-16 | 2022-11-17 @ 155.67 | 2022-10-12 @ 134.16 | +16.03 | 2023-11-17 (horizon) | −0.1729 / −0.1745 | +0.1727 | 2023-01-20 → 2023-01-23 member_sale @ 155.41 | −0.0037 / +0.0207 | yes |

All 10 match to at least 1e-12, as does the vectorised check of all 19,425 priced rows (`review/full_check.py`).

Trade 22252 (CCXX) shows F6: CCXX was a SPAC that continued as MPLN on 2020-10-09. It is a ticker change, not a delisting.

## Verdict

Alvorlige fejl fundet: 2

- **F1:** the OOS outperformance is an artefact of the empty-portfolio start and of one member. In a live-like continuous book, OOS is +64.8 % vs SPY +66.0 %.
- **F2:** skipping signals with a missing reference biases the X selection and the conclusion about the skip rule.

The code mechanics are otherwise correct and reproducible. MIDDEL: F3–F8. LILLE: F9–F13.
