"""
investor_ledger.py
-------------------
Håndterer investorindbetalinger/-hævninger via et unit-baseret NAV-system
(samme princip som fonde/ETF'er bruger), samt beregning af absolut, procentuelt
og annualiseret (CAGR) afkast pr. investor.

Unit-modellen sikrer korrekt fordeling selv når investorer ind- og udtræder
på forskellige tidspunkter til forskellig NAV.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, ROUND_HALF_UP


@dataclass
class LedgerEntry:
    investor_id: int
    entry_type: str          # deposit | withdrawal | fee | pnl_accrual
    amount_usd: Decimal
    units_delta: Decimal
    nav_per_unit: Decimal
    entry_date: date


@dataclass
class Investor:
    investor_id: int
    name: str
    units_held: Decimal = Decimal("0")
    total_deposited: Decimal = Decimal("0")
    total_withdrawn: Decimal = Decimal("0")
    ledger: list[LedgerEntry] = field(default_factory=list)


class FundLedger:
    """
    Central bogholderi for hele fonden. Al kapital repræsenteres som
    'units' (andele), værdisat løbende via `nav_per_unit`.
    """

    def __init__(self, initial_nav_per_unit: Decimal = Decimal("100.00")):
        self.nav_per_unit = initial_nav_per_unit
        self.total_units = Decimal("0")
        self.investors: dict[int, Investor] = {}

    # ---------- Kapitalbevægelser ----------

    def deposit(self, investor: Investor, amount_usd: Decimal, on_date: date) -> LedgerEntry:
        if amount_usd <= 0:
            raise ValueError("Indbetaling skal være positiv.")

        units_issued = (amount_usd / self.nav_per_unit).quantize(Decimal("0.000001"))
        investor.units_held += units_issued
        investor.total_deposited += amount_usd
        self.total_units += units_issued

        entry = LedgerEntry(investor.investor_id, "deposit", amount_usd, units_issued, self.nav_per_unit, on_date)
        investor.ledger.append(entry)
        return entry

    def withdraw(self, investor: Investor, amount_usd: Decimal, on_date: date) -> LedgerEntry:
        units_redeemed = (amount_usd / self.nav_per_unit).quantize(Decimal("0.000001"))
        if units_redeemed > investor.units_held:
            raise ValueError("Kan ikke hæve mere end investorens andel er værd.")

        investor.units_held -= units_redeemed
        investor.total_withdrawn += amount_usd
        self.total_units -= units_redeemed

        entry = LedgerEntry(investor.investor_id, "withdrawal", -amount_usd, -units_redeemed, self.nav_per_unit, on_date)
        investor.ledger.append(entry)
        return entry

    def update_nav(self, new_total_assets: Decimal) -> Decimal:
        """Kaldes dagligt/efter hver handel: genberegner NAV pr. unit ud fra fondens totale AUM."""
        if self.total_units == 0:
            return self.nav_per_unit
        self.nav_per_unit = (new_total_assets / self.total_units).quantize(Decimal("0.000001"))
        return self.nav_per_unit

    # ---------- Ejerandele & afkast ----------

    def ownership_pct(self, investor: Investor) -> Decimal:
        if self.total_units == 0:
            return Decimal("0")
        return (investor.units_held / self.total_units * 100).quantize(Decimal("0.0001"))

    def investor_returns(self, investor: Investor, as_of: date) -> dict:
        """
        Beregner:
          - absolut afkast ($)  = nuværende værdi - netto-indskudt kapital
          - procentuelt afkast (%)
          - CAGR baseret på tidsvægtet holdingsperiode (simplificeret: første indskud -> as_of)
        """
        current_value = investor.units_held * self.nav_per_unit
        net_contributed = investor.total_deposited - investor.total_withdrawn

        absolute_return = current_value - net_contributed
        pct_return = (absolute_return / net_contributed * 100) if net_contributed > 0 else Decimal("0")

        first_deposit = min(
            (e.entry_date for e in investor.ledger if e.entry_type == "deposit"),
            default=as_of,
        )
        years_held = max((as_of - first_deposit).days / 365.25, 1 / 365.25)

        if net_contributed > 0 and current_value > 0:
            cagr = (float(current_value / net_contributed) ** (1 / years_held) - 1) * 100
        else:
            cagr = 0.0

        return {
            "absolute_return_usd": absolute_return.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
            "pct_return": pct_return.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
            "cagr_pct": round(cagr, 2),
            "current_value_usd": current_value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
            "ownership_pct": self.ownership_pct(investor),
        }
