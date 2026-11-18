# Risk-overlay test: assumptions and interpretations

Follow-up to backtest v1: do simple, pre-specified risk overlays on the **locked v1 strategy** (hold 6 months, amount weighting, X = 10 %) raise its Sharpe ratio, or lower its risk without hurting return much?
- **Regenerate:** `python3 -m src.backtest.overlays` from `/home/claude/v1` (about 25 seconds), then `python3 reports/make_figures_ov.py`.
- **Tests:** `python3 -m unittest tests.test_overlays` (27 tests). The existing suites are unchanged and pass: backtest 42, member selection 19, live 86, parser 46, House download 19, prices 12, Senate download 38.
- **Code:** `src/backtest/overlay_config.py` (pre-registration), `src/backtest/overlays.py` (engine and CLI) and `reports/make_figures_ov.py` (figure and site data). The v1 modules are imported, not changed.
- **No existing file was modified.** SHA-256 of every file under `results/`, `src/`, `tests/`, `reports/`, `review/`, `live/`, `site/` and `docs/` was taken before the work and compared after: only new files were added. v1 was not regenerated, because nothing in it had to change.

## O0 Headline
- **Both pre-registered picks are v1 itself** (amount / no cap / no vol targeting).
  - Primary pick (highest in-sample Sharpe): v1 has the highest in-sample Sharpe of the 18 (1.0147). **No overlay beats v1 in-sample (0 of 18).**
  - Risk-reduction pick: v1 is the **only** combination within 1 pp of v1's in-sample CAGR (20.4 %; the next closest is 18.3 %). The one-sided reading gives the same pick.
- **PRIMARY OOS** (continuous book, 2022-01-03..2026-11-16) is therefore unchanged from v1: +63.9 %, Sharpe 0.56, vol 22.9 %, beta 1.10, alpha t −0.06, against SPY +66.0 %, Sharpe 0.69.
- **Pseudo-OOS, ex post (NOT picks):** 14 of 18 combinations have a higher OOS Sharpe than v1, but no difference is significant. The best is amount + 5 % position cap: Sharpe 0.72, Memmel z 0.83 vs v1 and 0.18 vs SPY, deflated Sharpe vs v1 0.36. In-sample it was below v1 (0.97 against 1.01).
- **Vol targeting is generic.** On v1 OOS it lowers vol from 22.9 % to 15.6 % and max drawdown from −22.4 % to −15.1 %, and costs 11.5 pp of return (+52.4 % against +63.9 %). Its Sharpe of 0.64 is below SPY (0.69) and below SPY + the same rule (0.70). In-sample it lowered v1's Sharpe (0.95 against 1.01) and CAGR (13.3 % against 20.4 %).

## O1 Pre-registration
- `overlay_config.py` was written at 10:21 on 2026-11-18 (SHA-256 `13c8caf3a810c763f9462d1c3213147ab72e809a276462dd13c1260c5762890a`). At that point `overlays.py` did not exist and no overlay number had been computed. The hash is stored in `recommendation.json → pre_registration.sha256`, and the file was not edited afterwards.
- The unit tests were written and passed before the first real-data run (10:25).
- Two robustness items were added to the CLI **after** the first run. Neither changes the picks or the criterion:
  - the start-up check (O5);
  - the ex-post deflation of the best OOS combination (O7).
  The single overlays applied to v1 were also carried through the detail tables as descriptive rows, labelled `not_picked_*`.
- **Pseudo-OOS:** 2022–2026 had been examined in v1, in the member-selection test and in earlier exploratory work. The grid values come from the brief and were not tuned. Concentration was already known to drive v1's risk, so the cap question was asked with that knowledge. **The only clean test is the live paper fund going forward.**

## O2 Locked v1 signal (unchanged)
- `run.build_signal_table` and `Evaluator.trades("…", "hold_6m", 10.0)`, unchanged. The same 8,716 in-sample trades (cut 2021) and 14,738 trades in the continuous book are used in every combination.
- Entry at the open after filing; skip on a rise > 10 %; hold 6 months; 0.1 % per side; all v1 exclusions; F6 gap rules. rf = 0. SPY (adjustment=all) is treated as total return.
- **Self-check at run time:** amount / none / off reproduces v1's `results/grid_summary.csv` (selection sample and PRIMARY OOS: total return, CAGR, vol, Sharpe, max DD) to 1e-9, with the same trade count. The run stops otherwise.

## O3 Weighting
- equal: w = 1. amount: w = range midpoint (`engine.amount_weight`, v1). sqrt_amount: w = √(midpoint).
- The notional only sets the starting value of a position in the calendar-time book. Weights then drift with prices, exactly as in v1.

## O4 Caps (how they are implemented)
- **The book** is rewritten per position-day. Start value S_i,t is w_i on the entry day and h_i·close_{t−1} later. End value E_i,t is h_i·close_t, or h_i·exit·(1 − c) on the exit day. Return ρ = E/S − 1. Natural weight ω = S/ΣS.
  - v1 is r_t = Σ ω ρ = ΣE/ΣS − 1; identical to `portfolio.py`, and unit-tested against it and the naive loop.
- **Water-filling, every day:**
  - Position cap: every weight above 5 % is set to 5 %, and the excess goes to the uncapped positions in proportion to their weights. This repeats until none exceeds 5 %.
  - Member cap: the same on the members' summed weights at 15 %; positions inside a member are scaled pro rata.
  - Only information available at the start of day t is used (the previous close and today's entry). A unit test perturbs all later prices and checks that the weights up to day t are unchanged.
- **Cash:** if fewer than 20 positions (or 7 members) are open, the part that cannot be placed sits in cash at 0 %. This happens only on the first 18 trading days of the book (2016-01-06..2016-02-01); see O5.
- **Daily, not only at entry (interpretation).** The brief says "no single position may exceed 5 % of the book's value at entry" and also "implement as weight caps … on each day". I apply the cap every day. That satisfies the entry condition and also prevents drift above the cap; it is the stricter reading.
- **Cost of the cap (conservative addition):**
  - v1 charges nothing for its implicit pro-rata rebalancing (F9). Here the cap-induced reshuffling is charged 0.1 %.
  - The charge applies to positions held on both t−1 and t. With drifted weight π = ω'_{t−1}(1 + ρ_{t−1})/(1 + r'_{t−1}), the charged amount is gross − |net|, where gross = Σ|ω' − π| and net = Σ(ω' − π).
  - The net part is the pro-rata funding of entries and exits, which v1 treats as free. For the uncapped book gross = |net| exactly, so its cap cost is 0.
  - Size: 0.01–0.08 %/yr (`cap_cost_per_year`).
- **Binding:** with amount weighting the 5 % cap binds on 36 % of in-sample and 50 % of OOS days, and the 15 % member cap on almost every day. With equal weighting the position cap never binds after the start-up (about 630–700 open positions), so equal/position = equal/none.

## O5 Start-up artefact (robustness, not used for the picks)
- On 2016-01-06..2016-02-01 too few positions are open for a cap to place all weight, so capped books held mostly cash through the January 2016 sell-off.
- This flatters every capped book's full-period in-sample Sharpe. For example, equal/position shows 0.90 against 0.81 for equal/none; measured from 2016-02-02 both are 0.904.
- `grid_summary.csv → sharpe_from_full_cap_placement`: measured from 2016-02-02, v1 is still the highest of the 18 (1.107), so **the pick is unchanged**.

## O6 Vol targeting
- **Underlying:** the weighted and capped book's daily net returns (after cap cost).
- **Rule:** σ_t = sample std (ddof 1) of r_{t−63..t−1} × √252, and exposure e_t = min(1, 15 %/σ_t). Rebalanced daily; never above 1. A unit test perturbs r from t onward and checks that the exposure at t is unchanged.
- **Warm-up (interpretation):** exposure 1 until 63 returns exist. The overlay has no information then, and this avoids crediting it with sitting in cash at the start.
- **Cash at 0 %.** No T-bill series is available and none may be added, so this **penalises the overlay**. The uninvested part (22 % on average OOS) earns nothing, although short-term rates were clearly positive in 2023–2026. The size of the penalty cannot be computed from the inputs.
- **Cost:** 0.1 % × |e_t − ẽ_{t−1}|, where ẽ_{t−1} = e_{t−1}(1 + r_{t−1})/(1 + e_{t−1} r_{t−1}) is yesterday's exposure after drift, i.e. the actual trade. On v1 this is 110–144 % turnover per year, costing 0.11–0.14 %/yr.
- **Exposure on v1:** mean 0.87 in-sample and 0.78 OOS.
  - The in-sample minimum is 0.23 on 2020-05-21. The March 2020 crash is still inside the 63-day window, so the overlay was most under-invested during the rebound.
  - The OOS minimum is 0.36 on 2024-10-09.
- **The continuous OOS book** measures σ on its own pre-2022 returns, which were available at the time.
- **SPY + vol targeting:** the same function on SPY's daily returns over the same days (bought at the open of the book's first day), evaluated on the same windows.

## O7 Selection and multiple testing
- **Primary:** highest Sharpe on `in_sample_cut2021`, with n_trades ≥ 200 and ties broken by grid order. `select_overlay` refuses any other sample.
- **Risk-reduction:** lowest in-sample vol among |CAGR − CAGR_v1| ≤ 1 pp. This is the literal two-sided reading, chosen before computing. The one-sided check (CAGR ≥ CAGR_v1 − 1 pp) gives the same pick.
- **Counts:**

  | | in-sample | OOS |
  |---|---|---|
  | Sharpe above v1 | 0 of 18 | 14 of 18 |
  | Sharpe above SPY | 1 of 18 (v1 itself, by 0.0005) | 1 of 18 (amount/position/off) |
  | Total return above v1 | | 1 of 18 |
  | Total return above SPY | | 1 of 18 |
  | Vol below v1 | | 17 of 18 |
  | Vol below SPY | | 12 of 18 |

  The Spearman rank correlation of in-sample and OOS Sharpe across the 18 is 0.38.
- **Deflated Sharpe** (Bailey & López de Prado 2014, N = 18; V = variance of the trials' daily in-sample Sharpe ratios; skew/kurtosis corrected):
  - raw return: DSR 0.98. This only says the book has a positive Sharpe, which is market exposure. The 18 trials are highly correlated, so V and SR0 are small.
  - active return vs SPY: DSR 0.23 (the requirement is > 0.95).
  - active return vs v1: not defined, because the pick is v1.
  - **Ex post** (best OOS combination vs v1, deflated over the 18 OOS results): PSR 0.54 against SR0 = 0, DSR 0.36. Even the best-looking overlay is not distinguishable from v1.

## O8 Holdings statistics (`concentration.csv`)
- Measured on the continuous book with vol targeting off (vol targeting scales all weights equally): the **daily** share of the book held by the top member and the top position, with summaries per period and month-end series.
- This is a different measure from v1's `concentration.csv`, which sums notional per member over the period.
- **v1 (amount, no cap):**
  - the top member holds 36 % of the book on an average day; the 95th percentile is 56–61 %;
  - the top position holds 11–14 % on average and up to 50 % OOS;
  - the top position is above 5 % on 36 % (in-sample) and 50 % (OOS) of days.
- **With the 5 % position cap:** the top member still holds 30–31 % on average. A member's many small positions are not touched.
- **With the 15 % member cap:** the top member is held to 15 % (median exactly 15 %).
- **Equal weighting** has a top member at 20–28 %, and sqrt_amount at 23–27 %.

## O9 Limitations
1. **Pseudo-OOS** (O1). Every OOS number here was already partly known.
2. **Cash at 0 %** penalises vol targeting, above all OOS, where rates were positive. The capped books' cash occurs only in January 2016, where it helped them (O5).
3. **Turnover:** the calendar-time book's free pro-rata rebalancing (v1 F9) still applies to all 18. Cap and vol-target trades are charged as described.
4. **All v1 data limitations apply:** scanned filings and the Senate are missing; delistings and ticker changes are exited at the last close; the skip rule uses dividend-adjusted prices; the non-discretionary filter is post hoc (review round 2, F1).
5. **The live paper fund** (`live/strategy.json`) already sizes differently from the backtest: 1 % of equity × clip(midpoint/8,000.5, 0.25, 10), with at most 10 % per position. None of the 18 combinations is exactly that rule.
