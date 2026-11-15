"""
alpaca_client.py
----------------
Tynd klient til Alpaca Market Data (kun det, price-scripts bruger).

Bekræftet mod brugerens konto (backtest_delay.py): GET /v2/stocks/bars med symbols, timeframe,
start, end, adjustment, feed, limit, next_page_token og headerne APCA-API-KEY-ID / APCA-API-SECRET-KEY.
Fra Alpacas dokumentation (ikke afprøvet her): parameteren asof (YYYY-MM-DD) og
GET /v1/corporate-actions. Se rapporten for kilder.
"""

from __future__ import annotations

import time

import requests

DATA_URL = "https://data.alpaca.markets"


class AlpacaError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class AuthError(AlpacaError):
    """401: forkerte eller manglende nøgler."""


class ForbiddenError(AlpacaError):
    """403: fx SIP-data ikke inkluderet i planen."""


class BadRequest(AlpacaError):
    """400/422: Alpaca afviser forespørgslen (fx ugyldigt symbol)."""


class AlpacaClient:
    def __init__(self, key_id, secret, base_url=DATA_URL, feed="sip", min_interval=0.35,
                 max_retries=8, backoff_base=2.0, sleep=time.sleep, session=None, log=None):
        self.headers = {"APCA-API-KEY-ID": key_id or "", "APCA-API-SECRET-KEY": secret or ""}
        self.base_url = base_url.rstrip("/")
        self.feed = feed
        self.fell_back = False
        self.min_interval = min_interval
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.sleep = sleep
        self.session = session or requests.Session()
        self.log = log or (lambda msg: None)
        self.n_calls = 0       # alle HTTP-forsøg
        self.n_429 = 0
        self.n_retries = 0
        self._last = 0.0

    # --------------------------------------------------------------- lavniveau
    def _throttle(self):
        wait = self.min_interval - (time.monotonic() - self._last)
        if wait > 0:
            self.sleep(wait)
        self._last = time.monotonic()

    def _backoff(self, attempt, retry_after=None):
        delay = min(60.0, self.backoff_base * (2 ** attempt))
        try:
            if retry_after is not None:
                delay = min(120.0, max(0.0, float(retry_after)))
        except ValueError:
            pass
        self.n_retries += 1
        self.sleep(delay)

    def get_json(self, path, params):
        url = self.base_url + path
        last = "ukendt fejl"
        for attempt in range(self.max_retries + 1):
            self._throttle()
            self.n_calls += 1
            try:
                resp = self.session.get(url, headers=self.headers, params=params, timeout=(10, 90))
            except requests.RequestException as exc:
                last = "netværksfejl (%s)" % type(exc).__name__
                self._backoff(attempt)
                continue
            sc = resp.status_code
            if sc == 200:
                try:
                    return resp.json()
                except ValueError:
                    last = "ugyldigt svar fra serveren"
                    self._backoff(attempt)
                    continue
            if sc == 429:
                self.n_429 += 1
                last = "429 (for mange kald)"
                self._backoff(attempt, resp.headers.get("Retry-After"))
                continue
            if sc >= 500:
                last = "serverfejl %d" % sc
                self._backoff(attempt)
                continue
            text = (resp.text or "")[:200]
            if sc == 401:
                raise AuthError("401 - nøglerne blev afvist", sc)
            if sc == 403:
                raise ForbiddenError("403 - %s" % text, sc)
            if sc in (400, 422):
                raise BadRequest("%d - %s" % (sc, text), sc)
            raise AlpacaError("HTTP %d - %s" % (sc, text), sc)
        raise AlpacaError("opgav efter %d forsøg: %s" % (self.max_retries + 1, last))

    # ------------------------------------------------------------------- bars
    def bars(self, symbols, start, end, feed=None, asof=None, adjustment="all"):
        """Henter alle sider. Returnerer {symbol: [bar-dicts]} kun for symboler med data."""
        params = {"symbols": ",".join(symbols), "timeframe": "1Day", "start": start, "end": end,
                  "adjustment": adjustment, "feed": feed or self.feed, "limit": 10000}
        if asof:
            params["asof"] = asof
        out = {}
        seen_tokens = set()
        while True:
            data = self.get_json("/v2/stocks/bars", params)
            for sym, bars in (data.get("bars") or {}).items():
                out.setdefault(sym, []).extend(bars or [])
            token = data.get("next_page_token")
            if not token:
                return out
            if token in seen_tokens:
                raise AlpacaError("pagineringen gentager sig - afbryder")
            seen_tokens.add(token)
            params["page_token"] = token

    def bars_auto(self, symbols, start, end, asof=None):
        """Som bars(), men skifter permanent fra sip til iex ved 403."""
        try:
            return self.bars(symbols, start, end, asof=asof)
        except ForbiddenError:
            if self.feed != "sip":
                raise
            self.log("  Din Alpaca-plan giver ikke SIP-data. Skifter til IEX-data.")
            self.feed = "iex"
            self.fell_back = True
            return self.bars(symbols, start, end, asof=asof)

    # ------------------------------------------------------ selskabshændelser
    def corporate_actions(self, symbols, types, start, end):
        """Returnerer liste af rå svar-sider (felter er ikke bekræftet; parses defensivt)."""
        params = {"symbols": ",".join(symbols), "types": ",".join(types),
                  "start": start, "end": end, "limit": 1000}
        pages, seen = [], set()
        while True:
            data = self.get_json("/v1/corporate-actions", params)
            pages.append(data)
            token = data.get("next_page_token") if isinstance(data, dict) else None
            if not token or token in seen:
                return pages
            seen.add(token)
            params["page_token"] = token
