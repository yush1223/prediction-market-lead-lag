"""Offline smoke test: serves synthetic Kalshi-shaped responses on localhost and
runs the universe pull and the pair census against them.

Verifies the plumbing — cursor pagination, fixed-point string parsing, the
parlay filter, the live/historical tier split, day-bitmap overlap, and
same-release detection — without touching the real exchange. It proves nothing
about market structure; it only proves the code runs.

The parlay check is the important one. Kalshi's market lists are flooded with
auto-generated multivariate parlay markets, and without mve_filter=exclude the
paging never finishes. The fixture returns a parlay flood unless the filter is
passed, so dropping it fails this test loudly instead of hanging in production.

    python3 smoke_test.py
"""

from __future__ import annotations

import json
import os
import random
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PREFIX = "/trade-api/v2"
DAY = 86400

SERIES = [
    ("KXCPI", "Economics"),
    ("KXCPICORE", "Economics"),      # built to settle on CPI's days: same-release
    ("KXEIACRUDE", "Economics"),
    ("KXGAS", "Economics"),
    ("KXFED", "Economics"),
    ("KXMCDFT", "Companies"),
]
PARLAY_COUNT = 400  # stands in for Kalshi's combinatorial flood
INTRADAY = "KXBTCH"  # hourly markets: the pull must drop this series whole


def iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_world(seed: int = 11):
    rng = random.Random(seed)
    now = int(time.time())
    events, markets = [], []
    cpi_closes: dict = {}

    for series, category in SERIES:
        for month_back in range(14):
            if series == "KXCPICORE" and month_back in cpi_closes:
                close = cpi_closes[month_back]          # deliberate twin
            else:
                close = now - month_back * 30 * DAY - rng.randint(0, 9) * DAY
            if series == "KXCPI":
                cpi_closes[month_back] = close

            open_ts = close - rng.randint(90, 160) * DAY
            event_ticker = f"{series}-{month_back:02d}"
            events.append({
                "event_ticker": event_ticker,
                "series_ticker": series,
                "category": category,
                "title": f"{series} event {month_back}",
            })
            for strike in range(rng.randint(3, 7)):
                settled = month_back > 0
                bid = round(rng.uniform(0.05, 0.9), 2)
                spread = rng.choice([0.01, 0.01, 0.02, 0.03])
                markets.append({
                    "ticker": f"{event_ticker}-T{strike}",
                    "event_ticker": event_ticker,
                    "market_type": "binary",
                    "status": "finalized" if settled else "active",
                    "yes_sub_title": f"above {strike}",
                    "open_time": iso(open_ts),
                    "close_time": iso(close),
                    "settlement_ts": iso(close) if settled else None,
                    "yes_bid_dollars": f"{bid:.4f}",
                    "yes_ask_dollars": f"{min(bid + spread, 0.99):.4f}",
                    "yes_bid_size_fp": f"{rng.randint(10, 900)}.00",
                    "yes_ask_size_fp": f"{rng.randint(10, 900)}.00",
                    "no_bid_dollars": f"{max(1 - bid - spread, 0.01):.4f}",
                    "no_ask_dollars": f"{max(1 - bid, 0.02):.4f}",
                    "last_price_dollars": f"{bid:.4f}",
                    "volume_fp": f"{rng.choice([0, 300, 2500, 40000]):.2f}",
                    "volume_24h_fp": f"{rng.choice([0, 20, 150, 1200]):.2f}",
                    "open_interest_fp": f"{rng.randint(0, 9000)}.00",
                    "result": rng.choice(["yes", "no"]) if settled else "",
                    "strike_type": "greater",
                    "floor_strike": float(strike),
                    "rules_primary": f"Settles YES if the measure exceeds {strike}.",
                    "rules_secondary": "",
                    "mve_collection_ticker": "",
                    "_settled": settled,
                })

    # An intraday series: 60 one-hour markets plus one long-lived outlier. The
    # outlier passes the per-market lifespan rule, so only the series-level
    # prune removes it.
    for i in range(61):
        close = now - i * 3600
        lifetime = 30 * DAY if i == 60 else 3600
        event_ticker = f"{INTRADAY}-{i:03d}"
        events.append({"event_ticker": event_ticker, "series_ticker": INTRADAY,
                       "category": "Crypto", "title": f"BTC hour {i}"})
        markets.append({
            "ticker": f"{event_ticker}-T0", "event_ticker": event_ticker,
            "market_type": "binary", "status": "active",
            "open_time": iso(close - lifetime), "close_time": iso(close + DAY),
            "settlement_ts": None, "yes_bid_dollars": "0.4000",
            "yes_ask_dollars": "0.4200", "volume_fp": "5000.00",
            "rules_primary": "Hourly BTC.", "rules_secondary": "Boilerplate.",
            "mve_collection_ticker": "", "_settled": False,
        })

    parlays = [{
        "ticker": f"KXPARLAY-{i:05d}",
        "event_ticker": f"KXPARLAY-{i // 10:04d}",
        "market_type": "binary",
        "status": "active",
        "open_time": iso(now - 30 * DAY),
        "close_time": iso(now + 30 * DAY),
        "settlement_ts": None,
        "yes_bid_dollars": "0.1000",
        "yes_ask_dollars": "0.1500",
        "volume_fp": "5.00",
        "mve_collection_ticker": f"KXPARLAY-COLL-{i // 50}",
        "rules_primary": "Parlay leg.",
        "_settled": False,
    } for i in range(PARLAY_COUNT)]

    return events, markets, parlays


def _close_ts(market: dict) -> float:
    return datetime.fromisoformat(market["close_time"].replace("Z", "+00:00")).timestamp()


EVENTS, MARKETS, PARLAYS = build_world()


def expected_stored(market: dict) -> bool:
    """What the pull should keep: traded, lived >= 2 days, not intraday."""
    life = _close_ts(market) - datetime.fromisoformat(
        market["open_time"].replace("Z", "+00:00")).timestamp()
    traded = float(market.get("volume_fp") or 0) > 0
    return traded and life >= 2 * DAY and not market["ticker"].startswith(INTRADAY)


N_EXPECTED = sum(expected_stored(m) for m in MARKETS)
N_EXPECTED_EVENTS = sum(not e["series_ticker"] == INTRADAY for e in EVENTS)

# Mirror Kalshi's live/historical split: anything settled before the cutoff is
# served only by /historical/markets.
CUTOFF = time.time() - 200 * DAY
LIVE = [m for m in MARKETS if not m["_settled"] or _close_ts(m) > CUTOFF]
HISTORICAL = [m for m in MARKETS if m["_settled"] and _close_ts(m) <= CUTOFF]


def strip(market: dict) -> dict:
    return {k: v for k, v in market.items() if not k.startswith("_")}


def page(items: list, qs: dict, key: str) -> dict:
    limit = int(qs.get("limit", ["100"])[0])
    start = int(qs.get("cursor", ["0"])[0] or 0)
    chunk = items[start:start + limit]
    nxt = start + limit
    return {key: chunk, "cursor": str(nxt) if nxt < len(items) else ""}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_a) -> None:
        pass

    def _send(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        path = url.path[len(PREFIX):] if url.path.startswith(PREFIX) else url.path
        qs = parse_qs(url.query)
        excluding = (qs.get("mve_filter") or [""])[0] == "exclude"

        if path == "/markets":
            status = (qs.get("status") or [""])[0]
            if status == "open":
                pool = [m for m in LIVE if not m["_settled"]]
            elif status == "settled":
                pool = [m for m in LIVE if m["_settled"]]
            elif status in ("closed", "unopened", "paused"):
                pool = []
            else:
                pool = list(LIVE)
            if not excluding:
                pool = pool + PARLAYS  # the flood, as the real API returns it
            return self._send(page([strip(m) for m in pool], qs, "markets"))

        if path == "/historical/markets":
            pool = list(HISTORICAL)
            if not excluding:
                pool = pool + PARLAYS
            return self._send(page([strip(m) for m in pool], qs, "markets"))

        if path == "/events":
            return self._send(page(EVENTS, qs, "events"))

        return self._send({"error": f"no fixture for {path}"}, 404)


def run() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    os.environ["KALSHI_BASE_URL"] = f"http://127.0.0.1:{port}{PREFIX}"
    os.environ["KALSHI_RPS"] = "1000"
    print(f"fixture exchange on 127.0.0.1:{port}")
    print(f"  {len(MARKETS)} real markets ({len(LIVE)} live, {len(HISTORICAL)} historical), "
          f"{len(EVENTS)} events, {len(PARLAYS)} parlays to be filtered out\n")

    workdir = tempfile.mkdtemp(prefix="kalshi-smoke-")
    db = os.path.join(workdir, "kalshi.db")
    failures = []

    try:
        import census
        import pull_universe

        print("--- pull_universe ---")
        pull_universe.main(["--db", db, "--rps", "1000"])

        conn = sqlite3.connect(db)
        n_markets = conn.execute("SELECT COUNT(*) FROM markets").fetchone()[0]
        n_events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        n_parlay = conn.execute(
            "SELECT COUNT(*) FROM markets WHERE ticker LIKE 'KXPARLAY%'").fetchone()[0]
        tiers = dict(conn.execute("SELECT tier, COUNT(*) FROM markets GROUP BY 1"))
        bid = conn.execute(
            "SELECT yes_bid FROM markets WHERE yes_bid IS NOT NULL LIMIT 1").fetchone()[0]
        vol = conn.execute(
            "SELECT volume FROM markets WHERE volume > 0 LIMIT 1").fetchone()[0]
        n_series = conn.execute(
            "SELECT COUNT(DISTINCT series_ticker) FROM markets").fetchone()[0]
        cols = {r[1] for r in conn.execute("PRAGMA table_info(markets)")}
        n_untraded = conn.execute(
            "SELECT COUNT(*) FROM markets WHERE NOT volume > 0").fetchone()[0]
        n_intraday = conn.execute(
            "SELECT COUNT(*) FROM markets WHERE series_ticker = ?", (INTRADAY,)).fetchone()[0]
        stats = {r[0]: r[1:] for r in conn.execute(
            "SELECT series_ticker, n_seen, n_short, n_untraded, n_stored, dropped "
            "FROM series_stats")}
        conn.close()

        if n_parlay:
            failures.append(f"{n_parlay} parlay markets stored - mve_filter=exclude "
                            "is not being passed")
        if n_markets != N_EXPECTED:
            failures.append(f"stored {n_markets} markets, expected {N_EXPECTED}")
        if n_events != N_EXPECTED_EVENTS:
            failures.append(f"stored {n_events} events, expected {N_EXPECTED_EVENTS}")
        if n_untraded:
            failures.append(f"{n_untraded} volume-0 markets stored")
        if n_intraday:
            failures.append(f"{n_intraday} markets of intraday series {INTRADAY} survived")
        if (stats.get(INTRADAY) or (0,) * 5)[4] != 1 or stats.get(INTRADAY, (0,))[0] != 61:
            failures.append(f"series_stats for {INTRADAY} is {stats.get(INTRADAY)}, "
                            "expected 61 seen and dropped=1")
        if any(stats.get(name, (0,) * 5)[4] for name, _ in SERIES):
            failures.append("a long-lived series was dropped as intraday")
        if "rules_secondary" in cols:
            failures.append("markets table still stores rules_secondary")
        if set(tiers) != {"live", "historical"}:
            failures.append(f"tiers {tiers}, expected exactly live and historical")
        if not (0 < (bid or 0) < 1):
            failures.append(f"yes_bid parsed as {bid}, expected dollars in (0,1)")
        if not vol or vol < 1:
            failures.append(f"volume parsed as {vol}, expected contracts")
        if "raw" in cols:
            failures.append("markets table still has a raw JSON column")
        print(f"\nparsed: {n_markets} markets, {n_events} events, {n_series} series, "
              f"tiers={tiers}, parlays stored={n_parlay}, bid={bid}, volume={vol}")

        print("\n--- size guard ---")
        tiny = os.path.join(workdir, "tiny.db")
        rc = pull_universe.main(["--db", tiny, "--rps", "1000", "--max-db-gb", "1e-6"])
        if rc != 2:
            failures.append(f"pull with a 1 KB cap returned {rc}, expected abort (2)")

        print("\n--- census ---")
        out = os.path.join(workdir, "report")
        # Loose thresholds: the fixture is small by design.
        census.main(["--db", db, "--out", out, "--min-events", "3",
                     "--min-volume", "1000", "--min-days", "30",
                     "--min-overlap", "30", "--min-pair-events", "2"])

        pairs_csv = os.path.join(out, "testable_pairs.csv")
        if not os.path.exists(pairs_csv):
            failures.append("census produced no testable_pairs.csv")
        else:
            import csv as _csv
            with open(pairs_csv) as fh:
                pairs = list(_csv.DictReader(fh))
            if not pairs:
                failures.append("census found zero testable pairs in the fixture")
            if any(r["series_a"] == r["series_b"] for r in pairs):
                failures.append("census paired a series with itself")
            seen = {tuple(sorted((r["series_a"], r["series_b"]))) for r in pairs}
            if len(seen) != len(pairs):
                failures.append("census emitted duplicate or mirrored pairs")
            if any(int(r["overlap_days"]) < 30 for r in pairs):
                failures.append("census returned a pair below the overlap threshold")
            # The CPI twin settles on identical days by construction: it must
            # be flagged, and it must still be in the list (flagged, not excluded).
            twin = [r for r in pairs
                    if tuple(sorted((r["series_a"], r["series_b"]))) == ("KXCPI", "KXCPICORE")]
            if not twin:
                failures.append("the CPI/CPICORE twin was dropped; flagged pairs must stay listed")
            elif twin[0]["same_release_flag"] != "1":
                failures.append("same-release flag missed the CPI/CPICORE twin")
            n_flag = sum(r["same_release_flag"] == "1" for r in pairs)
            print(f"\ncensus: {len(pairs)} testable pairs, {n_flag} flagged same-release, "
                  f"CPI twin flagged={bool(twin) and twin[0]['same_release_flag'] == '1'}")
        if os.path.exists(os.path.join(out, "same_release_pairs.csv")):
            failures.append("census still writes a separate same_release_pairs.csv")

    finally:
        server.shutdown()
        shutil.rmtree(workdir, ignore_errors=True)

    print("\n" + "=" * 70)
    if failures:
        print("FAILED")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASSED - parlay filter, pagination, fixed-point parsing, tier split,")
    print("         storage gate, intraday drop, size guard, overlap bitmaps and")
    print("         same-release flagging all working.")
    return 0


if __name__ == "__main__":
    sys.exit(run())
