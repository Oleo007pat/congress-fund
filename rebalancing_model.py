"""
rebalancing_model.py
---------------------
Matematisk model for:
  (A) Optimal rebalanceringsfrekvens (EOQ-inspireret cost-tradeoff)
  (B) Slippage / market impact-estimering (square-root impact-model)

------------------------------------------------------------------
(A) OPTIMAL REBALANCERINGSFREKVENS
------------------------------------------------------------------
Antagelser:
  - Mellem to rebalanceringer "drifter" porteføljen væk fra target-vægte.
    Denne drift-omkostning (tracking error) vokser ~ proportionalt med
    volatiliteten i kvadrat og tiden siden sidste rebalancering:
        DriftCost(Δt) ≈ (κ · σ² · Δt) / 2
    hvor κ er en skaleringskonstant for hvor "dyrt" tracking error er
    (fx tabt alpha eller risikostraf), σ er porteføljens volatilitet,
    Δt = 1/f er tiden mellem rebalanceringer, f = rebalanceringsfrekvens.

  - Hver rebalancering koster en fast transaktionsomkostning τ
    (kommission + forventet slippage, se del B).

  - Total gennemsnitlig omkostningsrate pr. tidsenhed:
        C(f) = τ · f + (κ · σ²) / (2f)

  - Minimér C(f) mht. f:
        dC/df = τ − (κ σ²)/(2f²) = 0
        f* = sqrt( κ σ² / (2τ) )

  Dette er strukturelt identisk med Economic Order Quantity (EOQ)-modellen
  fra lagerstyring — samme matematik, anden kontekst.

------------------------------------------------------------------
(B) SLIPPAGE / MARKET IMPACT
------------------------------------------------------------------
Square-root impact-model (Almgren-Chriss-inspireret):
    Slippage_bps = (Spread/2) + λ · σ_daily · sqrt(Q / ADV) · 10 000

  - Spread/2: halvdelen af bid-ask spreadet (umiddelbar likviditetsomkostning)
  - λ: empirisk kalibreret impact-koefficient (typisk 0.1–1.0)
  - σ_daily: aktivets daglige volatilitet
  - Q: ordrestørrelse (antal aktier eller $-værdi)
  - ADV: Average Daily Volume (gennemsnitligt dagligt handlet volumen)

Kvadratrods-leddet reflekterer den empirisk observerede "square-root law":
market impact vokser sub-lineært med ordrestørrelse relativt til likviditet.
"""

from __future__ import annotations
import math
from dataclasses import dataclass


@dataclass
class RebalanceParams:
    portfolio_volatility_annual: float   # σ, fx 0.25 for 25% årlig vol
    tracking_error_cost_coefficient: float  # κ, kalibreres empirisk (start: 1.0)
    transaction_cost_per_rebalance: float   # τ, i $ eller bps — skal matche enhed på κσ²


def optimal_rebalance_frequency(params: RebalanceParams) -> float:
    """
    Returnerer f* i rebalanceringer pr. år.
    f* = sqrt( κ σ² / (2τ) )
    """
    numerator = params.tracking_error_cost_coefficient * (params.portfolio_volatility_annual ** 2)
    denominator = 2 * params.transaction_cost_per_rebalance
    if denominator <= 0:
        raise ValueError("Transaktionsomkostning skal være positiv.")
    return math.sqrt(numerator / denominator)


def rebalance_interval_days(frequency_per_year: float) -> float:
    """Konverterer f* (pr. år) til et praktisk interval i dage."""
    if frequency_per_year <= 0:
        return float("inf")
    return 365.0 / frequency_per_year


@dataclass
class SlippageParams:
    spread_bps: float             # bid-ask spread i basispoint
    daily_volatility: float       # σ_daily, fx 0.02 for 2%
    order_size: float             # Q
    average_daily_volume: float   # ADV, samme enhed som Q (aktier eller $)
    impact_coefficient: float = 0.3  # λ, empirisk startpunkt


def estimate_slippage_bps(params: SlippageParams) -> float:
    """
    Slippage_bps = spread/2 + λ · σ_daily · sqrt(Q/ADV) · 10 000
    """
    if params.average_daily_volume <= 0:
        raise ValueError("ADV skal være positiv.")

    participation_rate = params.order_size / params.average_daily_volume
    impact_component = (
        params.impact_coefficient
        * params.daily_volatility
        * math.sqrt(participation_rate)
        * 10_000
    )
    return (params.spread_bps / 2) + impact_component


def should_split_order(order_value_usd: float, adv_usd: float, max_participation: float = 0.05) -> bool:
    """
    Simpel regel: hvis en ordre ville udgøre mere end `max_participation`
    (default 5%) af dagens gennemsnitlige handlede volumen, bør den splittes
    over flere dage / udføres via TWAP for at reducere market impact.
    """
    if adv_usd <= 0:
        return True
    return (order_value_usd / adv_usd) > max_participation


# ------------------------------------------------------------------
# Eksempel på anvendelse
# ------------------------------------------------------------------
if __name__ == "__main__":
    rebal = RebalanceParams(
        portfolio_volatility_annual=0.30,
        tracking_error_cost_coefficient=1.0,
        transaction_cost_per_rebalance=0.0015,  # 15 bps pr. rebalancering
    )
    f_star = optimal_rebalance_frequency(rebal)
    print(f"Optimal rebalanceringsfrekvens: {f_star:.2f} gange/år "
          f"(~hver {rebalance_interval_days(f_star):.0f}. dag)")

    slip = SlippageParams(
        spread_bps=5, daily_volatility=0.025, order_size=50_000,
        average_daily_volume=2_000_000, impact_coefficient=0.3,
    )
    print(f"Estimeret slippage: {estimate_slippage_bps(slip):.2f} bps")
