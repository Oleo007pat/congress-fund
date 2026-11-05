"""
bargo_feed.py
-------------
Henter kongreshandler fra Bargo Congress Trades API (gratis) og
normaliserer dem til vores eget format.

Bemærk: Bargos vilkår kræver synlig henvisning til Bargo, hvis data
vises eller bruges offentligt. Gratis-versionen dækker kun de seneste
ca. 3 måneder, så den kan ikke bruges til backtest over flere år.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from typing import Optional

import requests

BASE_URL = "https://www.bargo.ai/free-apis/congress/v1"
API_KEY = os.getenv("BARGO_API_KEY")  # valgfri, hæver dagsgrænsen

# Bargo skriver 'purchase'/'sale'. Vi bruger 'buy'/'sell' i resten af koden.
SIDE_MAP = {"purchase": "buy", "sale": "sell", "exchange": "exchange"}


@dataclass
class Trade:
    member: str
    member_slug: str
    chamber: str
    ticker: str
    side: str                    # 'buy' | 'sell' | 'exchange' | 'other'
    amount_low: float
    amount_high: float
    transaction_date: date
    disclosure_date: date
    est_price: Optional[float]   # kurs omkring medlemmets handel
    recent_price: Optional[float]

    @property
    def days_to_disclose(self) -> int:
        return (self.disclosure_date - self.transaction_date).days

    @property
    def price_drift_pct(self) -> Optional[float]:
        """Kursændring i % siden medlemmets handel."""
        if not self.est_price or not self.recent_price:
            return None
        return (self.recent_price - self.est_price) / self.est_price * 100


def parse_trade(item: dict) -> Optional[Trade]:
    """Laver én række fra API'et om til et Trade. Returnerer None, hvis rækken er ufuldstændig."""
    try:
        return Trade(
            member=item["member"],
            member_slug=item.get("member_slug", ""),
            chamber=item.get("chamber", ""),
            ticker=item["ticker"].upper(),
            side=SIDE_MAP.get(item["type"].lower(), "other"),
            amount_low=float(item["amount_low"]),
            amount_high=float(item["amount_high"]),
            transaction_date=date.fromisoformat(item["transaction_date"]),
            disclosure_date=date.fromisoformat(item["disclosure_date"]),
            est_price=item.get("est_price"),
            recent_price=item.get("recent_price"),
        )
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def fetch_trades(ticker: Optional[str] = None, chamber: Optional[str] = None, limit: int = 20) -> list[Trade]:
    """Henter de seneste handler. Returnerer en tom liste, hvis kaldet fejler."""
    headers = {"X-Api-Key": API_KEY} if API_KEY else {}
    params: dict = {"limit": limit}
    if ticker:
        params["ticker"] = ticker
    if chamber:
        params["chamber"] = chamber

    try:
        resp = requests.get(f"{BASE_URL}/trades", headers=headers, params=params, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as exc:
        print(f"Kunne ikke hente handler: {exc}")
        return []

    trades = [parse_trade(item) for item in resp.json().get("trades", [])]
    return [t for t in trades if t is not None]


if __name__ == "__main__":
    for t in fetch_trades(ticker="NVDA", limit=5):
        drift = t.price_drift_pct
        drift_txt = f"{drift:+.1f}%" if drift is not None else "ukendt"
        print(
            f"{t.member:<18} {t.side:<5} {t.ticker:<5} "
            f"handlet {t.transaction_date}, indberettet efter {t.days_to_disclose} dage, "
            f"kursændring siden: {drift_txt}"
        )
    print("Data: Bargo Congress Trades")
