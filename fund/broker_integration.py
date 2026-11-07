"""
broker_integration.py
----------------------
Wrapper omkring Alpaca (eksekvering) og Plaid (bank-tilknytning/ACH).
Ingen nøgler hardcoded — læses fra miljøvariabler. Alle netværkskald har
try/except og logges, så et fejlet kald aldrig fejler stille.
"""

from __future__ import annotations
import os
import logging
from dataclasses import dataclass
from typing import Optional
import requests

logger = logging.getLogger(__name__)

ALPACA_BASE_URL = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")  # default: paper trading
ALPACA_KEY_ID = os.getenv("ALPACA_KEY_ID")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")

PLAID_BASE_URL = os.getenv("PLAID_BASE_URL", "https://sandbox.plaid.com")  # default: sandbox
PLAID_CLIENT_ID = os.getenv("PLAID_CLIENT_ID")
PLAID_SECRET = os.getenv("PLAID_SECRET")


@dataclass
class OrderResult:
    success: bool
    broker_order_id: Optional[str]
    filled_price: Optional[float]
    status: str
    raw_response: Optional[dict] = None


class AlpacaClient:
    """Minimal wrapper omkring Alpaca Trading API v2."""

    def __init__(self):
        if not (ALPACA_KEY_ID and ALPACA_SECRET_KEY):
            logger.warning("Alpaca-nøgler ikke sat — kun mock/dry-run understøttet.")
        self._headers = {
            "APCA-API-KEY-ID": ALPACA_KEY_ID or "",
            "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY or "",
        }

    def get_last_price(self, ticker: str) -> Optional[float]:
        try:
            resp = requests.get(
                f"https://data.alpaca.markets/v2/stocks/{ticker}/trades/latest",
                headers=self._headers, timeout=10,
            )
            resp.raise_for_status()
            return float(resp.json()["trade"]["p"])
        except (requests.RequestException, KeyError) as exc:
            logger.error("Kunne ikke hente pris for %s: %s", ticker, exc)
            return None

    def submit_market_order(self, ticker: str, side: str, quantity: float, client_order_id: str) -> OrderResult:
        """
        side: 'buy' | 'sell'. Bruger market-ordre for simpelhed — for større
        ordrestørrelser bør TWAP/VWAP-algo-ordrer overvejes (se rebalancing_model.py
        for slippage-argumentation).
        """
        payload = {
            "symbol": ticker,
            "qty": str(quantity),
            "side": side,
            "type": "market",
            "time_in_force": "day",
            "client_order_id": client_order_id,
        }
        try:
            resp = requests.post(f"{ALPACA_BASE_URL}/v2/orders", json=payload, headers=self._headers, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            return OrderResult(
                success=True, broker_order_id=data.get("id"),
                filled_price=float(data["filled_avg_price"]) if data.get("filled_avg_price") else None,
                status=data.get("status", "unknown"), raw_response=data,
            )
        except requests.RequestException as exc:
            logger.error("Ordre fejlede for %s: %s", ticker, exc)
            return OrderResult(success=False, broker_order_id=None, filled_price=None, status="error")


class PlaidClient:
    """Minimal wrapper til bankkonto-tilknytning og ACH-transfers via Plaid."""

    def __init__(self):
        if not (PLAID_CLIENT_ID and PLAID_SECRET):
            logger.warning("Plaid-nøgler ikke sat — kun mock/dry-run understøttet.")

    def create_link_token(self, investor_id: int) -> Optional[str]:
        payload = {
            "client_id": PLAID_CLIENT_ID,
            "secret": PLAID_SECRET,
            "user": {"client_user_id": str(investor_id)},
            "client_name": "Congress Tracker Fund",
            "products": ["auth", "transfer"],
            "country_codes": ["US"],
            "language": "en",
        }
        try:
            resp = requests.post(f"{PLAID_BASE_URL}/link/token/create", json=payload, timeout=10)
            resp.raise_for_status()
            return resp.json().get("link_token")
        except requests.RequestException as exc:
            logger.error("Plaid link_token fejlede: %s", exc)
            return None

    def initiate_ach_transfer(self, access_token: str, account_id: str, amount_usd: float) -> Optional[str]:
        """Returnerer transfer_id ved succes."""
        payload = {
            "client_id": PLAID_CLIENT_ID,
            "secret": PLAID_SECRET,
            "access_token": access_token,
            "account_id": account_id,
            "type": "debit",
            "network": "ach",
            "amount": f"{amount_usd:.2f}",
            "ach_class": "ppd",
        }
        try:
            resp = requests.post(f"{PLAID_BASE_URL}/transfer/create", json=payload, timeout=10)
            resp.raise_for_status()
            return resp.json().get("transfer", {}).get("id")
        except requests.RequestException as exc:
            logger.error("Plaid ACH-transfer fejlede: %s", exc)
            return None
