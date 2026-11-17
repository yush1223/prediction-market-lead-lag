"""Stage 1: which pairs of Kalshi series are testable at all?

Pure database work over the universe pulled by pull_universe.py. Pulls nothing
from the network and touches no candlesticks — the point is to find out whether
a lead-lag study is even possible before spending anything on price data.

A pair is testable only if both series were alive at the same time, for long
enough, with enough independent events and enough volume to have meaningful
prices. The first version of this project died because its flagship pair
(GLP-1 prescriptions and fast-food menu prices) had ZERO overlapping days — one
series retired 15 months before the other launched. Kalshi series are
ephemeral, so overlap is the binding constraint, not an afterthought.

Also flags possible same-release pairs. Two series that settle off the same data
print at the same moment (CPI and Core CPI, say) move together
contemporaneously, which is not a lead-lag effect. The flag is settlement-day
coincidence, which cannot tell one release apart from two releases on one
schedule (weekly gold and copper both settle at Friday's close), so flagged
pairs stay in the list. Whether a pair is truly same-release is decided by hand
from rules_primary in the sign map.

    python3 census.py --db kalshi.db
    python3 census.py --db kalshi.db --min-events 20 --min-overlap 60 --out report/
"""

from __future__ import annotations

import argparse
import csv
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

DAY = 86400


def parse_day(value: Any) -> Optional[int]:
    """Days since the Unix epoch, from an RFC3339 string or an epoch number."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return int(value) // DAY
    text = str(value).replace("Z", "+00:00")
    try:
        return int(datetime.fromisoformat(text).timestamp()) // DAY
    except ValueError:
        return None


class Series:
    __slots__ = ("ticker", "category", "markets", "events", "volume",
                 "first_day", "last_day", "mask", "event_days")

    def __init__(self, ticker: str) -> None:
        self.ticker = ticker
        self.category = ""
        self.markets = 0
        self.events: set = set()
        self.volume = 0.0
        self.first_day: Optional[int] = None
        self.last_day: Optional[int] = None
        self.mask = 0            # bitmap of days with at least one market open
        self.event_days: set = set()  # distinct settlement/close days

    @property
    def active_days(self) -> int:
        return self.mask.bit_count()

    @property
    def lifespan(self) -> int:
        if self.first_day is None or self.last_day is None:
            return 0
        return self.last_day - self.first_day + 1


def load_series(conn: sqlite3.Connection, min_market_volume: float) -> Dict[str, Series]:
    """Build per-series day bitmaps from the markets table.

    Markets below `min_market_volume` are ignored outright: an untraded market
    has no usable price, so counting its calendar days as 'active' would
    overstate every overlap downstream.
    """
    rows = conn.execute(
        "SELECT m.series_ticker, m.event_ticker, m.open_time, m.close_time, "
        "       m.settlement_ts, m.volume, COALESCE(e.category, '') "
        "FROM markets m LEFT JOIN events e ON e.event_ticker = m.event_ticker "
        "WHERE m.series_ticker IS NOT NULL AND m.series_ticker != ''"
    ).fetchall()
    if not rows:
        return {}

    # First pass: find the calendar origin so day indices stay small.
    days: List[int] = []
    for _, _, open_time, close_time, settlement, volume, _ in rows:
        for value in (open_time, close_time, settlement):
            d = parse_day(value)
            if d is not None:
                days.append(d)
    if not days:
        return {}
    origin = min(days)

    out: Dict[str, Series] = {}
    for series_ticker, event_ticker, open_time, close_time, settlement, volume, category in rows:
        vol = float(volume or 0.0)
        s = out.get(series_ticker)
        if s is None:
            s = out[series_ticker] = Series(series_ticker)
        if category and not s.category:
            s.category = category
        s.markets += 1
        s.volume += vol
        if vol < min_market_volume:
            continue

        start = parse_day(open_time)
        end = parse_day(close_time)
        if start is None or end is None or end < start:
            continue

        if event_ticker:
            s.events.add(event_ticker)
        settle_day = parse_day(settlement) or end
        s.event_days.add(settle_day - origin)

        lo, hi = start - origin, end - origin
        s.first_day = lo if s.first_day is None else min(s.first_day, lo)
        s.last_day = hi if s.last_day is None else max(s.last_day, hi)
        # Set bits [lo, hi] in one shot.
        s.mask |= ((1 << (hi - lo + 1)) - 1) << lo

    return out


def filter_series(
    series: Dict[str, Series], min_events: int, min_volume: float, min_days: int
) -> List[Series]:
    kept = [
        s for s in series.values()
        if len(s.events) >= min_events and s.volume >= min_volume and s.active_days >= min_days
    ]
    kept.sort(key=lambda s: -s.volume)
    return kept


def events_within(s: Series, mask: int) -> int:
    """How many of this series' settlements fall inside a day mask."""
    return sum(1 for d in s.event_days if d >= 0 and (mask >> d) & 1)


def census(
    kept: List[Series], min_overlap: int, min_pair_events: int, same_release_cut: float
) -> List[dict]:
    candidates: List[dict] = []

    for i, a in enumerate(kept):
        for b in kept[i + 1:]:
            overlap_mask = a.mask & b.mask
            overlap = overlap_mask.bit_count()
            if overlap < min_overlap:
                continue

            ev_a = events_within(a, overlap_mask)
            ev_b = events_within(b, overlap_mask)
            if min(ev_a, ev_b) < min_pair_events:
                continue

            shared = len(a.event_days & b.event_days)
            denom = min(len(a.event_days), len(b.event_days)) or 1
            frac = shared / denom

            row = {
                "series_a": a.ticker,
                "category_a": a.category,
                "series_b": b.ticker,
                "category_b": b.category,
                "overlap_days": overlap,
                "events_a": ev_a,
                "events_b": ev_b,
                "min_events": min(ev_a, ev_b),
                "volume_a": round(a.volume),
                "volume_b": round(b.volume),
                "same_release_frac": round(frac, 3),
                "same_release_flag": int(frac >= same_release_cut),
            }
            candidates.append(row)

    key = lambda r: (-r["min_events"], -r["overlap_days"], -min(r["volume_a"], r["volume_b"]))
    candidates.sort(key=key)
    return candidates


def write_csv(path: str, rows: Sequence[dict]) -> None:
    if not rows:
        return
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"  [csv] {path}  ({len(rows):,} rows)")


def show(rows: Sequence[dict], limit: int, title: str) -> None:
    print(f"\n{title}")
    if not rows:
        print("  (none)")
        return
    print(f"  {'series A':<22}{'series B':<22}{'days':>6}{'ev A':>6}{'ev B':>6}"
          f"{'vol A':>15}{'vol B':>15}  same-day")
    for r in rows[:limit]:
        print(f"  {r['series_a'][:22]:<22}{r['series_b'][:22]:<22}"
              f"{r['overlap_days']:>6}{r['events_a']:>6}{r['events_b']:>6}"
              f"{r['volume_a']:>15,}{r['volume_b']:>15,}"
              f"  {r['same_release_frac']:.2f}{' FLAG' if r['same_release_flag'] else ''}")
    if len(rows) > limit:
        print(f"  ... and {len(rows) - limit:,} more (see the CSV)")


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description="Stage 1 pair census for Kalshi lead-lag research")
    ap.add_argument("--db", default="kalshi.db")
    ap.add_argument("--out", default="report", help="CSV output directory ('' to skip)")
    ap.add_argument("--top", type=int, default=30)
    # Series-level filters, applied before pairing. Keep these tight: pairing is
    # quadratic, and an illiquid series contributes noise rather than signal.
    ap.add_argument("--min-events", type=int, default=20,
                    help="independent events a series needs to qualify")
    ap.add_argument("--min-volume", type=float, default=50_000,
                    help="lifetime contracts traded across the series")
    ap.add_argument("--min-days", type=int, default=120,
                    help="days on which the series had a traded market open")
    ap.add_argument("--min-market-volume", type=float, default=100,
                    help="ignore markets below this volume when building day coverage")
    # Pair-level filters.
    ap.add_argument("--min-overlap", type=int, default=60,
                    help="overlapping active days a pair needs")
    ap.add_argument("--min-pair-events", type=int, default=10,
                    help="events each side needs inside the overlap window")
    ap.add_argument("--same-release-cut", type=float, default=0.5,
                    help="settlement-day coincidence above which a pair is flagged as "
                         "possibly same-release (flagged, never excluded)")
    args = ap.parse_args(argv)

    if not os.path.exists(args.db):
        print(f"No database at {args.db}. Run: python3 pull_universe.py --db {args.db}")
        return 1

    conn = sqlite3.connect(args.db)
    print(f"Kalshi pair census - {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC")
    print(f"database: {args.db}")

    all_series = load_series(conn, args.min_market_volume)
    conn.close()
    if not all_series:
        print("\nNo series found. Has pull_universe.py run against this database?")
        return 1

    total_markets = sum(s.markets for s in all_series.values())
    print(f"\n  series in universe: {len(all_series):,}   markets: {total_markets:,}")

    kept = filter_series(all_series, args.min_events, args.min_volume, args.min_days)
    print(f"  series passing filters: {len(kept):,}")
    print(f"    (>= {args.min_events} events, >= {args.min_volume:,.0f} volume, "
          f">= {args.min_days} active days)")

    if not kept:
        print("\n  Nothing passes. Loosen the filters, but note what that means: no")
        print("  series on the exchange has both sustained history and real volume,")
        print("  which is itself the answer to whether this study is possible.")
        return 0

    if len(kept) > 1500:
        print(f"\n  WARNING: {len(kept):,} series means ~{len(kept) ** 2 // 2:,} pairs. "
              "Tighten the filters if this is slow.")

    print(f"\n  pairing {len(kept):,} series ({len(kept) * (len(kept) - 1) // 2:,} combinations)")
    candidates = census(kept, args.min_overlap, args.min_pair_events, args.same_release_cut)
    flagged = [r for r in candidates if r["same_release_flag"]]

    print(f"\n  testable pairs:        {len(candidates):,}")
    print(f"  flagged same-release:  {len(flagged):,}  (settle on the same days >= "
          f"{args.same_release_cut:.0%} of the time; kept, check rules_primary)")

    show(candidates, args.top, "TESTABLE PAIRS  (sorted by events on the weaker side)")
    show(flagged, 10, "FLAGGED SAME-RELEASE  (kept in the list above; decide in the sign map)")

    by_cat: Dict[Tuple[str, str], int] = defaultdict(int)
    for r in candidates:
        key = tuple(sorted((r["category_a"] or "?", r["category_b"] or "?")))
        by_cat[key] += 1
    if by_cat:
        print("\nTESTABLE PAIRS BY CATEGORY")
        for (ca, cb), n in sorted(by_cat.items(), key=lambda kv: -kv[1])[:15]:
            print(f"  {ca[:24]:<24} x {cb[:24]:<24} {n:>6,}")

    print("\n" + "=" * 78)
    print("READ THIS BEFORE GOING FURTHER")
    print("=" * 78)
    n = len(candidates)
    if n == 0:
        print("  No testable pairs. Either the filters are too strict, or Kalshi's")
        print("  series do not co-exist long enough to study lead-lag. Loosen once,")
        print("  and if it still comes back empty, that is the finding.")
    elif n < 30:
        print(f"  Only {n} testable pairs. That is a thin research programme: a grid")
        print("  search over so few pairs will be dominated by whichever one got")
        print("  lucky. Proceed only if several pairs have a real economic story.")
    else:
        print(f"  {n:,} testable pairs is enough to search - and enough to fool you.")
        print(f"  A sweep over {n:,} pairs x lag windows x thresholds x directions is")
        print("  tens of thousands of variants, so pure noise will hand you dozens of")
        print("  beautiful equity curves. Before any backtest:")
        print("    1. assign each pair's expected direction from the economics, write")
        print("       it to disk and hash it - never from what made money")
        print("    2. split time: search on the early period, hold out the late one")
        print("    3. re-run the whole search on date-shuffled data to learn what the")
        print("       best noise result looks like on your own data")
        print("    4. only variants beating that null go to the holdout, exactly once")
        print("  Report the number of variants tested beside any result, always.")

    if args.out:
        write_csv(os.path.join(args.out, "testable_pairs.csv"), candidates)
        write_csv(os.path.join(args.out, "series_passing.csv"), [
            {
                "series": s.ticker, "category": s.category, "markets": s.markets,
                "events": len(s.events), "volume": round(s.volume),
                "active_days": s.active_days, "lifespan_days": s.lifespan,
            }
            for s in kept
        ])

    return 0


if __name__ == "__main__":
    sys.exit(main())
