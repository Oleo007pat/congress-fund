"""
committee_filter.py
--------------------
Verificerer løbende komité-medlemskab (Congress.gov / ProPublica Congress API)
og filtrerer handler efter relevans ift. medlemmets udvalgsposter.

Formål: en handel i en banksektor-aktie er langt mere signifikant/interessant
hvis medlemmet sidder i Financial Services-udvalget end hvis ikke.
"""

from __future__ import annotations
import os
import logging
from dataclasses import dataclass
from datetime import date
from typing import Optional
import requests

logger = logging.getLogger(__name__)

CONGRESS_API_BASE = "https://api.congress.gov/v3"
CONGRESS_API_KEY = os.getenv("CONGRESS_GOV_API_KEY")

# Simpel sektor-til-udvalg mapping — udvid/kalibrér efter behov.
SECTOR_COMMITTEE_MAP = {
    "financials": {"Financial Services", "Banking, Housing, and Urban Affairs"},
    "defense": {"Armed Services"},
    "energy": {"Energy and Commerce", "Energy and Natural Resources"},
    "healthcare": {"Energy and Commerce", "Health, Education, Labor and Pensions"},
    "technology": {"Commerce, Science, and Transportation", "Energy and Commerce"},
}


@dataclass
class CommitteeMembership:
    bioguide_id: str
    committee_name: str
    start_date: date
    end_date: Optional[date]
    role: str

    @property
    def is_active(self) -> bool:
        return self.end_date is None or self.end_date >= date.today()


class CommitteeService:
    def __init__(self, api_key: Optional[str] = CONGRESS_API_KEY):
        if not api_key:
            logger.warning("Ingen Congress.gov API-nøgle sat — kør i mock mode.")
        self.api_key = api_key

    def fetch_memberships(self, bioguide_id: str) -> list[CommitteeMembership]:
        """Henter aktuelle og historiske udvalgsmedlemskaber for et medlem."""
        params = {"api_key": self.api_key} if self.api_key else {}
        try:
            resp = requests.get(
                f"{CONGRESS_API_BASE}/member/{bioguide_id}/committee-assignments",
                params=params, timeout=15,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            logger.error("Kunne ikke hente committee data for %s: %s", bioguide_id, exc)
            return []

        items = resp.json().get("committeeAssignments", [])
        return [
            CommitteeMembership(
                bioguide_id=bioguide_id,
                committee_name=item["committeeName"],
                start_date=date.fromisoformat(item["startDate"]),
                end_date=date.fromisoformat(item["endDate"]) if item.get("endDate") else None,
                role=item.get("role", "member"),
            )
            for item in items
        ]

    def verify_still_active(self, bioguide_id: str, committee_name: str) -> bool:
        """Bruges i den kvartalsvise 'stadig i udvalget?'-verifikation."""
        memberships = self.fetch_memberships(bioguide_id)
        return any(m.committee_name == committee_name and m.is_active for m in memberships)


def sector_for_ticker(ticker: str, sector_lookup: dict[str, str]) -> Optional[str]:
    """sector_lookup: ekstern mapping ticker->sektor (fx fra market data provider)."""
    return sector_lookup.get(ticker.upper())


def is_trade_committee_relevant(
    ticker: str,
    member_committees: list[CommitteeMembership],
    sector_lookup: dict[str, str],
) -> bool:
    """
    Returnerer True hvis aktivets sektor matcher et af medlemmets AKTIVE udvalg.
    Bruges ikke som hård eksekverings-gate (STOCK Act omfatter alle handler),
    men som et prioriterings-/risiko-flag for review og position-sizing.
    """
    sector = sector_for_ticker(ticker, sector_lookup)
    if not sector:
        return False

    relevant_committees = SECTOR_COMMITTEE_MAP.get(sector, set())
    active_committee_names = {m.committee_name for m in member_committees if m.is_active}

    return bool(relevant_committees & active_committee_names)
