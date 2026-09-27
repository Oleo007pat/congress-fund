"""
trade_logic.py
--------------
Konverterer en verificeret disclosure til et TradeSignal, og afgør om handlen
skal eksekveres eller springes over ("Trade Delay Penalty" logik).

Kernebeslutning:
  SKIP hvis  |price_now - price_at_transaction| / price_at_transaction > PRICE_DRIFT_THRESHOLD
        ELLER realized_volatility(ticker) > VOLATILITY_THRESHOLD
        ELLER days_to_disclose > HARD_MAX_DAYS (compliance-flag, ikke i sig selv skip-grund
              da STOCK Act tillader op til 45 dage — men bruges til alarm/logging)
"""

from __future__ import annotations
import logging
import statistics
from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# --- Kalibrerbare tærskelværdier (bør fastsættes via backtesting, ikke gættes) ---
PRICE_DRIFT_THRESHOLD = 0.08        # 8% bevægelse siden medlemmets transaktionsdato
VOLATILITY_MULTIPLIER = 2.0         # skip hvis nuværende vol > 2x 90-dages baseline
STOCK_ACT_MAX_DAYS = 45


class SignalDecision(str, Enum):
    EXECUTE = "execute"
    SKIP_PRICE_DRIFT = "skip_price_drift"
    SKIP_HIGH_VOLATILITY = "skip_high_volatility"
    SKIP_STALE_DISCLOSURE = "skip_stale_disclosure"


@dataclass
class TradeSignal:
    ticker: str
    side: str                    # 'buy' | 'sell'
    source_disclosure_id: int
    transaction_date: date
    disclosure_date: date
    decision: SignalDecision
    price_at_transaction: float
    price_now: float
    price_drift_pct: float
    current_volatility: float
    baseline_volatility: float
    rationale: str


# Type-signaturer for injicerede afhængigheder (gør modulet testbart uden live API-kald)
PriceLookupFn = Callable[[str, date], Optional[float]]
HistoricalPricesFn = Callable[[str, date, int], list[float]]  # ticker, end_date, lookback_days


class DelayFilter:
    def __init__(self, price_lookup: PriceLookupFn, historical_prices: HistoricalPricesFn):
        self.price_lookup = price_lookup
        self.historical_prices = historical_prices

    def _realized_volatility(self, prices: list[float]) -> float:
        if len(prices) < 2:
            return 0.0
        returns = [(prices[i] / prices[i - 1] - 1) for i in range(1, len(prices))]
        return statistics.pstdev(returns) * (252 ** 0.5)  # annualiseret

    def evaluate(
        self,
        ticker: str,
        side: str,
        source_disclosure_id: int,
        transaction_date: date,
        disclosure_date: date,
    ) -> TradeSignal:
        price_at_txn = self.price_lookup(ticker, transaction_date)
        price_now = self.price_lookup(ticker, date.today())

        if price_at_txn is None or price_now is None:
            return TradeSignal(
                ticker, side, source_disclosure_id, transaction_date, disclosure_date,
                SignalDecision.SKIP_PRICE_DRIFT, 0.0, 0.0, 0.0, 0.0, 0.0,
                "Manglende prisdata — kan ikke verificere drift, springer over.",
            )

        drift_pct = (price_now - price_at_txn) / price_at_txn

        baseline_prices = self.historical_prices(ticker, transaction_date, 90)
        recent_prices = self.historical_prices(ticker, date.today(), 20)
        baseline_vol = self._realized_volatility(baseline_prices)
        current_vol = self._realized_volatility(recent_prices)

        days_to_disclose = (disclosure_date - transaction_date).days
        is_stale = days_to_disclose > STOCK_ACT_MAX_DAYS  # compliance-afvigelse, bør altid logges/alarmeres

        if is_stale:
            decision = SignalDecision.SKIP_STALE_DISCLOSURE
            rationale = f"Indberetning kom {days_to_disclose} dage efter transaktion — overskrider STOCK Act-grænsen, flagges til compliance-review."
        elif abs(drift_pct) > PRICE_DRIFT_THRESHOLD:
            decision = SignalDecision.SKIP_PRICE_DRIFT
            rationale = f"Pris har bevæget sig {drift_pct:.2%} siden medlemmets handel, over tærsklen {PRICE_DRIFT_THRESHOLD:.0%}."
        elif baseline_vol > 0 and current_vol > baseline_vol * VOLATILITY_MULTIPLIER:
            decision = SignalDecision.SKIP_HIGH_VOLATILITY
            rationale = f"Nuværende volatilitet ({current_vol:.2%}) er >{VOLATILITY_MULTIPLIER}x baseline ({baseline_vol:.2%})."
        else:
            decision = SignalDecision.EXECUTE
            rationale = "Inden for tærskelværdier — godkendt til eksekvering."

        return TradeSignal(
            ticker=ticker, side=side, source_disclosure_id=source_disclosure_id,
            transaction_date=transaction_date, disclosure_date=disclosure_date,
            decision=decision, price_at_transaction=price_at_txn, price_now=price_now,
            price_drift_pct=round(drift_pct * 100, 2), current_volatility=round(current_vol * 100, 2),
            baseline_volatility=round(baseline_vol * 100, 2), rationale=rationale,
        )
