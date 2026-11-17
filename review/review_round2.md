# Bias review, round 2 (final): Congress-buys backtest v1

Reviewer: independent bias reviewer. I did not edit `src/`, `data/` or `results/`.
New scratch scripts are in `review/` and run with `python3 -m review.<name>` from `/home/claude/v1`:
- independent of `src/`:
  - `r2_full_check`: re-derives all eligible rows, the continuous book, the Newey-West alpha and the yearly table;
  - `r2_hand_check`: the 10 new trades;
  - `r2_sel_check`: selection-sample Sharpe ratios and one robustness variant;
  - `r2_gap`: the F6 gap rule;
- reusing the author's `Evaluator` read-only (its math is verified above): `r2_nd`, the non-discretionary filter with the rows put back.

## What I verified and found correct

- **Tests:** `python3 -m unittest tests.test_backtest` passes, 42 of 42.
- **Full re-derivation:** all 18,068 eligible rows checked independently, with my own piece rule (gap > 10 days AND (resumption open/last close outside [½, 2] OR gap > 63)). 0 mismatches in:
  - entry date and open;
  - reference close and rise;
  - hold_6m exit date, price and reason;
  - net return and SPY window return.
- **PRIMARY continuous book, hold_6m / amount / X=10, 2022-01-03..2026-11-16:** recomputed with my own per-trade loop.

  | | total | CAGR | vol | Sharpe | max DD |
  |---|---|---|---|---|---|
  | strategy (mine) | +63.9035 % | 10.677 % | 22.946 % | 0.55872 | −22.356 % |
  | SPY (mine) | +65.9937 % | — | — | 0.688047 | — |

  This is identical to `oos_continuous.csv` / `recommendation.json`.
- **Alpha and Newey-West** (my own Bartlett implementation): alpha −0.3254 %/yr, t(NW5) = −0.0579, beta 1.1003, t(β−1) = 2.557. This is identical to `significance.csv`.
  - The t-value barely depends on the lag choice: −0.056 with 0 lags (White), −0.059 with 10, −0.060 with 20.
  - Mean excess +0.87 %/yr, t = 0.156. Tracking error 12.83 %.
  - The Memmel z of −0.49 checks out by hand with the formula in `analysis.py` l.41-44. The NW sandwich in l.15-25 is correct.
- **`yearly.csv`:** all 11 calendar years match my compounding to 6 decimals (e.g. 2022 −7.59 % vs SPY −18.18 %, 2024 +16.76 % vs +24.89 %). The product of 2022–2026 equals the PRIMARY total of +63.9 %.
- **Selection sample (cut 2021), checked independently:**
  - hold_6m/amount: X=8 1.014322, X=10 1.014739, X=12 1.001281, X=none 1.001454, SPY 1.014224. All equal the file. Only these 2 of the 136 combinations exceed SPY's Sharpe.
  - Robustness "excluding DelBene" → hold_3m/amount/30: Sharpe 0.871864, equal to `selection_robustness.csv`.
  - The other figures in ASSUMPTIONS §0, F4 and F5 match the result files: +88.3 %/0.65, +60.5 %/0.67, Cleo Fields +57.8 %/0.53, artefact runs +63.7 %, the 15 % run +102 %, and equal/none +50.2 % with alpha t −0.71.
- **No OOS information in the selection:**
  - `select_configuration` only receives `in_sample_cut2021` rows (run.py l.320-321).
  - The robustness variants only change that sample (run.py l.410-420). The "12-month warm-up" only moves the start of the selection window.
  - The continuous book is used only for reporting, after the configuration is fixed. That is legitimate.
- **F2:** signals without a reference price are now removed from every X, including none (run.py l.96-101). The selection sample has n=10,697 for X=none, equal to the full eligible universe.
- **Name merge:** 39 spellings → 17 keys (first + last name + state). All 17 are the same person.
  - Same district everywhere, except Scott Franklin FL15/FL18, which is the 2022 redistricting.
  - Different people with the same surname and state are correctly kept apart: Carolyn and Sean Maloney (NY), Patrick and Stephanie Murphy (FL).
  - A last-name + state scan finds no missed merges. No member appears under more than one state.
- **F1:** the empty-start book is now labelled SECONDARY. The headline says plainly that the strategy does not beat SPY.

---

## Findings

### F1 — MIDDEL: the non-discretionary keyword list is post hoc, and in-sample it removed rows that had performed badly, which changes the selection
**Where:**
- `src/backtest/engine.py` l.71-83 (`NONDISCRETIONARY_PATTERNS`) and l.124-125;
- `results/ASSUMPTIONS.md` §F3 ("third_party_managed is beyond the four categories named in the brief").

**What is wrong:**
1. **No look-ahead, but outcome-favourable in effect.** The filter uses only the filing's own text, which is known at entry. But it was written after round 1, which had shown the in-sample selection and the OOS contributors.
2. **The excluded rows had underperformed in-sample:**
   - The 262 excluded in-sample rows have mean hold_6m excess −2.1 % (amount-weighted −4.0 %), against −0.2 % for the kept rows.
   - Excluding them lifts the selection Sharpe of the chosen configuration from 0.930 to 1.015.
   - That is what makes it one of only 2 of the 136 combinations above SPY (1.014).
   - With the rows put back, the same pre-registered criterion picks hold_12m/amount/X=6 (Sharpe 0.948 < SPY). Its PRIMARY OOS is +63.75 %, Sharpe 0.586.
3. **The third_party_managed category is broad and removes a whole member:**
   - It covers "rebalanc", "advisor", "in error" and similar phrases, and all 265 of its rows are OOS.
   - 189 of them are "Portfolio Rebalance" rows from Jefferson Shreve: every one of his OOS buys. A member who calls a trade a rebalance can still have made the decision.
   - Putting this category back lowers the PRIMARY OOS from +63.9 % / 0.559 to +61.9 % / 0.549.
   - So the category flatters the reported OOS result slightly.

**Consequence:** the conclusion "the strategy does not beat SPY OOS" is unchanged; it holds in every variant. But the named configuration (hold_6m/amount/10) and its "in-sample Sharpe = SPY" are a product of post-hoc cleaning.

The choice changes under every reasonable perturbation:

| perturbation | chosen configuration |
|---|---|
| non-discretionary rows put back | hold_12m/amount/6 |
| excluding DelBene | hold_3m/amount/30 |
| excluding the top 1 % of trades | member_sells/equal/10 |

On top of that, X=10 beats X=8 by only 0.0004 in Sharpe.

**Fix** (report as a limitation):
1. Run the main case with only the brief's categories: dividend reinvestment, vesting/compensation, gift/inheritance, transfer/corporate action.
2. Report third_party_managed as a sensitivity.
3. State in the headline that no configuration is selected with confidence: the named one is the arbitrary winner of a near-tie.

### F2 — LILLE: the F6 gap rule still decides with future data, but has no effect on the chosen configuration
**Where:** `src/backtest/prices.py` l.91-98; `src/backtest/config.py` l.52-53.

**What is wrong:**
- Whether a gap of more than 10 days ends a piece depends on the resumption price (the 2× test) and on the length of the gap (more than 63 days). Neither is known when the gap starts.
- If the piece is broken, the position exits at the pre-gap close on the pre-gap date. That exit is back-dated: a live holder would have been stuck through the gap.
- If the piece is kept, the position is valued flat at the last close and exits on the first bar after the gap. That part is causal and correct.

**Evidence** (`review/r2_gap.py`; "delisted" exits in symbols that trade again later, with the planned exit after the resumption):
- **hold_6m:** 11 such exits, none affected. All resume after the planned exit, so there is no effect on the chosen configuration.
- **hold_12m:** 1 trade, BRKL row 44703, weight 8,000.
- **member_sells:** 53 trades. Most are reused tickers with resumption/exit ratios of 0.01–0.4 or 2–6 (PCLN, FB, LLL, PX, AMTD, APC, MIC, HCP), so the last close is the sensible proxy. WW (2025, post-bankruptcy new equity at 165×) exits at −99.1 % against a realistic −100 %.

**Fix:** document in ASSUMPTIONS §F6 that the classification uses the resumption price and the gap length (look-ahead), with the effect above. A causal rule would need corporate-action data.

### F3 — LILLE: ticker changes are still exited as delistings
**Where:** `src/backtest/engine.py` l.265-274; `results/ticker_change_candidates.csv`.

- 34 probable renames are listed but not continued into their successor.
- In hold_6m/X=10 this cuts 18 trades short: 8 exit in the OOS window, carrying 0.43 M of amount weight. Examples: FB→META, SQ→XYZ, PSTG→P, MMC→MRSH, AMEH→ASTH (175 k).
- The direction of the effect varies, and it is small.

**Fix:** map successor tickers so the position continues, or keep this as a limitation.

### F4 — LILLE: F7 (the skip rule on dividend-adjusted prices) is not fixed, and X=10 is not robust to it
**Where:** `results/skip_dividend_flip.csv`.

- At X=10, up to 403 in-sample and 268 OOS skips could flip to "take" with a dividend of ≤ 2 %; with ≤ 0.5 %, 101 and 67.
- Given the 0.0004 Sharpe margin over X=8, the choice of X can depend on this.
- The limitation is documented; no other data source is available.

### F5 — LILLE: the text about pre-registration and the remaining vesting rows
- `recommendation.json` (`selection_criterion`) still says "Fixed in src/backtest/config.py before any result was computed". The criterion is unchanged, but the universe it runs on (F2/F3/duplicates) was redesigned after seeing round-1 results, including OOS. Add one sentence saying so.
- DelBene's three other unlabelled spouse MSFT rows of "$5–25 M" (pattern consistent with vesting) remain. They make DelBene 19 % of the in-sample weight, and the selection depends on them.

### F6 — LILLE: duplicate handling
**Where:** `src/backtest/engine.py` l.114-123.

- The 150 identical rows are dropped as duplicates, but they can be genuine repeated lots. The effect is small and neutral.
- The cross-document key requires the same owner, so a self/joint double report across filings is not caught. Only 4 rows are affected.

---

## 10 new hand-recomputed trades

**Method:** seed `20261118` with `numpy.random.default_rng`, used to pick from the eligible rows (`drop_reason` empty) of `results/trades_backtest.csv`:
- 8 random rows;
- 1 random hold_6m `delisted` exit;
- 1 random member_sells `member_sale` exit.

Everything was derived from `data/processed/trades.csv`, `data/prices/ticker_map.csv` and `data/prices/daily/*.csv` with my own piece rule and name normalisation (first + last + state), without `src/`. Script: `review/r2_hand_check.py`; full output in `review/_r2_hand_check.csv`.

The table shows the chosen exit (hold_6m) plus member_sells. The "X=10" column says whether the trade is taken at X = 10 %.

| row_id | member / ticker | txn / filing | entry (date @ open) | ref (date @ close) | rise % (X=10) | hold_6m exit (reason) | h6 gross / net | h6 SPY | member_sells: sale filing → exit, reason @ price | ms net / SPY | match |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 20600 | Susie Lee / WM | 2020-04-06 / 2020-06-15 | 2020-06-16 @ 96.74 | 2020-04-06 @ 82.12 | +17.80 (skip) | 2020-12-16 (horizon) | +0.0951 / +0.0929 | +0.1829 | 2020-07-10 → 2020-07-13 member_sale @ 94.32 | −0.0270 / +0.0192 | yes |
| 41478 | April McClain Delaney / PAYX | 2025-02-27 / 2025-03-20 | 2025-03-21 @ 134.65 | 2025-02-27 @ 138.99 | −3.12 (take) | 2025-09-22 (horizon) | −0.0818 / −0.0836 | +0.1992 | 2026-03-10 → 2026-03-11 member_sale @ 92.58 | −0.3138 / +0.2221 | yes |
| 5091 | Bill Flores / TEGP | 2017-01-13 / 2017-02-13 | 2017-02-14 @ 24.65 | 2017-01-13 @ 24.20 | +1.86 (take) | 2017-08-14 (horizon) | −0.0742 / −0.0761 | +0.0698 | 2017-03-08 → 2017-03-09 member_sale @ 25.32 | +0.0251 / +0.0178 | yes |
| 3236 | Bill Flores / WGP | 2016-09-26 / 2016-10-06 | 2016-10-07 @ 18.25 | 2016-09-26 @ 17.15 | +6.41 (take) | 2017-04-07 (horizon) | +0.1134 / +0.1112 | +0.0994 | 2016-12-11 → 2016-12-12 member_sale @ 18.23 | −0.0031 / +0.0476 | yes |
| 27582 | Blake Moore / JNJ | 2021-02-24 / 2021-07-17 | 2021-07-19 @ 143.93 | 2021-02-24 @ 139.69 | +3.04 (take) | 2022-01-19 (horizon) | +0.0131 / +0.0111 | +0.0671 | 2024-01-24 → 2024-01-25 member_sale @ 147.18 | +0.0205 / +0.1882 | yes |
| 32433 | John Curtis / AZO | 2022-08-12 / 2022-08-15 | 2022-08-16 @ 2279.43 | 2022-08-12 @ 2259.18 | +0.90 (take) | 2023-02-16 (horizon) | +0.1281 / +0.1258 | −0.0371 | 2023-12-15 → 2023-12-18 member_sale @ 2638 | +0.1550 / +0.1276 | yes |
| 19222 | Dean Phillips / PPG | 2020-03-05 / 2020-04-10 | 2020-04-13 @ 83.85 | 2020-03-05 @ 89.49 | −6.30 (take) | 2020-10-13 (horizon) | +0.4035 / +0.4007 | +0.2742 | 2020-05-07 → 2020-05-08 member_sale @ 80.26 | −0.0447 / +0.0503 | yes |
| 27639 | Virginia Foxx / GSK | 2021-07-09 / 2021-08-02 | 2021-08-03 @ 40.44 | 2021-07-09 @ 40.31 | +0.32 (take) | 2022-02-03 (horizon) | +0.1422 / +0.1399 | +0.0255 | 2022-08-05 → 2022-08-08 member_sale @ 34.09 | −0.1587 / −0.0394 | yes |
| 6192 (delisted) | Bill Flores / TLLP | 2017-05-25 / 2017-06-19 | 2017-06-20 @ 47.59 | 2017-05-25 @ 53.87 | −11.66 (take) | 2017-07-31 (delisted) | +0.0954 / +0.0932 | +0.0104 | none → 2017-07-31 delisted @ 52.13 | +0.0932 / +0.0104 | yes |
| 35795 (member sale) | Josh Gottheimer / NVDA | 2023-05-08 / 2023-06-11 | 2023-06-12 @ 39.08 | 2023-05-08 @ 29.06 | +34.48 (skip) | 2023-12-12 (horizon) | +0.2160 / +0.2135 | +0.0848 | 2023-07-11 → 2023-07-12 member_sale @ 42.90 | +0.0956 / +0.0398 | yes |

All 10 match to at least 1e-9 in every field, including the X=10 skip decision.

Trade 6192 (TLLP → Andeavor Logistics, 2017-08-01) is an example of F3: a rename treated as a delisting.

## Status of the round-1 findings

| round 1 | status |
|---|---|
| F1 empty-start artefact | fixed. The continuous book is primary and recomputed exactly; the headline now says no outperformance |
| F2 missing reference skipped | fixed. Removed for every X |
| F3 non-discretionary / selection | partly fixed. Filter introduced, but post hoc and outcome-favourable in-sample (new F1); the instability is documented |
| F4 significance | fixed. NW alpha, IR and Memmel verified |
| F5 price artefacts | documented with sensitivities (known artefacts −0.2 pp OOS). Not corrected in the data, which is acceptable |
| F6 delisting / gaps | partly fixed. Waiting through halts is now causal, but the break classification still uses future data with no effect on the chosen configuration (new F2); renames not continued (new F3) |
| F7 dividend rise | documented and quantified, not fixed (new F4) |
| F8 pseudo-OOS | documented |
| F9–F13 | documented or fixed (completed-trade statistics, F12, F13 text) |

## Verdict

Ingen alvorlige fejl

The headline is reproduced exactly: OOS +63.9 % (Sharpe 0.56) vs SPY +66.0 % (0.69), alpha −0.3 %/yr, NW t = −0.06. Its conclusion, that the strategy does not beat SPY, holds in every variant I tested.

The remaining issues are limitations:
- MIDDEL: F1, the post-hoc keyword filter and the unstable choice of configuration;
- LILLE: F2–F6.
