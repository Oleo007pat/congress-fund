"""Alpaca PAPER broker: trading API (account, clock, calendar, positions, orders, assets) and market data.

Market data (daily bars, snapshots) reuses prices/alpaca_client.AlpacaClient.  Only the paper endpoint is
accepted (live.safety.validate_paper_url).  Keys are never printed or logged; error text is redacted.

NOTE: the response shapes below follow Alpaca's documentation and have NOT been exercised against the real
API from this environment (no network).  See live/README.md, section "Skal verificeres ved første kørsel".
"""
from __future__ import annotations

import time

import requests

from prices.alpaca_client import AlpacaClient, AlpacaError, AuthError, BadRequest, ForbiddenError, DATA_URL
from live.safety import redact, validate_paper_url


class OrderRejected(AlpacaError):
    """Alpaca refused the order (HTTP 400/403/422)."""


class DuplicateClientOrderId(AlpacaError):
    pass


class Broker:
    def __init__(self, key_id: str, secret: str, base_url: str, session=None, sleep=time.sleep,
                 max_retries: int = 4, data_client: AlpacaClient | None = None):
        self.base_url = validate_paper_url(base_url)          # raises NotPaperError
        self._headers = {"APCA-API-KEY-ID": key_id or "", "APCA-API-SECRET-KEY": secret or ""}
        self._secrets = [key_id or "", secret or ""]
        self.session = session or requests.Session()
        self.sleep = sleep
        self.max_retries = max_retries
        self.data = data_client or AlpacaClient(key_id, secret, base_url=DATA_URL, feed="sip", sleep=sleep,
                                                session=self.session)

    def __repr__(self) -> str:                                  # never expose credentials
        return f"<Broker paper {self.base_url}>"

    # ------------------------------------------------------------------ low level
    def _request(self, method: str, path: str, params=None, body=None, retry: bool = True):
        url = self.base_url + "/v2" + path
        last = "ukendt fejl"
        attempts = self.max_retries + 1 if retry else 1
        for attempt in range(attempts):
            try:
                resp = self.session.request(method, url, headers=self._headers, params=params, json=body,
                                            timeout=(10, 60))
            except requests.RequestException as exc:
                last = "netværksfejl (%s)" % type(exc).__name__
                if attempt + 1 < attempts:
                    self.sleep(min(30.0, 2.0 * 2 ** attempt))
                    continue
                raise AlpacaError(last)
            sc = resp.status_code
            if sc == 429 or sc >= 500:
                last = "HTTP %d" % sc
                if attempt + 1 < attempts:
                    self.sleep(min(30.0, 2.0 * 2 ** attempt))
                    continue
                raise AlpacaError(last, sc)
            return sc, resp
        raise AlpacaError(last)

    def _json(self, resp):
        try:
            return resp.json()
        except ValueError:
            raise AlpacaError("ugyldigt svar fra Alpaca", resp.status_code)

    def _get(self, path: str, params=None, allow_404: bool = False):
        sc, resp = self._request("GET", path, params=params)
        if sc == 200:
            return self._json(resp)
        if sc == 404 and allow_404:
            return None
        text = redact((resp.text or "")[:200], self._secrets)
        if sc == 401:
            raise AuthError("401 - nøglerne blev afvist", sc)
        if sc == 403:
            raise ForbiddenError("403 - %s" % text, sc)
        raise AlpacaError("HTTP %d - %s" % (sc, text), sc)

    # ------------------------------------------------------------------ trading
    def account(self) -> dict:
        return self._get("/account")

    def clock(self) -> dict:
        return self._get("/clock")

    def calendar(self, start: str, end: str) -> list[dict]:
        return self._get("/calendar", {"start": start, "end": end})

    def positions(self) -> list[dict]:
        return self._get("/positions")

    def orders(self, after: str | None = None, status: str = "all", limit: int = 500) -> list[dict]:
        params = {"status": status, "limit": limit, "direction": "desc", "nested": "false"}
        if after:
            params["after"] = after
        return self._get("/orders", params)

    def order_by_client_id(self, client_order_id: str) -> dict | None:
        return self._get("/orders:by_client_order_id", {"client_order_id": client_order_id}, allow_404=True)

    def asset(self, symbol: str) -> dict | None:
        from urllib.parse import quote
        return self._get("/assets/" + quote(symbol, safe=""), allow_404=True)

    def submit_order(self, symbol: str, side: str, tif: str, client_order_id: str, qty: str | None = None,
                     notional: str | None = None) -> dict:
        """Market order.  POST is never retried automatically: after a network error the caller looks the
        order up by client_order_id (idempotent)."""
        body = {"symbol": symbol, "side": side, "type": "market", "time_in_force": tif,
                "client_order_id": client_order_id}
        if qty is not None:
            body["qty"] = qty
        if notional is not None:
            body["notional"] = notional
        sc, resp = self._request("POST", "/orders", body=body, retry=False)
        if sc in (200, 201):
            return self._json(resp)
        msg = ""
        try:
            msg = str(resp.json().get("message", ""))
        except Exception:
            msg = (resp.text or "")[:200]
        msg = redact(msg, self._secrets)
        if sc == 401:
            raise AuthError("401 - nøglerne blev afvist", sc)
        if "client_order_id" in msg and "unique" in msg.lower():
            raise DuplicateClientOrderId(msg, sc)
        if sc in (400, 403, 422):
            raise OrderRejected(msg or "HTTP %d" % sc, sc)
        raise AlpacaError("HTTP %d - %s" % (sc, msg), sc)

    # ------------------------------------------------------------------ market data
    def bars(self, symbols: list[str], start: str, end: str) -> dict:
        """Daily bars, adjustment=all, feed sip (falls back permanently to iex on 403 for plans without SIP)."""
        return self.data.bars_auto(symbols, start, end)

    def snapshots(self, symbols: list[str]) -> dict:
        """IEX snapshots (latest trade / daily bar).  Accepts both response shapes."""
        data = self.data.get_json("/v2/stocks/snapshots", {"symbols": ",".join(symbols), "feed": "iex"})
        if isinstance(data, dict) and isinstance(data.get("snapshots"), dict):
            return data["snapshots"]
        return data if isinstance(data, dict) else {}
