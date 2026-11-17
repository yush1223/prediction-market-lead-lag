"""Stale-quote fill check on AAA gas weekly, implementing test_spec_v7_trades.md.

    python3 trades_check.py --db kalshi.db --trades-db trades.db --out report/
"""

from __future__ import annotations

import argparse
import bisect
import os
import sqlite3
import sys
from collections import defaultdict
from typing import Optional

import numpy as np

from hourly_suite import sha
from lead_lag_test import series_changes
from minute_test import build_bars
from pilot_test import _ts

SPEC_SHA = "94af0d201a500009fb167640f91f58ad0c491afc90014c8789a5f86b3ac0501c"
LEAD, LAG = "KXWTIW", "KXAAAGASW"
WINDOW = 3600
HORIZONS = {"30min": 1800, "2h": 7200, "6h": 21600}
PRIOR_TALLY = 6880


def wls_cluster(m, w, A, B, g):
    """Contract-weighted regression of markout on group dummies; returns
    (coef, t) for A and B relative to baseline C, clustered by g."""
    X = np.column_stack([np.ones_like(m), A, B])
    sw = np.sqrt(w)
    Xw, yw = X * sw[:, None], m * sw
    inv = np.linalg.inv(Xw.T @ Xw)
    beta = inv @ Xw.T @ yw
    u = yw - Xw @ beta
    _, gi = np.unique(g, return_inverse=True)
    G = gi.max() + 1
    S = np.zeros((G, 3))
    np.add.at(S, gi, Xw * u[:, None])
    n = len(m)
    V = (G / (G - 1)) * ((n - 1) / (n - 3)) * inv @ (S.T @ S) @ inv
    se = np.sqrt(np.diag(V))
    return beta, beta / se, G


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description="Stale-quote fill check (v7)")
    ap.add_argument("--db", default="kalshi.db")
    ap.add_argument("--trades-db", default="trades.db")
    ap.add_argument("--out", default="report")
    args = ap.parse_args(argv)
    got = sha("test_spec_v7_trades.md")
    print(f"test_spec_v7_trades.md sha256 {got[:12]}... {'matches' if got == SPEC_SHA else 'DOES NOT MATCH'}")
    if got != SPEC_SHA:
        return 1

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True, timeout=120)
    conn.execute("PRAGMA temp_store = MEMORY")
    build_bars(conn, [LEAD])
    L = series_changes(conn, LEAD, table="bars_5m", period=300)
    nz = np.array([abs(v["change"]) for v in L.values() if v["change"] is not None and abs(v["change"]) > 1e-12])
    cut = float(np.percentile(nz, 95))
    big = sorted((h * 300, 1 if v["change"] > 0 else -1) for h, v in L.items()
                 if v["change"] is not None and abs(v["change"]) >= cut)
    big_t = [b[0] for b in big]
    print(f"oil 5-min bars with a change: {len(nz):,}; big-move cutoff |d| >= {cut:.3f}; big bars {len(big):,}")

    span = conn.execute("SELECT MIN(c.end_ts), MAX(c.end_ts) FROM candles_1m c JOIN markets m ON m.ticker = c.ticker "
                        "WHERE m.series_ticker IN (?, ?)", (LEAD, LAG)).fetchone()
    meta = {t: (ev, _ts(c)) for t, ev, c in conn.execute(
        "SELECT ticker, event_ticker, close_time FROM markets WHERE series_ticker = ?", (LAG,))}
    quotes = defaultdict(list)
    for t, e, b, a in conn.execute(
        "SELECT c.ticker, c.end_ts, c.yes_bid_close, c.yes_ask_close FROM candles_1m c JOIN markets m "
        "ON m.ticker = c.ticker WHERE m.series_ticker = ? ORDER BY c.ticker, c.end_ts", (LAG,)):
        quotes[t].append((e, b, a))
    qt = {t: [x[0] for x in v] for t, v in quotes.items()}

    def mid_at(t, T):
        if t not in qt or T > (meta[t][1] or 0):
            return None
        i = bisect.bisect_right(qt[t], T) - 1
        if i < 0 or T - qt[t][i] > 86400:
            return None
        _, b, a = quotes[t][i]
        if b is None or a is None or a - b > 0.20:
            return None
        return (a + b) / 2

    tconn = sqlite3.connect(args.trades_db)
    trades = tconn.execute("SELECT ticker, ts, yes_price, count, taker_side FROM trades WHERE ts BETWEEN ? AND ?",
                           span).fetchall()
    print(f"gas trades in the 1-minute span: {len(trades):,}")

    lines = ["STALE-QUOTE FILL CHECK - AAA gas weekly trades after big WTI moves (spec v7)",
             f"  big oil move = top 5% of 5-min changes (|d| >= {cut:.3f}); window 60 min after the bar"]
    primary = None
    for name, H in HORIZONS.items():
        m, w, A, B, g, rows = [], [], [], [], [], []
        for t, ts, yp, cnt, side in trades:
            if t not in meta or yp is None or not cnt or side not in ("yes", "no"):
                continue
            mid = mid_at(t, ts + H)
            if mid is None:
                continue
            s = 1 if side == "yes" else -1
            i = bisect.bisect_right(big_t, ts) - 1
            grp = "C"
            if i >= 0 and ts - big_t[i] <= WINDOW:
                grp = "A" if s == big[i][1] else "B"
            mk = s * (mid - yp)
            m.append(mk); w.append(cnt); A.append(grp == "A"); B.append(grp == "B"); g.append(meta[t][0])
            rows.append((grp, mk, cnt, t, ts, yp, side))
        m, w = np.array(m), np.array(w)
        A, B, g = np.array(A, float), np.array(B, float), np.array(g)
        beta, tv, G = wls_cluster(m, w, A, B, g)
        lines.append(f"\n  horizon {name}  (gas events {G})")
        lines.append(f"    {'group':<22}{'trades':>9}{'contracts':>12}{'mean markout':>14}{'share > 0':>11}{'sum $':>11}")
        for lab, code in (("A aligned w/ oil", "A"), ("B against oil", "B"), ("C baseline", "C")):
            sel = [r for r in rows if r[0] == code]
            if not sel:
                continue
            mm = np.array([r[1] for r in sel]); ww = np.array([r[2] for r in sel])
            lines.append(f"    {lab:<22}{len(sel):>9,}{ww.sum():>12,.0f}{np.average(mm, weights=ww) * 100:>12.2f}c"
                         f"{(mm > 0).mean():>11.0%}{(mm * ww).sum():>11,.0f}")
        lines.append(f"    A - C = {beta[1] * 100:.2f}c (t {tv[1]:.2f});  B - C = {beta[2] * 100:.2f}c (t {tv[2]:.2f})")
        if name == "2h":
            primary = (beta[1], tv[1])
            a_rows = [r for r in rows if r[0] == "A"]
            if a_rows:
                top = max(a_rows, key=lambda r: r[1] * r[2])
                tot = sum(r[1] * r[2] for r in a_rows)
                lines.append(f"    largest single A trade: {top[3]} {top[6]} {top[2]:.0f} @ {top[5]:.2f}, "
                             f"markout {top[1] * 100:.1f}c -> ${top[1] * top[2]:,.0f} of A's ${tot:,.0f} total")
    b, t = primary
    lines.append(f"\n  PRIMARY (2h, A - C): {b * 100:.2f}c per contract, t {t:.2f} -> "
                 f"{'PASS' if b > 0 and t >= 2 else 'no'}")
    lines.append(f"  Tally: {PRIOR_TALLY} + 1 = {PRIOR_TALLY + 1}. No fees (taker fees would reduce A's markout).")
    print("\n".join(lines))
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "v7_trades_report.txt"), "w") as fh:
        fh.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
