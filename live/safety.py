"""Safety: paper-only guard and secret redaction."""
from __future__ import annotations

import os
import re
from urllib.parse import urlparse

PAPER_HOST = "paper-api.alpaca.markets"
PAPER_BASE = "https://" + PAPER_HOST


class NotPaperError(Exception):
    pass


def validate_paper_url(url: str | None) -> str:
    """Returns the normalised paper base URL, or raises NotPaperError.
    The host must be EXACTLY paper-api.alpaca.markets over https (no userinfo, no other port,
    no trailing-dot or look-alike host).  Only '' or '/v2' is accepted as path."""
    if not url or not url.strip():
        raise NotPaperError("ALPACA_BASE_URL er ikke sat. Kørslen afvises: kun papirkontoen er tilladt.")
    try:
        p = urlparse(url.strip())
        host, port = p.hostname, p.port
    except ValueError:
        raise NotPaperError("ALPACA_BASE_URL kan ikke læses. Kørslen afvises.") from None
    ok = (p.scheme == "https" and host == PAPER_HOST and p.username is None and p.password is None
          and port in (None, 443) and p.path.rstrip("/") in ("", "/v2") and not p.query and not p.fragment
          and not p.params)
    if not ok:
        shown = host or "(ukendt)"
        raise NotPaperError(f"ALPACA_BASE_URL peger på '{shown}', ikke præcis {PAPER_BASE}. "
                            "Kørslen afvises: kun papirkontoen er tilladt.")
    return PAPER_BASE


_SECRET_ENV = ("ALPACA_KEY_ID", "ALPACA_SECRET_KEY")


def redact(text: object, secrets: list[str] | None = None) -> str:
    """Replaces any key/secret value (from the environment or the given list) with ***."""
    s = str(text)
    vals = list(secrets or []) + [os.environ.get(k, "") for k in _SECRET_ENV]
    for v in vals:
        if v and len(v) >= 4:
            s = s.replace(v, "***")
    return re.sub(r"(APCA-API-(?:KEY-ID|SECRET-KEY)['\"]?\s*[:=]\s*['\"]?)[^'\",\s}]+", r"\1***", s)
