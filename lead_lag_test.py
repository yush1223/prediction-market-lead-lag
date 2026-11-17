"""Lead-lag test v2, implementing test_spec_v2.md exactly. No sweep.

For every series-matched pair in the frozen sign map, fits

    dLag[t+1] = a + b * dLead[t] + c * dLag[t]

on the early 65% of joint days, compares each pair's one-sided t against the
max-t of a date-shuffled null over all pairs, and refits survivors once on
the late 35%.

    python3 lead_lag_test.py --db kalshi.db --out report/
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import os
import sqlite3
import sys
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

import numpy as np

from pilot_test import _ts, cluster_ols, parse_strike

DAY = 86400
MAX_SPREAD = 0.20
SPLIT = 0.65
N_NULL = 500
SEED = 20261110
MIN_DAYS, MIN_CLUSTERS = 30, 8
SPEC_SHA = "f04c3c53f4d81d73fdf1da75b9159f60db63a4368354949ea76658d97f524803"
SIGN_MAP_SHA = "9a551c0a596c6e72a264b32de29731e37c9e4cca74d3485e98bcde48932fe5a2"


def sha(path: str) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def series_changes(conn: sqlite3.Connection, series: str, table: str = "candles",
                   period: int = DAY) -> Dict[int, dict]:
    """period index -> {event, change, kind, spread} under index v2. Daily by
    default; pass table="candles_1h", period=3600 for the hourly test."""
    meta: Dict[str, Tuple[str, tuple]] = {}
    close_of: Dict[str, int] = {}
    has_bin: Dict[str, bool] = defaultdict(bool)
    for t, ev, st, fl, cp, sub, close in conn.execute(
        "SELECT ticker, event_ticker, strike_type, floor_strike, cap_strike, yes_sub_title, close_time "
        "FROM markets WHERE series_ticker = ?", (series,)
    ):
        k = parse_strike(st, fl, cp, sub)
        if not k:
            continue
        meta[t] = (ev, k)
        close_of[ev] = max(close_of.get(ev, 0), _ts(close) or 0)
        has_bin[ev] |= k[0] == "bin"
    if not meta:
        return {}

    quotes: Dict[Tuple[str, int], Dict[str, Tuple[float, float]]] = defaultdict(dict)
    day_end: Dict[int, int] = {}
    ph = ",".join("?" * len(meta))
    for t, end_ts, bid, ask in conn.execute(
        f"SELECT ticker, end_ts, yes_bid_close, yes_ask_close FROM {table} WHERE ticker IN ({ph})", list(meta)
    ):
        if bid is None or ask is None or ask - bid > MAX_SPREAD:
            continue
        # daily candles end at ET midnight (04:00 or 05:00 UTC); hourly on the hour
        day = (end_ts - 6 * 3600) // DAY if period == DAY else end_ts // period
        day_end[day] = max(day_end.get(day, 0), end_ts)
        quotes[(meta[t][0], day)][t] = ((bid + ask) / 2, ask - bid)

    events_on: Dict[int, List[str]] = defaultdict(list)
    for ev, d in quotes:
        events_on[d].append(ev)

    out: Dict[int, dict] = {}
    for d, evs in events_on.items():
        live = [e for e in evs if close_of.get(e, 0) - day_end[d] >= DAY]
        if not live:
            continue
        ev = min(live, key=lambda e: close_of[e])
        today, prev = quotes[(ev, d)], quotes.get((ev, d - 1))
        if not prev:
            out[d] = {"event": ev, "change": None}
            continue
        common = [t for t in today if t in prev]
        if not common:
            out[d] = {"event": ev, "change": None}
            continue
        if has_bin[ev]:
            if len(common) < 2:
                out[d] = {"event": ev, "change": None}
                continue

            def ev_value(q):
                w = np.array([q[t][0] for t in common])
                if w.sum() <= 0:
                    return None
                x = []
                for t in common:
                    kind, lo, hi = meta[t][1]
                    x.append(hi if kind == "lt" else lo if kind == "gt" else (lo + hi) / 2)
                return float(np.dot(w / w.sum(), x))

            e0, e1 = ev_value(prev), ev_value(today)
            change = None if e0 is None or e1 is None else e1 - e0
            out[d] = {"event": ev, "change": change, "kind": "range", "spread": None}
        else:
            def p_above(t, q):
                kind = meta[t][1][0]
                return q[t][0] if kind == "gt" else 1 - q[t][0]

            k = min(common, key=lambda t: abs(p_above(t, prev) - 0.5))
            out[d] = {"event": ev, "change": p_above(k, today) - p_above(k, prev),
                      "kind": "threshold", "spread": today[k][1]}
    return out


def fit(rows: List[tuple], lead_override: Optional[np.ndarray] = None):
    y = np.array([r[1] for r in rows])
    lead = np.array([r[2] for r in rows]) if lead_override is None else lead_override
    X = np.column_stack([np.ones(len(rows)), lead, [r[3] for r in rows]])
    beta, se, g = cluster_ols(y, X, [r[4] for r in rows])
    t = beta[1] / se[1] if se[1] > 0 else 0.0
    return beta[1], t, g


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description="Lead-lag test v2 (test_spec_v2.md)")
    ap.add_argument("--db", default="kalshi.db")
    ap.add_argument("--out", default="report")
    args = ap.parse_args(argv)

    for path, want in (("test_spec_v2.md", SPEC_SHA), ("sign_map.csv", SIGN_MAP_SHA)):
        got = sha(path)
        print(f"{path} sha256 {got[:12]}... {'matches' if got == want else 'DOES NOT MATCH'}")
        if got != want:
            return 1

    with open("sign_map.csv") as fh:
        pairs = [r for r in csv.DictReader(fh) if r["matching"] == "series"]
    conn = sqlite3.connect(args.db)
    series = sorted({p["leader"] for p in pairs} | {p["laggard"] for p in pairs})
    idx = {s: series_changes(conn, s) for s in series}

    print("\nCONTINUITY (index v2)")
    for s in series:
        ch = [v["change"] for v in idx[s].values() if v["change"] is not None]
        nz = sum(abs(c) > 1e-12 for c in ch)
        print(f"  {s:<22} {len(ch):>5} days with a change, {nz / max(len(ch), 1):>4.0%} non-zero")

    data = {}
    for p in pairs:
        L, G = idx[p["leader"]], idx[p["laggard"]]
        rows = []
        for d in sorted(G):
            nxt, cur, ld = G.get(d + 1), G[d], L.get(d)
            if nxt and ld and cur["change"] is not None and nxt["change"] is not None and ld["change"] is not None:
                rows.append((d, nxt["change"], ld["change"], cur["change"], nxt["event"], nxt.get("spread")))
        cut = int(len(rows) * SPLIT)
        data[p["pair_id"]] = (p, rows[:cut], rows[cut:])

    testable = {}
    for pid, (p, search, _hold) in data.items():
        if len(search) >= MIN_DAYS and len({r[4] for r in search}) >= MIN_CLUSTERS:
            testable[pid] = search

    results = {}
    for pid, (p, search, hold) in data.items():
        sign = 1 if p["sign"] == "+" else -1
        row = {"pair_id": pid, "leader": p["leader"], "laggard": p["laggard"], "sign": p["sign"],
               "search_days": len(search), "holdout_days": len(hold),
               "search_clusters": len({r[4] for r in search})}
        if pid in testable:
            b, t, g = fit(search)
            row.update(b=b, search_s=t * sign, search_t=t)
        results[pid] = row

    # Date-shuffled null: max one-sided s across all testable pairs.
    rng = np.random.default_rng(SEED)
    null_max = []
    for _ in range(N_NULL):
        best = -np.inf
        for pid, search in testable.items():
            sign = 1 if data[pid][0]["sign"] == "+" else -1
            lead = np.array([r[2] for r in search])
            _, t, _ = fit(search, rng.permutation(lead))
            best = max(best, t * sign)
        null_max.append(best)
    threshold = float(np.percentile(null_max, 95))

    survivors = [pid for pid in testable if results[pid]["search_s"] > threshold]
    for pid in survivors:
        p, _, hold = data[pid]
        sign = 1 if p["sign"] == "+" else -1
        if len(hold) >= MIN_DAYS // 2 and len({r[4] for r in hold}) >= MIN_CLUSTERS // 2:
            b, t, _ = fit(hold)
            results[pid].update(holdout_b=b, holdout_s=t * sign, holdout_pass=t * sign >= 2)
        else:
            results[pid].update(holdout_s=None, holdout_pass=False)

    for pid, (p, search, _h) in data.items():
        sp = [r[5] for r in search if r[5] is not None]
        lead = [r[2] for r in search]
        if "b" in results[pid] and sp:
            results[pid]["pred_move_c"] = abs(results[pid]["b"]) * float(np.std(lead)) * 100
            results[pid]["typical_spread_c"] = float(np.median(sp)) * 100

    ordered = sorted(results.values(), key=lambda r: -(r.get("search_s") if r.get("search_s") is not None else -99))
    print(f"\nSEARCH PERIOD  (early {SPLIT:.0%} of each pair's joint days)")
    print(f"  {'pair':<5}{'leader -> laggard':<40}{'sign':>5}{'days':>6}{'events':>7}{'s = signed t':>13}")
    for r in ordered:
        s = f"{r['search_s']:>13.2f}" if "search_s" in r else f"{'untestable':>13}"
        print(f"  {r['pair_id']:<5}{(r['leader'] + ' -> ' + r['laggard'])[:39]:<40}{r['sign']:>5}"
              f"{r['search_days']:>6}{r['search_clusters']:>7}{s}")

    ss = np.array([r["search_s"] for r in results.values() if "search_s" in r])
    print(f"\n  testable pairs: {len(ss)} of {len(results)}")
    if len(ss):
        print(f"  distribution of s: min {ss.min():.2f}  25% {np.percentile(ss, 25):.2f}  median "
              f"{np.median(ss):.2f}  75% {np.percentile(ss, 75):.2f}  max {ss.max():.2f}")
        print(f"  pairs with s >= 2 (naive, uncorrected): {int((ss >= 2).sum())}   "
              f"expected by chance at 2.3% one-sided: {0.023 * len(ss):.1f}")
    print(f"\nSHUFFLED NULL  ({N_NULL} date-shuffles of the whole search)")
    print(f"  best-noise s: median {np.median(null_max):.2f}, 95th percentile {threshold:.2f}  <- threshold")
    print(f"\nSURVIVORS (search s > {threshold:.2f}): {len(survivors)}")
    for pid in survivors:
        r = results[pid]
        hs = r.get("holdout_s")
        print(f"  {pid} {r['leader']} -> {r['laggard']}: search s {r['search_s']:.2f}, "
              f"holdout s {('%.2f' % hs) if hs is not None else 'too few days'} "
              f"-> {'PASS' if r.get('holdout_pass') else 'fail'}  ({r['holdout_days']} holdout days)")
    if any("pred_move_c" in r for r in results.values()):
        print("\nSIZE vs SPREAD (threshold laggards; 1-sd leader move, search period)")
        for r in ordered:
            if "pred_move_c" in r and r.get("search_s", 0) >= 1.5:
                print(f"  {r['pair_id']} {r['laggard']}: predicted {r['pred_move_c']:.2f}c vs spread {r['typical_spread_c']:.1f}c")
    print(f"\nVariants tested: {len(ss)} (v2) + 2 (v1 pilot). Fees not modelled; no P&L claimed.")

    os.makedirs(args.out, exist_ok=True)
    out = os.path.join(args.out, "test_v2_results.csv")
    keys = sorted({k for r in results.values() for k in r})
    with open(out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=keys)
        w.writeheader()
        w.writerows(ordered)
    with open(os.path.join(args.out, "test_v2_null.csv"), "w") as fh:
        fh.write("max_s\n" + "\n".join(f"{x:.4f}" for x in null_max) + "\n")
    print(f"  [csv] {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
