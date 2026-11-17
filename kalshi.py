"""Minimal Kalshi REST client.

Covers the pieces the feasibility work needs: token-bucket rate limiting,
cursor pagination, 429/5xx backoff, optional request signing, and parsers for
the fixed-point string fields the API now returns.

Public market data (markets, events, orderbook, trades, candlesticks) needs no
API key. Auth is only wired up so the same client can be reused later for
private endpoints; leave the env vars unset and everything here still works.
"""

from __future__ import annotations

import base64
import os
import random
import threading
import time
from typing import Any, Dict, Iterable, Iterator, List, Optional

import requests

DEFAULT_BASE = "https://external-api.kalshi.com/trade-api/v2"
API_PREFIX = "/trade-api/v2"

# Kalshi prices are dollars per contract in [0, 1]; a contract settles at $1.
CONTRACT_NOTIONAL = 1.0


# --------------------------------------------------------------------------
# Fixed-point field parsing
# --------------------------------------------------------------------------
# The API migrated money/count fields to decimal *strings* ("0.5600") under
# names like `yes_bid_dollars` and `volume_fp`. Older integer-cent fields
# (`yes_bid`, `volume`) may still be present. Read the new name first and fall
# back, so this keeps working across the migration in either direction.

def dollars(obj: Optional[Dict[str, Any]], base: str) -> Optional[float]:
    """Price field as float dollars. `base` is the stem, e.g. "yes_bid"."""
    if not obj:
        return None
    v = obj.get(f"{base}_dollars")
    if v not in (None, ""):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
    v = obj.get(base)
    if v in (None, ""):
        return None
    try:
        return float(v) / 100.0  # legacy integer cents
    except (TypeError, ValueError):
        return None


def count(obj: Optional[Dict[str, Any]], base: str) -> Optional[float]:
    """Quantity field as float contracts. `base` is the stem, e.g. "volume"."""
    if not obj:
        return None
    for name in (f"{base}_fp", base):
        v = obj.get(name)
        if v not in (None, ""):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
    return None


def level_pair(entry: Any) -> Optional[tuple]:
    """Parse one order-book level, returned as a [price, size] pair."""
    if not isinstance(entry, (list, tuple)) or len(entry) < 2:
        return None
    try:
        price, size = float(entry[0]), float(entry[1])
    except (TypeError, ValueError):
        return None
    # Guard against a legacy cents-based book sneaking through.
    if price > 1.5:
        price /= 100.0
    return price, size


def series_of(market: Dict[str, Any]) -> Optional[str]:
    """Series ticker for a market.

    The market object does not carry `series_ticker`, but tickers are
    SERIES-DATE-STRIKE and event tickers are SERIES-DATE, so the series is the
    leading segment. Needed because the live candlestick path requires it.
    """
    for key in ("series_ticker", "event_ticker", "ticker"):
        val = market.get(key)
        if val:
            if key == "series_ticker":
                return val
            return str(val).split("-")[0] or None
    return None


# --------------------------------------------------------------------------
# Rate limiting
# --------------------------------------------------------------------------

class RateLimiter:
    """Token bucket over requests/second.

    Kalshi bills most reads at 10 tokens and lower tiers hold roughly three
    seconds of budget, so an idle client may burst. Modelling it as
    requests/second with a 3s burst is close enough and stays well clear of
    the 429 boundary.
    """

    def __init__(self, rps: float, burst_seconds: float = 3.0) -> None:
        self.rps = max(0.1, float(rps))
        self.capacity = max(1.0, self.rps * burst_seconds)
        self._tokens = self.capacity
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        # Serialising the wait inside the lock is intentional: it keeps the
        # pacing correct when the recorder's loops share one client.
        with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(
                    self.capacity, self._tokens + (now - self._last) * self.rps
                )
                self._last = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                time.sleep((1.0 - self._tokens) / self.rps)


# --------------------------------------------------------------------------
# Optional request signing
# --------------------------------------------------------------------------

def _load_private_key(path: str):
    from cryptography.hazmat.primitives import serialization

    with open(path, "rb") as fh:
        return serialization.load_pem_private_key(fh.read(), password=None)


def _sign(key, message: bytes) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ed25519, padding

    if isinstance(key, ed25519.Ed25519PrivateKey):
        return key.sign(message)
    # RSA keys sign with RSA-PSS over SHA-256, salt length = digest length.
    return key.sign(
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )


class KalshiError(RuntimeError):
    def __init__(self, status: int, body: str, url: str) -> None:
        super().__init__(f"HTTP {status} from {url}: {body[:400]}")
        self.status = status
        self.body = body
        self.url = url


class Kalshi:
    """Read-only Kalshi client.

    env vars (all optional):
      KALSHI_BASE_URL        override the REST base
      KALSHI_API_KEY_ID      key id, enables signing
      KALSHI_PRIVATE_KEY     path to the PEM private key
      KALSHI_RPS             requests/second budget (default 4)
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        rps: Optional[float] = None,
        timeout: float = 30.0,
        max_retries: int = 5,
    ) -> None:
        self.base_url = (base_url or os.environ.get("KALSHI_BASE_URL") or DEFAULT_BASE).rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.limiter = RateLimiter(float(rps or os.environ.get("KALSHI_RPS") or 4))
        self.session = requests.Session()
        self.session.headers["Accept"] = "application/json"

        self._key_id = os.environ.get("KALSHI_API_KEY_ID")
        self._key = None
        key_path = os.environ.get("KALSHI_PRIVATE_KEY")
        if self._key_id and key_path:
            try:
                self._key = _load_private_key(key_path)
            except Exception as exc:  # noqa: BLE001 - signing is optional
                print(f"[kalshi] signing disabled ({exc}); public endpoints still work")
                self._key = None

    # -- internals ---------------------------------------------------------

    def _auth_headers(self, method: str, path: str) -> Dict[str, str]:
        if not self._key:
            return {}
        ts = str(int(time.time() * 1000))
        # Sign timestamp + uppercase method + path from the API root, with the
        # query string stripped.
        message = f"{ts}{method.upper()}{API_PREFIX}{path.split('?')[0]}".encode()
        return {
            "KALSHI-ACCESS-KEY": self._key_id or "",
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(_sign(self._key, message)).decode(),
        }

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if not path.startswith("/"):
            path = "/" + path
        url = self.base_url + path
        clean = {k: v for k, v in (params or {}).items() if v is not None}

        last: Optional[Exception] = None
        for attempt in range(self.max_retries):
            self.limiter.acquire()
            try:
                resp = self.session.get(
                    url, params=clean, headers=self._auth_headers("GET", path), timeout=self.timeout
                )
            except requests.RequestException as exc:
                last = exc
                time.sleep(min(30.0, 2**attempt) + random.random())
                continue

            if resp.status_code == 200:
                return resp.json()

            # 429 carries no retry header, so back off and retry. 5xx likewise.
            if resp.status_code == 429 or resp.status_code >= 500:
                last = KalshiError(resp.status_code, resp.text, resp.url)
                time.sleep(min(30.0, 2**attempt) + random.random())
                continue

            raise KalshiError(resp.status_code, resp.text, resp.url)

        raise last or KalshiError(0, "exhausted retries", url)

    def paginate(
        self,
        path: str,
        key: str,
        params: Optional[Dict[str, Any]] = None,
        limit: int = 1000,
        max_pages: Optional[int] = None,
        progress_every: int = 20,
        label: str = "",
    ) -> Iterator[Dict[str, Any]]:
        """Walk a cursor-paginated list endpoint, yielding items.

        Falls back to a smaller page size if the endpoint rejects `limit`, and
        stops if the cursor repeats (a silent-loop guard).
        """
        params = dict(params or {})
        params["limit"] = limit
        cursor: Optional[str] = None
        seen_cursors: set = set()
        pages = 0
        total = 0

        while True:
            if cursor:
                params["cursor"] = cursor
            try:
                payload = self.get(path, params)
            except KalshiError as exc:
                if exc.status == 400 and params.get("limit", 0) > 100:
                    params["limit"] = 100
                    continue
                raise

            items = payload.get(key) or []
            for item in items:
                yield item
            total += len(items)
            pages += 1

            if progress_every and pages % progress_every == 0:
                print(f"[kalshi] {label or path}: {total} rows after {pages} pages")

            cursor = payload.get("cursor") or None
            if not cursor or not items:
                break
            if cursor in seen_cursors:
                print(f"[kalshi] {label or path}: cursor repeated, stopping at {total} rows")
                break
            seen_cursors.add(cursor)
            if max_pages and pages >= max_pages:
                break

    # -- endpoints ---------------------------------------------------------

    def markets(
        self, status: Optional[str] = None, mve_filter: Optional[str] = "exclude", **kw
    ) -> Iterator[Dict[str, Any]]:
        """GET /markets. status filter: unopened|open|paused|closed|settled.

        Note the filter vocabulary differs from the `status` value on the
        returned object (initialized/active/closed/determined/finalized/...).

        mve_filter defaults to "exclude". Kalshi auto-generates an enormous
        number of multivariate parlay markets; without this the list is so
        large that paging never terminates in practice. Parlays are also
        combinations of other markets, so they can never be an independent
        observation. Pass mve_filter=None only if you specifically want them.
        """
        params = {"status": status, "mve_filter": mve_filter}
        params.update(kw)
        yield from self.paginate("/markets", "markets", params, label=f"markets[{status or 'any'}]")

    def historical_markets(
        self, mve_filter: Optional[str] = "exclude", **kw
    ) -> Iterator[Dict[str, Any]]:
        """GET /historical/markets - settled markets older than the cutoff.

        Same parlay flood as the live endpoint, same filter.
        """
        params = {"mve_filter": mve_filter}
        params.update(kw)
        yield from self.paginate(
            "/historical/markets", "markets", params, label="historical_markets"
        )

    def series(self, **kw) -> Iterator[Dict[str, Any]]:
        """GET /series - the series catalogue."""
        yield from self.paginate("/series", "series", kw, limit=200, label="series")

    def events(self, status: Optional[str] = None, **kw) -> Iterator[Dict[str, Any]]:
        """GET /events. Carries `category`, which the market object lacks."""
        params = {"status": status}
        params.update(kw)
        yield from self.paginate(
            "/events", "events", params, limit=200, label=f"events[{status or 'any'}]"
        )

    def orderbook(self, ticker: str, depth: Optional[int] = None) -> Dict[str, Any]:
        """GET /markets/{ticker}/orderbook.

        Returns BIDS ONLY, under `orderbook_fp` as `yes_dollars` / `no_dollars`,
        because YES and NO are reciprocal: the best YES ask is
        1 - (best NO bid). Store both raw sides and derive asks downstream.
        """
        return self.get(f"/markets/{ticker}/orderbook", {"depth": depth})

    def batch_candlesticks(
        self, tickers: Iterable[str], start_ts: int, end_ts: int, period_interval: int = 1440
    ) -> List[Dict[str, Any]]:
        """GET /markets/candlesticks - up to 100 tickers, 10k candles per call.

        period_interval must be exactly 1, 60 or 1440 minutes. Candlesticks do
        not paginate, so windows have to be chunked by the caller.
        """
        batch = list(tickers)[:100]
        if not batch:
            return []
        payload = self.get(
            "/markets/candlesticks",
            {
                "market_tickers": ",".join(batch),
                "start_ts": start_ts,
                "end_ts": end_ts,
                "period_interval": period_interval,
            },
        )
        return payload.get("markets") or []

    def trades(self, ticker: Optional[str] = None, **kw) -> Iterator[Dict[str, Any]]:
        """GET /markets/trades. Trades older than the cutoff are on
        /historical/trades instead."""
        params = {"ticker": ticker}
        params.update(kw)
        yield from self.paginate("/markets/trades", "trades", params, label="trades")


def chunked(items: Iterable[Any], size: int) -> Iterator[List[Any]]:
    buf: List[Any] = []
    for item in items:
        buf.append(item)
        if len(buf) >= size:
            yield buf
            buf = []
    if buf:
        yield buf
