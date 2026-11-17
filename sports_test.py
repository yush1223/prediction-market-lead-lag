"""Sports game -> season lead-lag, matched by team/player (test_spec_v6_sports.md).

    python3 sports_test.py --db kalshi.db --out report/
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sqlite3
import sys
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

import numpy as np

from hourly_suite import MIN_CLUSTERS, MIN_HOURS, SIGN_MAP_SHA, SPLIT, SEED, cluster_t, sha
from pilot_test import _ts, parse_strike

SPEC_SHA = "d7b6a7a99b6dc5e89e99dc3e36a79c3dd2b74faba1b623b3975f477948a06ac0"
PAIRS = ["S01", "S05", "S06", "S07", "S08", "S09", "S10"]
LADDERS = {"KXNFLWINS", "KXNCAAFWINS"}
MIN_LEFT = 3600
MAX_SPREAD = 0.20
HORIZONS = [1, 6]
N_NULL = 500


def entity_of(series: str, ticker: str, event: str) -> str:
    if series in LADDERS:
        return re.sub(r"^\d+", "", event.split("-", 1)[1])
    return ticker.rsplit("-", 1)[1]


def entity_changes(conn: sqlite3.Connection, series: str) -> Dict[str, Dict[int, Tuple[str, Optional[float]]]]:
    """entity -> hour -> (event, change) under the v6 index."""
    meta = {}
    close_of: Dict[str, int] = {}
    for t, ev, st, fl, cp, sub, close in conn.execute(
        "SELECT ticker, event_ticker, strike_type, floor_strike, cap_strike, yes_sub_title, close_time "
        "FROM markets WHERE series_ticker = ? AND volume >= 100", (series,)
    ):
        k = parse_strike(st, fl, cp, sub) if series in LADDERS else ("single", None, None)
        if not k:
            continue
        ent = entity_of(series, t, ev)
        meta[t] = (ent, ev, k)
        close_of[ev] = max(close_of.get(ev, 0), _ts(close) or 0)
    if not meta:
        return {}
    quotes: Dict[Tuple[str, str, int], Dict[str, float]] = defaultdict(dict)
    hour_end: Dict[int, int] = {}
    ph = ",".join("?" * len(meta))
    for t, end_ts, bid, ask in conn.execute(
        f"SELECT ticker, end_ts, yes_bid_close, yes_ask_close FROM candles_1h WHERE ticker IN ({ph})", list(meta)
    ):
        if bid is None or ask is None or ask - bid > MAX_SPREAD:
            continue
        ent, ev, _k = meta[t]
        h = end_ts // 3600
        hour_end[h] = end_ts
        quotes[(ent, ev, h)][t] = (bid + ask) / 2

    by_ent_hour: Dict[Tuple[str, int], List[str]] = defaultdict(list)
    for ent, ev, h in quotes:
        by_ent_hour[(ent, h)].append(ev)
    out: Dict[str, Dict[int, Tuple[str, Optional[float]]]] = defaultdict(dict)
    for (ent, h), evs in by_ent_hour.items():
        live = [e for e in evs if close_of.get(e, 0) - hour_end[h] >= MIN_LEFT]
        if not live:
            continue
        ev = min(live, key=lambda e: close_of[e])
        today, prev = quotes[(ent, ev, h)], quotes.get((ent, ev, h - 1))
        common = [t for t in today if prev and t in prev]
        if not common:
            out[ent][h] = (ev, None)
            continue

        def p_above(t, q):
            kind = meta[t][2][0]
            return 1 - q[t] if kind == "lt" else q[t]

        k = min(common, key=lambda t: abs(p_above(t, prev) - 0.5))
        out[ent][h] = (ev, p_above(k, today) - p_above(k, prev))
    return out


def pooled_rows(lead: dict, lag: dict, k: int):
    """Rows across shared entities: hour, y, x_lead, x_lag, cluster."""
    rows = []
    for ent in set(lead) & set(lag):
        L, G = lead[ent], lag[ent]
        for h, (ev, ch) in G.items():
            if ch is None or h not in L or L[h][1] is None:
                continue
            fut = [G.get(h + j) for j in range(1, k + 1)]
            if any(f is None or f[1] is None for f in fut):
                continue
            rows.append((h, sum(f[1] for f in fut), L[h][1], ch, f"{ent}|{fut[0][0]}"))
    rows.sort(key=lambda r: r[0])
    return rows, len(set(lead) & set(lag))


def arrays(rows):
    y = np.array([r[1] for r in rows]); x = np.array([r[2] for r in rows])
    z = np.array([r[3] for r in rows]); g = np.array([r[4] for r in rows])
    return y, x, z, g


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description="Sports lead-lag (v6)")
    ap.add_argument("--db", default="kalshi.db")
    ap.add_argument("--out", default="report")
    args = ap.parse_args(argv)
    for path, want in (("test_spec_v6_sports.md", SPEC_SHA), ("sign_map.csv", SIGN_MAP_SHA)):
        got = sha(path)
        print(f"{path} sha256 {got[:12]}... {'matches' if got == want else 'DOES NOT MATCH'}")
        if got != want:
            return 1
    with open("sign_map.csv") as fh:
        pairs = {r["pair_id"]: r for r in csv.DictReader(fh) if r["pair_id"] in PAIRS}
    conn = sqlite3.connect(args.db)
    names = sorted({p["leader"] for p in pairs.values()} | {p["laggard"] for p in pairs.values()})
    E = {s: entity_changes(conn, s) for s in names}
    lines = ["SPORTS v6 - game -> season, matched by team/player", "", "COVERAGE"]
    for s in names:
        n_ch = sum(1 for d in E[s].values() for v in d.values() if v[1] is not None)
        lines.append(f"  {s:<18} entities {len(E[s]):>5}, entity-hours with a change {n_ch:>8}")

    rng = np.random.default_rng(SEED)
    results = []
    for fam, rev in (("F4 signed", False), ("F4r reversed placebo", True)):
        tests = []
        for pid in PAIRS:
            p = pairs[pid]
            lead, lag = (p["laggard"], p["leader"]) if rev else (p["leader"], p["laggard"])
            for k in HORIZONS:
                rows, n_ent = pooled_rows(E[lead], E[lag], k)
                cut = int(len(rows) * SPLIT)
                search, hold = rows[:cut], rows[cut:]
                r = {"family": fam, "pair": pid + ("-rev" if rev else ""), "leader": lead, "laggard": lag,
                     "horizon_h": k, "entities": n_ent, "search_hours": len(search), "holdout_hours": len(hold),
                     "clusters": len({x[4] for x in search})}
                if len(search) >= MIN_HOURS and r["clusters"] >= MIN_CLUSTERS:
                    b, t, G = cluster_t(*arrays(search))
                    r.update(b=b, t=t, stat=t if not rev else abs(t))
                    tests.append((r, arrays(search), arrays(hold) if hold else None))
                results.append(r)
        if not tests:
            lines.append(f"\n{fam}: no testable tests")
            continue
        nulls = np.full(N_NULL, -np.inf)
        for i in range(N_NULL):
            for r, (y, x, z, g), _h in tests:
                _, t, _ = cluster_t(y, rng.permutation(x), z, g)
                nulls[i] = max(nulls[i], abs(t) if rev else t)
        thr = float(np.percentile(nulls, 95))
        lines.append(f"\n{'=' * 78}\n{fam}\n{'=' * 78}")
        lines.append(f"  testable {len(tests)}; null best-noise median {np.median(nulls):.2f}, 95th pct {thr:.2f}")
        for r, _s, hold in sorted(tests, key=lambda x: -x[0]["stat"]):
            flag = ""
            if r["stat"] > thr:
                if hold is not None and len(hold[0]) >= MIN_HOURS // 2 and len(set(hold[3])) >= MIN_CLUSTERS // 2:
                    _, ht, _ = cluster_t(*hold)
                    ok = (ht >= 2) if not rev else (abs(ht) >= 2 and np.sign(ht) == np.sign(r["t"]))
                    r.update(holdout_t=ht, holdout_pass=bool(ok))
                    flag = f"  SURVIVOR -> holdout {ht:.2f} {'PASS' if ok else 'fail'}"
                else:
                    r.update(holdout_t=None, holdout_pass=False)
                    flag = "  SURVIVOR -> holdout too small"
            lines.append(f"  {r['pair']:<8} {r['leader']:>16} -> {r['laggard']:<18} {r['horizon_h']}h  stat "
                         f"{r['stat']:6.2f}  hours {r['search_hours']:>6}  clusters {r['clusters']:>4}  "
                         f"entities {r['entities']:>4}{flag}")
        for r in results:
            if r["family"] == fam and "t" not in r:
                lines.append(f"  {r['pair']:<8} {r['leader']:>16} -> {r['laggard']:<18} {r['horizon_h']}h  "
                             f"untestable (hours {r['search_hours']}, clusters {r['clusters']}, entities {r['entities']})")
    n = sum(1 for r in results if "t" in r)
    lines.append(f"\nVariants tested in v6: {n}. Added to the overnight tally. No fees, no P&L.")
    print("\n".join(lines))
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "v6_sports_report.txt"), "w") as fh:
        fh.write("\n".join(lines) + "\n")
    keys = sorted({k for r in results for k in r})
    with open(os.path.join(args.out, "v6_sports_tests.csv"), "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=keys)
        w.writeheader()
        w.writerows(results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
