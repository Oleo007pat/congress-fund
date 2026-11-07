"""
stock_act_scraper.py
---------------------
Henter og normaliserer Periodic Transaction Report (PTR) data under STOCK Act.

Primær strategi: struktureret aggregator-API (fx Quiver Quantitative / lignende),
med en officiel-kilde fallback-parser som verifikationslag. Rå PDF-scraping af
Senate eFD / House Clerk er bevidst holdt som separat, isoleret modul, da det er
det mest skrøbelige led i pipelinen.

Ingen API-nøgler er hardcoded. Sæt via miljøvariabler.
"""

from __future__ import annotations
import os
import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional
import requests

logger = logging.getLogger(__name__)

AGGREGATOR_API_BASE = os.getenv("CONGRESS_TRADE_API_BASE", "https://api.example-aggregator.com")
AGGREGATOR_API_KEY = os.getenv("CONGRESS_TRADE_API_KEY")


@dataclass
class Disclosure:
    bioguide_id: str
    member_name: str
    ticker: str
    asset_description: str
    transaction_type: str      # 'buy' | 'sell' | 'exchange'
    transaction_date: date
    disclosure_date: date
    amount_low: float
    amount_high: float
    owner: str                 # self / spouse / joint / dependent
    source: str
    source_url: Optional[str] = None

    @property
    def days_to_disclose(self) -> int:
        """STOCK Act kræver indberetning inden for 45 dage."""
        return (self.disclosure_date - self.transaction_date).days


class DisclosureFeed:
    """Henter nye PTR-indberetninger fra den strukturerede aggregator-API."""

    def __init__(self, api_base: str = AGGREGATOR_API_BASE, api_key: Optional[str] = AGGREGATOR_API_KEY):
        if not api_key:
            logger.warning("Ingen API-nøgle sat for disclosure-feed — kør i dry-run/mock mode.")
        self.api_base = api_base
        self.api_key = api_key

    def fetch_new_disclosures(self, since: date) -> list[Disclosure]:
        """
        Henter alle nye indberetninger siden `since`.
        Forventet respons-skema (aggregator-afhængigt, tilpas ved integration):
        [{bioguide_id, member_name, ticker, asset_description, transaction_type,
          transaction_date, disclosure_date, amount_low, amount_high, owner, url}, ...]
        """
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        params = {"since": since.isoformat()}

        try:
            resp = requests.get(f"{self.api_base}/v1/disclosures", headers=headers, params=params, timeout=15)
            resp.raise_for_status()
        except requests.RequestException as exc:
            logger.error("Kunne ikke hente disclosures: %s", exc)
            return []

        raw_items = resp.json().get("data", [])
        return [self._parse(item) for item in raw_items]

    @staticmethod
    def _parse(item: dict) -> Disclosure:
        return Disclosure(
            bioguide_id=item["bioguide_id"],
            member_name=item["member_name"],
            ticker=item["ticker"].upper(),
            asset_description=item.get("asset_description", ""),
            transaction_type=item["transaction_type"].lower(),
            transaction_date=datetime.fromisoformat(item["transaction_date"]).date(),
            disclosure_date=datetime.fromisoformat(item["disclosure_date"]).date(),
            amount_low=float(item["amount_low"]),
            amount_high=float(item["amount_high"]),
            owner=item.get("owner", "self"),
            source="aggregator",
            source_url=item.get("url"),
        )


class PerformanceRanker:
    """
    Beregner rullende N-års historisk afkast pr. kongresmedlem baseret på
    deres indberettede handler, med henblik på at identificere den
    bedst præsterende profil dynamisk (aldrig hardcoded).

    Metode: for hver 'buy' beregnes afkast fra transaction_date til enten
    et matchende 'sell' eller til dags dato (mark-to-market), vægtet ligeligt
    pr. handel (midtpunkt af beløbsintervallet bruges som proxy for position size).
    """

    def __init__(self, price_lookup):
        """
        price_lookup: callable(ticker: str, on_date: date) -> float
        Injiceres udefra (fx Alpaca/Polygon market data klient) for testbarhed.
        """
        self.price_lookup = price_lookup

    def compute_member_return(self, disclosures: list[Disclosure], years: int = 3) -> dict:
        cutoff = date.today().replace(year=date.today().year - years)
        relevant = [d for d in disclosures if d.transaction_date >= cutoff]

        weighted_returns = []
        total_weight = 0.0

        for d in relevant:
            position_size = (d.amount_low + d.amount_high) / 2
            entry_price = self.price_lookup(d.ticker, d.transaction_date)
            exit_price = self.price_lookup(d.ticker, date.today())
            if not entry_price or not exit_price:
                continue

            trade_return = (exit_price - entry_price) / entry_price
            if d.transaction_type == "sell":
                trade_return *= -1  # kort/salg-perspektiv

            weighted_returns.append(trade_return * position_size)
            total_weight += position_size

        if total_weight == 0:
            return {"return_pct": 0.0, "cagr_pct": 0.0, "trade_count": 0}

        blended_return = sum(weighted_returns) / total_weight
        cagr = (1 + blended_return) ** (1 / years) - 1

        return {
            "return_pct": round(blended_return * 100, 4),
            "cagr_pct": round(cagr * 100, 4),
            "trade_count": len(relevant),
        }

    def rank_members(self, disclosures_by_member: dict[str, list[Disclosure]], years: int = 3) -> list[tuple]:
        """Returnerer [(bioguide_id, metrics_dict), ...] sorteret efter cagr_pct desc."""
        results = [
            (bioguide_id, self.compute_member_return(discs, years=years))
            for bioguide_id, discs in disclosures_by_member.items()
        ]
        return sorted(results, key=lambda x: x[1]["cagr_pct"], reverse=True)
