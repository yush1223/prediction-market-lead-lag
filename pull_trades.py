"""One-time download of every trade in the given series' markets (spec v7).

Historical-tier markets use /historical/trades, live ones /markets/trades;
both paginate by cursor. Resumable through trade_log.

    python3 pull_trades.py --db kalshi.db --trades-db trades.db --series KXAAAGASW

Trades go to their own file so this never contends for kalshi.db's write lock.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from datetime import datetime
from typing import Optional

from kalshi import Kalshi, KalshiError

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    trade_id   TEXT PRIMARY KEY,
    ticker     TEXT,
    ts         REAL,
    yes_price  REAL,
    count      REAL,
    taker_side TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_ticker ON trades(ticker, ts);
CREATE TABLE IF NOT EXISTS trade_log (ticker TEXT PRIMARY KEY, n INTEGER, fetched_at INTEGER);
"""


def _f(v) -> Optional[float]:
    try:
        return None if v in (None, "") else float(v)
    except (TypeError, ValueError):
        return None


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description="Download trades for a series")
    ap.add_argument("--db", default="kalshi.db", help="read-only source of the markets table")
    ap.add_argument("--trades-db", default="trades.db")
    ap.add_argument("--series", nargs="+", required=True)
    ap.add_argument("--min-market-volume", type=float, default=100)
    ap.add_argument("--rps", type=float, default=4.0)
    args = ap.parse_args(argv)

    src = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True, timeout=60)
    conn = sqlite3.connect(args.trades_db)
    conn.executescript(SCHEMA)
    done = {r[0] for r in conn.execute("SELECT ticker FROM trade_log")}
    ph = ",".join("?" * len(args.series))
    todo = [r for r in src.execute(
        f"SELECT ticker, tier FROM markets WHERE volume >= ? AND series_ticker IN ({ph})",
        [args.min_market_volume] + args.series) if r[0] not in done]
    print(f"[trades] {len(todo)} markets to fetch ({len(done)} already done)")
    api = Kalshi(rps=args.rps)
    started, failed = time.time(), 0
    for i, (ticker, tier) in enumerate(todo, 1):
        path = "/historical/trades" if tier == "historical" else "/markets/trades"
        rows = []
        try:
            for t in api.paginate(path, "trades", {"ticker": ticker}, limit=1000, label=ticker):
                ts = datetime.fromisoformat(t["created_time"].replace("Z", "+00:00")).timestamp()
                rows.append((t["trade_id"], ticker, ts, _f(t.get("yes_price_dollars") or t.get("yes_price")),
                             _f(t.get("count_fp") or t.get("count")), t.get("taker_side")))
        except KalshiError as exc:
            failed += 1
            print(f"[trades] {ticker}: HTTP {exc.status}")
            continue
        conn.executemany("INSERT OR REPLACE INTO trades VALUES (?,?,?,?,?,?)", rows)
        conn.execute("INSERT OR REPLACE INTO trade_log VALUES (?,?,?)", (ticker, len(rows), int(time.time())))
        if i % 50 == 0:
            conn.commit()
            rate = i / (time.time() - started)
            print(f"[trades] {i}/{len(todo)}  ~{(len(todo) - i) / max(rate, 0.01) / 60:.0f} min left")
    conn.commit()
    n = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    print(f"[trades] done in {time.time() - started:.0f}s - {n:,} trades stored; {failed} markets failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
