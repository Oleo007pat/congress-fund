"""
fee_engine.py
-------------
Automatiseret månedlig forvaltningsgebyr: 1 basispunkt (0,01%) pr. måned,
trukket pro-rata efter hver investors ejerandel på trækningstidspunktet.

1 bp/måned ≈ 12 bp/år (~0,12% p.a.) — bemærk at dette er markant lavere end
typiske 2%-fee-strukturer og bør sanity-checkes mod jeres faktiske
forretningsmodel/omkostningsbase (custodian, data, compliance osv.).
"""

from __future__ import annotations
from decimal import Decimal, ROUND_HALF_UP
from datetime import date

from .investor_ledger import FundLedger, Investor, LedgerEntry

MONTHLY_FEE_BPS = Decimal("1")  # 1 basispunkt = 0.01%


def monthly_fee_rate() -> Decimal:
    return MONTHLY_FEE_BPS / Decimal("10000")  # -> 0.0001


class FeeEngine:
    def __init__(self, ledger: FundLedger):
        self.ledger = ledger

    def accrue_monthly_fee(self, total_assets: Decimal, period_end: date) -> list[LedgerEntry]:
        """
        Beregner samlet gebyr = AUM * 0.01%, fordeler det pro-rata på investorer
        (ved at reducere deres units svarende til gebyrets dollarværdi),
        og returnerer de bogførte ledger-entries til audit-log.
        """
        fee_rate = monthly_fee_rate()
        total_fee_usd = (total_assets * fee_rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        if total_fee_usd <= 0 or self.ledger.total_units == 0:
            return []

        entries = []
        for investor in self.ledger.investors.values():
            investor_share = self.ledger.ownership_pct(investor) / Decimal("100")
            investor_fee = (total_fee_usd * investor_share).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if investor_fee <= 0:
                continue

            units_deducted = (investor_fee / self.ledger.nav_per_unit).quantize(Decimal("0.000001"))
            investor.units_held -= units_deducted
            self.ledger.total_units -= units_deducted

            entry = LedgerEntry(
                investor.investor_id, "fee", -investor_fee, -units_deducted,
                self.ledger.nav_per_unit, period_end,
            )
            investor.ledger.append(entry)
            entries.append(entry)

        return entries

    def annualized_fee_pct(self) -> Decimal:
        """1 bp/måned komponeret over 12 måneder, til rapportering."""
        monthly = monthly_fee_rate()
        annual = (Decimal("1") + monthly) ** 12 - Decimal("1")
        return (annual * 100).quantize(Decimal("0.0001"))
