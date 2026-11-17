"""Pull the Kalshi market universe into SQLite.

Grabs events (for category, which markets don't carry), markets across every
status, and the historical tier for markets settled before Kalshi's live/history
cutoff. Safe to re-run: rows are upserted on ticker.

    python3 pull_universe.py --db kalshi.db
    python3 pull_universe.py --db kalshi.db --skip-historical   # faster first look

Storage is filtered, because the full exchange does not fit on disk. Most
markets are intraday (hourly crypto, daily index ladders) and cannot take part
in a daily lead-lag study, so a market is stored only if it traded (volume > 0)
and lived at least --min-lifespan-days. Every market seen, stored or not, is
counted in series_stats, and after the pull any series whose markets are mostly
short-lived is dropped whole. The pull aborts if the database outgrows
--max-db-gb.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from datetime import datetime
from typing import Any, Dict, Iterable, Iterator, Optional

from kalshi import Kalshi, count, dollars, series_of

MARKET_STATUSES = ["open", "closed", "settled", "unopened", "paused"]
EVENT_STATUSES = ["open", "closed", "settled"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS markets (
    ticker                TEXT PRIMARY KEY,
    event_ticker          TEXT,
    series_ticker         TEXT,
    market_type           TEXT,
    status                TEXT,
    tier                  TEXT,
    yes_sub_title         TEXT,
    open_time             TEXT,
    close_time            TEXT,
    settlement_ts         TEXT,
    yes_bid               REAL,
    yes_ask               REAL,
    last_price            REAL,
    volume                REAL,
    volume_24h            REAL,
    open_interest         REAL,
    result                TEXT,
    strike_type           TEXT,
    floor_strike          REAL,
    cap_strike            REAL,
    mve_collection_ticker TEXT,
    rules_primary         TEXT,
    fetched_at            INTEGER
);
CREATE INDEX IF NOT EXISTS idx_markets_event  ON markets(event_ticker);
CREATE INDEX IF NOT EXISTS idx_markets_series ON markets(series_ticker);
CREATE INDEX IF NOT EXISTS idx_markets_settle ON markets(settlement_ts);

CREATE TABLE IF NOT EXISTS events (
    event_ticker  TEXT PRIMARY KEY,
    series_ticker TEXT,
    category      TEXT,
    title         TEXT,
    fetched_at    INTEGER
);

-- One row per series for every market seen, including the ones not stored.
-- n_short counts markets that lived under --min-lifespan-days; n_untraded
-- counts volume 0. dropped = 1 means the series was judged intraday and its
-- markets removed.
CREATE TABLE IF NOT EXISTS series_stats (
    series_ticker TEXT PRIMARY KEY,
    n_seen        INTEGER,
    n_short       INTEGER,
    n_untraded    INTEGER,
    n_stored      INTEGER,
    dropped       INTEGER
);

CREATE TABLE IF NOT EXISTS pull_log (
    started_at INTEGER,
    source     TEXT,
    rows       INTEGER,
    note       TEXT
);
"""

MARKET_COLS = [
    "ticker", "event_ticker", "series_ticker", "market_type", "status", "tier",
    "yes_sub_title", "open_time", "close_time", "settlement_ts", "yes_bid",
    "yes_ask", "last_price", "volume", "volume_24h", "open_interest", "result",
    "strike_type", "floor_strike", "cap_strike", "mve_collection_ticker",
    "rules_primary", "fetched_at",
]


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    return conn


def _float(value: Any) -> Optional[float]:
    try:
        return None if value in (None, "") else float(value)
    except (TypeError, ValueError):
        return None


def market_row(m: Dict[str, Any], tier: str, now: int) -> tuple:
    return (
        m.get("ticker"),
        m.get("event_ticker"),
        series_of(m),
        m.get("market_type"),
        m.get("status"),
        tier,
        m.get("yes_sub_title") or m.get("subtitle"),
        m.get("open_time"),
        m.get("close_time"),
        m.get("settlement_ts"),
        dollars(m, "yes_bid"),
        dollars(m, "yes_ask"),
        dollars(m, "last_price"),
        count(m, "volume"),
        count(m, "volume_24h"),
        count(m, "open_interest"),
        m.get("result"),
        m.get("strike_type"),
        _float(m.get("floor_strike")),
        _float(m.get("cap_strike")),
        m.get("mve_collection_ticker"),
        m.get("rules_primary"),
        now,
    )


def _ts(value: Any) -> Optional[float]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class DiskLimit(Exception):
    """The database outgrew --max-db-gb. Never swallowed by per-source handlers."""


class Gate:
    """Decides which markets are stored, tallies every market seen per series,
    and enforces the database size cap."""

    def __init__(self, db_path: str, min_lifespan_days: float, max_db_gb: float) -> None:
        self.db_path = db_path
        self.min_lifespan = min_lifespan_days * 86400
        self.max_bytes = max_db_gb * 1024 ** 3
        self.event_series: Dict[str, str] = {}
        # series -> [seen, short, untraded, stored]
        self.stats: Dict[str, list] = {}

    def load_event_series(self, conn: sqlite3.Connection) -> None:
        self.event_series = dict(conn.execute(
            "SELECT event_ticker, series_ticker FROM events WHERE series_ticker IS NOT NULL"))

    def admit(self, m: Dict[str, Any]) -> bool:
        series = self.event_series.get(m.get("event_ticker") or "") or series_of(m) or ""
        tally = self.stats.setdefault(series, [0, 0, 0, 0])
        tally[0] += 1
        opened, closed = _ts(m.get("open_time")), _ts(m.get("close_time"))
        short = opened is None or closed is None or closed - opened < self.min_lifespan
        untraded = not (count(m, "volume") or 0) > 0
        tally[1] += short
        tally[2] += untraded
        if short or untraded:
            return False
        tally[3] += 1
        return True

    def check_size(self) -> None:
        size = sum(os.path.getsize(p) for p in (self.db_path, self.db_path + "-wal")
                   if os.path.exists(p))
        if size > self.max_bytes:
            raise DiskLimit(f"database at {size / 1024 ** 3:.2f} GB exceeds the "
                            f"{self.max_bytes / 1024 ** 3:.2f} GB cap")


def store_markets(
    conn: sqlite3.Connection, markets: Iterable[Dict[str, Any]], *, tier: str, source: str,
    gate: Gate,
) -> int:
    now = int(time.time())
    sql = (
        f"INSERT INTO markets ({','.join(MARKET_COLS)}) "
        f"VALUES ({','.join('?' * len(MARKET_COLS))}) "
        "ON CONFLICT(ticker) DO UPDATE SET "
        + ",".join(f"{c}=excluded.{c}" for c in MARKET_COLS if c != "ticker")
    )
    total = seen = 0
    batch = []
    for m in markets:
        if not m.get("ticker"):
            continue
        seen += 1
        if seen % 5000 == 0:
            gate.check_size()
        if not gate.admit(m):
            continue
        batch.append(market_row(m, tier, now))
        if len(batch) >= 500:
            conn.executemany(sql, batch)
            conn.commit()
            total += len(batch)
            batch = []
    if batch:
        conn.executemany(sql, batch)
        conn.commit()
        total += len(batch)
    conn.execute(
        "INSERT INTO pull_log (started_at, source, rows, note) VALUES (?,?,?,?)",
        (now, source, total, f"{tier}; seen {seen}"),
    )
    conn.commit()
    gate.check_size()
    print(f"[pull] {source}: stored {total} of {seen} markets")
    return total


def drop_intraday(conn: sqlite3.Connection, gate: Gate) -> None:
    """Write series_stats and remove every series whose markets are mostly
    short-lived, including its few long-lived outliers and its events."""
    rows = []
    dropped = []
    for series, (seen, short, untraded, stored) in gate.stats.items():
        is_intraday = short * 2 > seen
        rows.append((series, seen, short, untraded, stored, int(is_intraday)))
        if is_intraday:
            dropped.append((series,))
    conn.executemany(
        "INSERT INTO series_stats VALUES (?,?,?,?,?,?) ON CONFLICT(series_ticker) DO UPDATE SET "
        "n_seen=excluded.n_seen, n_short=excluded.n_short, n_untraded=excluded.n_untraded, "
        "n_stored=excluded.n_stored, dropped=excluded.dropped", rows)
    conn.executemany("DELETE FROM markets WHERE series_ticker = ?", dropped)
    conn.executemany("DELETE FROM events WHERE series_ticker = ?", dropped)
    conn.commit()
    print(f"[pull] series seen: {len(rows)}; dropped as intraday: {len(dropped)}")


def store_events(
    conn: sqlite3.Connection, events: Iterable[Dict[str, Any]], *, source: str, gate: Gate
) -> int:
    now = int(time.time())
    sql = (
        "INSERT INTO events (event_ticker, series_ticker, category, title, fetched_at) "
        "VALUES (?,?,?,?,?) ON CONFLICT(event_ticker) DO UPDATE SET "
        "series_ticker=excluded.series_ticker, category=excluded.category, "
        "title=excluded.title, fetched_at=excluded.fetched_at"
    )
    total = 0
    batch = []
    for e in events:
        ticker = e.get("event_ticker")
        if not ticker:
            continue
        batch.append(
            (
                ticker,
                e.get("series_ticker") or str(ticker).split("-")[0],
                e.get("category"),
                e.get("title"),
                now,
            )
        )
        if len(batch) >= 500:
            conn.executemany(sql, batch)
            conn.commit()
            total += len(batch)
            batch = []
            if total % 5000 == 0:
                gate.check_size()
    if batch:
        conn.executemany(sql, batch)
        conn.commit()
        total += len(batch)
    conn.execute(
        "INSERT INTO pull_log (started_at, source, rows, note) VALUES (?,?,?,?)",
        (now, source, total, "events"),
    )
    conn.commit()
    print(f"[pull] {source}: {total} events")
    return total


def backfill_series(conn: sqlite3.Connection) -> None:
    """Prefer the authoritative series_ticker from events over the one derived
    from the ticker prefix."""
    conn.execute(
        "UPDATE markets SET series_ticker = ("
        "  SELECT e.series_ticker FROM events e"
        "  WHERE e.event_ticker = markets.event_ticker AND e.series_ticker IS NOT NULL"
        ") WHERE EXISTS ("
        "  SELECT 1 FROM events e"
        "  WHERE e.event_ticker = markets.event_ticker AND e.series_ticker IS NOT NULL"
        ")"
    )
    conn.commit()


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description="Pull the Kalshi market universe into SQLite")
    ap.add_argument("--db", default="kalshi.db")
    ap.add_argument("--rps", type=float, default=4.0, help="request budget per second")
    ap.add_argument("--base-url", default=None)
    ap.add_argument("--skip-historical", action="store_true",
                    help="skip /historical/markets (much faster, less history)")
    ap.add_argument("--skip-events", action="store_true")
    ap.add_argument("--max-pages", type=int, default=None,
                    help="cap pages per source, for a quick smoke test")
    ap.add_argument("--min-lifespan-days", type=float, default=2.0,
                    help="store only markets open at least this long; series mostly "
                         "below it are dropped whole as intraday")
    ap.add_argument("--max-db-gb", type=float, default=4.0,
                    help="abort the pull if the database grows past this")
    args = ap.parse_args(argv)

    api = Kalshi(base_url=args.base_url, rps=args.rps)
    conn = connect(args.db)
    gate = Gate(args.db, args.min_lifespan_days, args.max_db_gb)
    started = time.time()
    try:
        pull(api, conn, gate, args)
    except DiskLimit as exc:
        print(f"\n[pull] ABORTED: {exc}. Nothing was pruned; inspect {args.db} "
              "before re-running.")
        conn.close()
        return 2

    finish(conn, gate, args, started)
    return 0


def pull(api: Kalshi, conn: sqlite3.Connection, gate: Gate, args: argparse.Namespace) -> None:
    if not args.skip_events:
        for status in EVENT_STATUSES:
            try:
                store_events(
                    conn,
                    api.paginate("/events", "events", {"status": status}, limit=200,
                                 max_pages=args.max_pages, label=f"events[{status}]"),
                    source=f"events:{status}",
                    gate=gate,
                )
            except DiskLimit:
                raise
            except Exception as exc:  # noqa: BLE001 - keep going on a bad status filter
                print(f"[pull] events[{status}] failed: {exc}")

    gate.load_event_series(conn)
    for status in MARKET_STATUSES:
        try:
            store_markets(
                conn,
                api.paginate("/markets", "markets",
                             {"status": status, "mve_filter": "exclude"}, limit=1000,
                             max_pages=args.max_pages, label=f"markets[{status}]"),
                tier="live",
                source=f"markets:{status}",
                gate=gate,
            )
        except DiskLimit:
            raise
        except Exception as exc:  # noqa: BLE001
            print(f"[pull] markets[{status}] failed: {exc}")

    if not args.skip_historical:
        try:
            store_markets(
                conn,
                api.paginate("/historical/markets", "markets",
                             {"mve_filter": "exclude"}, limit=1000,
                             max_pages=args.max_pages, label="historical_markets"),
                tier="historical",
                source="historical_markets",
                gate=gate,
            )
        except DiskLimit:
            raise
        except Exception as exc:  # noqa: BLE001
            print(f"[pull] historical markets failed: {exc}")


def finish(conn: sqlite3.Connection, gate: Gate, args: argparse.Namespace,
           started: float) -> None:
    backfill_series(conn)
    drop_intraday(conn, gate)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("VACUUM")

    n_markets = conn.execute("SELECT COUNT(*) FROM markets").fetchone()[0]
    n_events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    print(
        f"\n[pull] done in {time.time() - started:.0f}s - "
        f"{n_markets} markets, {n_events} events in {args.db}"
    )
    print(f"[pull] database size {os.path.getsize(args.db) / 1024 ** 2:.0f} MB")
    print("[pull] next: python3 census.py --db " + args.db)
    conn.close()


if __name__ == "__main__":
    sys.exit(main())
