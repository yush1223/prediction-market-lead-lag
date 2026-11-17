"""Minute-level timing profile for E01, implementing test_spec_v4_minute.md.

1-minute candles are sparse (emitted only on change), so each market's last
quote is carried forward to every 5-minute bar end until its next candle or
its close. The forward-filled bars go into a temporary table and the v2 index
code runs on them unchanged.

    python3 minute_test.py --db kalshi.db --out report/
"""

from __future__ import annotations

import argparse
import csv
import os
import sqlite3
import sys
from collections import defaultdict
from typing import Optional

import numpy as np

from lead_lag_test import SIGN_MAP_SHA, fit, series_changes, sha
from pilot_test import _ts

SPEC_SHA = "2c34fac33d6d3ea1367cc675511ab4cf50325b1a674a6a9fa9eb5b059786b4d5"
LEAD, LAG, SIGN = "KXWTIW", "KXAAAGASW", 1
BAR = 300
HORIZONS = [1, 3, 6, 12, 24, 48, 72]
N_NULL, SEED, PRIOR_TALLY = 500, 20261110, 45


def build_bars(conn: sqlite3.Connection, series: list) -> int:
    conn.execute("CREATE TEMP TABLE bars_5m (ticker TEXT, end_ts INTEGER, yes_bid_close REAL, "
                 "yes_ask_close REAL)")
    close_of = {t: _ts(c) for t, c in conn.execute(
        f"SELECT ticker, close_time FROM markets WHERE series_ticker IN ({','.join('?' * len(series))})",
        series)}
    by_ticker = defaultdict(list)
    for t, e, b, a in conn.execute(
        "SELECT c.ticker, c.end_ts, c.yes_bid_close, c.yes_ask_close FROM candles_1m c "
        "JOIN markets m ON m.ticker = c.ticker WHERE m.series_ticker IN "
        f"({','.join('?' * len(series))}) ORDER BY c.ticker, c.end_ts", series
    ):
        by_ticker[t].append((e, b, a))
    n = 0
    for t, cs in by_ticker.items():
        end = close_of.get(t) or cs[-1][0]
        rows, i = [], 0
        bar = -(-cs[0][0] // BAR) * BAR  # first bar end at or after the first candle
        while bar <= end:
            while i + 1 < len(cs) and cs[i + 1][0] <= bar:
                i += 1
            rows.append((t, bar, cs[i][1], cs[i][2]))
            bar += BAR
        conn.executemany("INSERT INTO bars_5m VALUES (?,?,?,?)", rows)
        n += len(rows)
    return n


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description="Minute-level timing profile for E01 (v4)")
    ap.add_argument("--db", default="kalshi.db")
    ap.add_argument("--out", default="report")
    args = ap.parse_args(argv)
    for path, want in (("test_spec_v4_minute.md", SPEC_SHA), ("sign_map.csv", SIGN_MAP_SHA)):
        got = sha(path)
        print(f"{path} sha256 {got[:12]}... {'matches' if got == want else 'DOES NOT MATCH'}")
        if got != want:
            return 1

    conn = sqlite3.connect(args.db)
    n = build_bars(conn, [LEAD, LAG])
    print(f"forward-filled 5-minute bars: {n:,}")
    L = series_changes(conn, LEAD, table="bars_5m", period=BAR)
    G = series_changes(conn, LAG, table="bars_5m", period=BAR)
    for s, d in ((LEAD, L), (LAG, G)):
        ch = [v["change"] for v in d.values() if v["change"] is not None]
        print(f"  {s:<10} {len(ch):>7} bars with a change, {sum(abs(c) > 1e-12 for c in ch) / max(len(ch), 1):.0%} non-zero")

    rng = np.random.default_rng(SEED)
    print(f"\nTIMING PROFILE  E01 {LEAD} -> {LAG}  (descriptive; same period as v3 holdout)")
    print(f"  {'horizon':>9}{'bars':>9}{'events':>8}{'b':>12}{'t':>8}{'cum. predicted move':>22}")
    out, tmax_null = [], np.full(N_NULL, -np.inf)
    for k in HORIZONS:
        rows = []
        for h in sorted(G):
            cur, ld = G[h], L.get(h)
            if cur["change"] is None or not ld or ld["change"] is None:
                continue
            fut = [G.get(h + j) for j in range(1, k + 1)]
            if any(f is None or f["change"] is None for f in fut):
                continue
            rows.append((h, sum(f["change"] for f in fut), ld["change"], cur["change"], fut[0]["event"]))
        if len(rows) < 100:
            print(f"  {k * 5:>6} min  too few bars ({len(rows)})")
            continue
        b, t, g = fit(rows)
        lead = np.array([r[2] for r in rows])
        move = abs(b) * float(np.std(lead)) * 100
        print(f"  {k * 5:>6} min{len(rows):>9}{g:>8}{b:>12.4f}{t * SIGN:>8.2f}{move:>19.3f}c")
        out.append({"horizon_min": k * 5, "bars": len(rows), "events": g, "b": b, "t": t * SIGN,
                    "pred_move_c": move})
        for r in range(N_NULL):
            _, tn, _ = fit(rows, rng.permutation(lead))
            tmax_null[r] = max(tmax_null[r], tn * SIGN)
    print(f"\n  reference null: 95th pct of max t across horizons = {np.percentile(tmax_null, 95):.2f} "
          f"(median {np.median(tmax_null):.2f})")
    print(f"\nVariants counted: {len(out)} (exploratory) + {PRIOR_TALLY} earlier = {len(out) + PRIOR_TALLY}. "
          "No fees, no P&L.")
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "test_v4_minute_profile.csv"), "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(out[0].keys()) if out else ["horizon_min"])
        w.writeheader()
        w.writerows(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
