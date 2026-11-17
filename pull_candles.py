"""One-time download of daily candlesticks for the series in the sign map.

Reads sign_map.csv, takes every series named in a series-matched pair, and
pulls daily (1440-minute) candles for each of its markets that traded at least
--min-market-volume contracts. Live markets go through the batch endpoint (up
to 100 tickers per call); historical markets have no batch endpoint and are
fetched one ticker at a time, which is where the runtime goes.

Resumable: tickers already recorded in candle_log are skipped, so an
interrupted run picks up where it stopped.

    python3 pull_candles.py --db kalshi.db
    python3 pull_candles.py --db kalshi.db --series KXCPI   # one series, for a test
    python3 pull_candles.py --db kalshi.db --interval 60 --series KXWTIW   # hourly

Daily candles go to `candles` / `candle_log`; hourly to `candles_1h` / `candle_log_1h`.
"""

from __future__ import annotations

import argparse
import csv
import os
import sqlite3
import sys
import time
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

from kalshi import Kalshi, KalshiError

DAY = 86400
MAX_ROWS_PER_CALL = 9000  # Kalshi caps a batch call near 10k candles
MAX_PERIODS_PER_WINDOW = 4999  # and rejects a time range spanning more than 5000 candles

SCHEMA = """
CREATE TABLE IF NOT EXISTS {candles} (
    ticker        TEXT,
    end_ts        INTEGER,
    yes_bid_close REAL,
    yes_ask_close REAL,
    price_close   REAL,
    price_mean    REAL,
    volume        REAL,
    open_interest REAL,
    PRIMARY KEY (ticker, end_ts)
);
CREATE TABLE IF NOT EXISTS {log} (
    ticker     TEXT PRIMARY KEY,
    tier       TEXT,
    n_candles  INTEGER,
    fetched_at INTEGER
);
"""


def _ts(value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def _num(obj: Any, *keys: str) -> Optional[float]:
    """First parseable number among keys. Live candles suffix fields with
    _dollars / _fp; historical candles do not, so callers pass both forms."""
    if not isinstance(obj, dict):
        return None
    for k in keys:
        v = obj.get(k)
        if v not in (None, ""):
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return None


def candle_row(ticker: str, c: Dict[str, Any]) -> tuple:
    bid, ask, price = c.get("yes_bid") or {}, c.get("yes_ask") or {}, c.get("price") or {}
    return (
        ticker,
        int(c["end_period_ts"]),
        _num(bid, "close_dollars", "close"),
        _num(ask, "close_dollars", "close"),
        _num(price, "close_dollars", "close"),
        _num(price, "mean_dollars", "mean"),
        _num(c, "volume_fp", "volume"),
        _num(c, "open_interest_fp", "open_interest"),
    )


def tables(interval: int) -> Dict[str, str]:
    return {"candles": "candles", "log": "candle_log"} if interval == 1440 else \
        {"candles": f"candles_{interval // 60}h" if interval >= 60 else f"candles_{interval}m",
         "log": f"candle_log_{interval // 60}h" if interval >= 60 else f"candle_log_{interval}m"}


def store(conn: sqlite3.Connection, ticker: str, tier: str, candles: List[Dict[str, Any]],
          tbl: Dict[str, str]) -> None:
    rows = [candle_row(ticker, c) for c in candles if c.get("end_period_ts")]
    conn.executemany(f"INSERT OR REPLACE INTO {tbl['candles']} VALUES (?,?,?,?,?,?,?,?)", rows)
    conn.execute(f"INSERT OR REPLACE INTO {tbl['log']} VALUES (?,?,?,?)",
                 (ticker, tier, len(rows), int(time.time())))


def sign_map_series(path: str) -> List[str]:
    with open(path) as fh:
        rows = [r for r in csv.DictReader(fh) if r["matching"] == "series"]
    return sorted({r["leader"] for r in rows} | {r["laggard"] for r in rows})


def windows(start: int, end: int, interval: int) -> Iterable[tuple]:
    """Split [start, end] into ranges of at most MAX_PERIODS_PER_WINDOW candles."""
    step = MAX_PERIODS_PER_WINDOW * interval * 60
    while start < end:
        yield start, min(start + step, end)
        start += step


def live_batches(markets: List[tuple], now: int, interval: int = 1440) -> Iterable[tuple]:
    """Group live markets into calls of <= 100 tickers whose shared window keeps
    tickers x days under the per-call row cap."""
    markets = sorted(markets, key=lambda m: m[1])
    batch: List[tuple] = []
    for m in markets:
        trial = batch + [m]
        start = min(x[1] for x in trial)
        end = min(max(x[2] for x in trial), now)
        periods = (end - start) // (interval * 60) + 1
        if batch and (len(trial) > 100 or len(trial) * periods > MAX_ROWS_PER_CALL):
            yield batch
            batch = [m]
        else:
            batch = trial
    if batch:
        yield batch


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description="Download daily candles for sign-map series")
    ap.add_argument("--db", default="kalshi.db")
    ap.add_argument("--sign-map", default="sign_map.csv")
    ap.add_argument("--series", nargs="*", help="override: pull only these series")
    ap.add_argument("--min-market-volume", type=float, default=100)
    ap.add_argument("--rps", type=float, default=4.0)
    ap.add_argument("--interval", type=int, default=1440, choices=[1, 60, 1440],
                    help="candle length in minutes")
    ap.add_argument("--max-db-gb", type=float, default=None,
                    help="stop cleanly (exit 2) if the database grows past this")
    args = ap.parse_args(argv)
    tbl = tables(args.interval)

    series = args.series or sign_map_series(args.sign_map)
    conn = sqlite3.connect(args.db)
    conn.executescript(SCHEMA.format(**tbl))
    done = {r[0] for r in conn.execute(f"SELECT ticker FROM {tbl['log']}")}
    ph = ",".join("?" * len(series))
    rows = conn.execute(
        f"SELECT ticker, open_time, close_time, tier FROM markets "
        f"WHERE volume >= ? AND series_ticker IN ({ph})", [args.min_market_volume] + series
    ).fetchall()
    now = int(time.time())
    todo = [(t, _ts(o), _ts(c), tier) for t, o, c, tier in rows
            if t not in done and _ts(o) and _ts(c)]
    live = [m for m in todo if m[3] == "live"]
    hist = [m for m in todo if m[3] == "historical"]
    print(f"[candles] {len(series)} series, {len(rows)} markets; {len(done)} already done; "
          f"to fetch: {len(live)} live, {len(hist)} historical")

    api = Kalshi(rps=args.rps)
    started = time.time()
    failed = 0

    # One-minute candles, and any market too long for one window at coarser
    # intervals, are fetched one ticker at a time, window by window.
    period_s = args.interval * 60
    long_live = [m for m in live if args.interval == 1
                 or (min(m[2], now) - m[1]) // period_s + 1 > MAX_PERIODS_PER_WINDOW]
    long_set = {m[0] for m in long_live}
    for ticker, start, end, _tier in long_live:
        got_all: List[Dict[str, Any]] = []
        try:
            for a, b in windows(start, min(end, now), args.interval):
                got = api.batch_candlesticks([ticker], a, b, args.interval)
                got_all += (got[0].get("candlesticks") or []) if got else []
        except KalshiError as exc:
            print(f"[candles] {ticker}: HTTP {exc.status}")
            failed += 1
            continue
        store(conn, ticker, "live", got_all, tbl)
        conn.commit()
    short_live = [m for m in live if m[0] not in long_set]
    for batch in live_batches(short_live, now, args.interval):
        start = min(m[1] for m in batch)
        end = min(max(m[2] for m in batch), now)
        try:
            got = api.batch_candlesticks([m[0] for m in batch], start, end, args.interval)
        except KalshiError as exc:
            print(f"[candles] live batch failed ({exc.status}); {len(batch)} tickers left for re-run")
            failed += len(batch)
            continue
        by_ticker = {g.get("market_ticker"): g.get("candlesticks") or [] for g in got}
        for m in batch:
            store(conn, m[0], "live", by_ticker.get(m[0], []), tbl)
        conn.commit()
    print(f"[candles] live done: {len(live)} markets in {time.time() - started:.0f}s")

    for i, (ticker, start, end, _tier) in enumerate(hist, 1):
        got_all = []
        try:
            for a, b in windows(start, min(end, now), args.interval):
                got = api.get(f"/historical/markets/{ticker}/candlesticks",
                              {"start_ts": a, "end_ts": b, "period_interval": args.interval})
                got_all += got.get("candlesticks") or []
        except KalshiError as exc:
            failed += 1
            if exc.status != 404:
                print(f"[candles] {ticker}: HTTP {exc.status}")
            continue
        store(conn, ticker, "historical", got_all, tbl)
        if i % 200 == 0:
            conn.commit()
            if args.max_db_gb and os.path.getsize(args.db) > args.max_db_gb * 1024 ** 3:
                print(f"[candles] ABORTED: database past {args.max_db_gb} GB; re-run resumes")
                conn.close()
                return 2
            rate = i / (time.time() - started)
            print(f"[candles] historical {i}/{len(hist)}  ~{(len(hist) - i) / max(rate, 0.01) / 60:.0f} min left")
    conn.commit()

    n = conn.execute(f"SELECT COUNT(*) FROM {tbl['candles']}").fetchone()[0]
    print(f"\n[candles] done in {time.time() - started:.0f}s - {n:,} candles stored; "
          f"{failed} tickers failed (re-run to retry)")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
