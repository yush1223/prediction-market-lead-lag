"""Hourly lead-lag test v3, implementing test_spec_v3_hourly.md exactly.

    python3 hourly_test.py --db kalshi.db --out report/
"""

from __future__ import annotations

import argparse
import csv
import os
import sqlite3
import sys
from typing import Optional

import numpy as np

from lead_lag_test import MIN_CLUSTERS, SIGN_MAP_SHA, fit, series_changes, sha

SPEC_SHA = "16f4d59b4f84e6c853ed0624dfd6fd90f7a983eeaa9fa14d23a6a077f00ec09c"
PAIRS = ["E01", "E06", "E23", "E40", "E38"]
HORIZONS = [1, 6]
SPLIT, N_NULL, SEED, MIN_HOURS = 0.65, 500, 20261110, 100
PRIOR_TALLY = 37


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description="Hourly lead-lag test v3")
    ap.add_argument("--db", default="kalshi.db")
    ap.add_argument("--out", default="report")
    args = ap.parse_args(argv)
    for path, want in (("test_spec_v3_hourly.md", SPEC_SHA), ("sign_map.csv", SIGN_MAP_SHA)):
        got = sha(path)
        print(f"{path} sha256 {got[:12]}... {'matches' if got == want else 'DOES NOT MATCH'}")
        if got != want:
            return 1

    with open("sign_map.csv") as fh:
        pairs = {r["pair_id"]: r for r in csv.DictReader(fh) if r["pair_id"] in PAIRS}
    conn = sqlite3.connect(args.db)
    series = sorted({p["leader"] for p in pairs.values()} | {p["laggard"] for p in pairs.values()})
    idx = {s: series_changes(conn, s, table="candles_1h", period=3600) for s in series}
    print("\nCONTINUITY (hourly index)")
    for s in series:
        ch = [v["change"] for v in idx[s].values() if v["change"] is not None]
        print(f"  {s:<14} {len(ch):>6} hours with a change, "
              f"{sum(abs(c) > 1e-12 for c in ch) / max(len(ch), 1):>4.0%} non-zero")

    tests = {}
    for pid in PAIRS:
        p = pairs[pid]
        L, G = idx[p["leader"]], idx[p["laggard"]]
        for k in HORIZONS:
            rows = []
            for h in sorted(G):
                cur, ld = G[h], L.get(h)
                fut = [G.get(h + j) for j in range(1, k + 1)]
                if cur["change"] is None or not ld or ld["change"] is None:
                    continue
                if any(f is None or f["change"] is None for f in fut):
                    continue
                rows.append((h, sum(f["change"] for f in fut), ld["change"], cur["change"],
                             fut[0]["event"], fut[0].get("spread")))
            cut = int(len(rows) * SPLIT)
            tests[(pid, k)] = (p, rows[:cut], rows[cut:])

    testable = {key: v for key, v in tests.items()
                if len(v[1]) >= MIN_HOURS and len({r[4] for r in v[1]}) >= MIN_CLUSTERS}
    results = {}
    for key, (p, search, hold) in tests.items():
        sign = 1 if p["sign"] == "+" else -1
        r = {"pair_id": key[0], "horizon_h": key[1], "leader": p["leader"], "laggard": p["laggard"],
             "search_hours": len(search), "holdout_hours": len(hold),
             "search_clusters": len({x[4] for x in search})}
        if key in testable:
            b, t, _ = fit(search)
            r.update(b=b, search_s=t * sign)
            sp = [x[5] for x in search if x[5] is not None]
            if sp:
                r["pred_move_c"] = abs(b) * float(np.std([x[2] for x in search])) * 100
                r["typical_spread_c"] = float(np.median(sp)) * 100
        results[key] = r

    rng = np.random.default_rng(SEED)
    null_max = []
    for _ in range(N_NULL):
        best = -np.inf
        for key, (p, search, _h) in testable.items():
            sign = 1 if p["sign"] == "+" else -1
            _, t, _ = fit(search, rng.permutation(np.array([x[2] for x in search])))
            best = max(best, t * sign)
        null_max.append(best)
    threshold = float(np.percentile(null_max, 95)) if null_max else float("nan")

    survivors = [key for key in testable if results[key]["search_s"] > threshold]
    for key in survivors:
        p, _s, hold = tests[key]
        sign = 1 if p["sign"] == "+" else -1
        if len(hold) >= MIN_HOURS // 2 and len({x[4] for x in hold}) >= MIN_CLUSTERS // 2:
            b, t, _ = fit(hold)
            results[key].update(holdout_b=b, holdout_s=t * sign, holdout_pass=t * sign >= 2)
        else:
            results[key].update(holdout_s=None, holdout_pass=False)

    print(f"\nSEARCH PERIOD (early {SPLIT:.0%} of joint hours)")
    print(f"  {'pair':<5}{'h':>3}  {'leader -> laggard':<30}{'hours':>7}{'events':>7}{'s':>8}"
          f"{'pred move':>11}{'spread':>8}")
    for key in sorted(results, key=lambda k: -results[k].get("search_s", -99)):
        r = results[key]
        s = f"{r['search_s']:>8.2f}" if "search_s" in r else f"{'untest.':>8}"
        mv = f"{r['pred_move_c']:>9.3f}c" if "pred_move_c" in r else f"{'':>10}"
        spd = f"{r['typical_spread_c']:>6.1f}c" if "typical_spread_c" in r else ""
        print(f"  {r['pair_id']:<5}{r['horizon_h']:>3}  {(r['leader'] + ' -> ' + r['laggard'])[:29]:<30}"
              f"{r['search_hours']:>7}{r['search_clusters']:>7}{s} {mv}{spd:>8}")
    ss = [r["search_s"] for r in results.values() if "search_s" in r]
    print(f"\n  testable tests: {len(ss)} of {len(results)}; naive s >= 2: {sum(x >= 2 for x in ss)}")
    if null_max:
        print(f"\nSHUFFLED NULL ({N_NULL} shuffles): best-noise s median {np.median(null_max):.2f}, "
              f"95th pct {threshold:.2f} <- threshold")
    print(f"\nSURVIVORS: {len(survivors)}")
    for key in survivors:
        r = results[key]
        hs = r.get("holdout_s")
        print(f"  {key[0]} {key[1]}h: search {r['search_s']:.2f}, holdout "
              f"{('%.2f' % hs) if hs is not None else 'too few hours'} -> "
              f"{'PASS' if r.get('holdout_pass') else 'fail'}")
    print(f"\nVariants tested: {len(ss)} (v3) + {PRIOR_TALLY} earlier = {len(ss) + PRIOR_TALLY}. "
          "No fees, no P&L.")

    os.makedirs(args.out, exist_ok=True)
    keys = sorted({k for r in results.values() for k in r})
    with open(os.path.join(args.out, "test_v3_hourly_results.csv"), "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=keys)
        w.writeheader()
        w.writerows(results.values())
    return 0


if __name__ == "__main__":
    sys.exit(main())
