"""Pilot lead-lag test, implementing pilot_spec.md exactly. No sweep.

Builds one daily number per series (the implied median of the underlying, from
the front event's strike ladder), then for each pilot pair fits

    dLag[t+1] = a + b * dLead[t] + c * dLag[t]

with standard errors clustered by laggard event. b is the delayed response.

    python3 pilot_test.py --db kalshi.db
"""

from __future__ import annotations

import argparse
import hashlib
import math
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import numpy as np

DAY = 86400
MAX_SPREAD = 0.20
PAIRS = [("E01", "KXWTIW", "KXAAAGASW", +1), ("E23", "KXTNOTEW", "KXFRM", +1)]
SPEC_SHA = "e8c843dfe4ec45cc537e047e6ee49344febfbd054f7461bd4d7c3beb65048366"

NUM = r"\$?(-?[\d.]+)%?"


def parse_strike(strike_type, floor, cap, sub) -> Optional[Tuple[str, float, float]]:
    """(kind, lo, hi): kind 'gt' means P(x > lo); 'lt' means P(x < hi);
    'bin' means P(lo <= x <= hi). Older markets carry no strike fields, so
    fall back to the subtitle ('Above 6.50%', '$64.99 or below', '$77 to 77.99')."""
    if strike_type == "greater" or strike_type == "greater_or_equal":
        return ("gt", floor, math.inf) if floor is not None else None
    if strike_type == "less" or strike_type == "less_or_equal":
        return ("lt", -math.inf, cap) if cap is not None else None
    if strike_type == "between":
        return ("bin", floor, cap) if floor is not None and cap is not None else None
    s = (sub or "").replace(",", "")
    if m := re.search(rf"{NUM}\s+to\s+{NUM}", s):
        return ("bin", float(m[1]), float(m[2]))
    if (m := re.search(rf"Above\s+{NUM}", s, re.I)) or (m := re.search(rf"{NUM}\s+or above", s, re.I)):
        return ("gt", float(m[1]), math.inf)
    if (m := re.search(rf"Below\s+{NUM}", s, re.I)) or (m := re.search(rf"{NUM}\s+or below", s, re.I)):
        return ("lt", -math.inf, float(m[1]))
    return None


def pav_decreasing(p: List[float]) -> List[float]:
    """Pool-adjacent-violators fit of a non-increasing sequence. P(x > k)
    cannot rise with k; noisy quotes sometimes do."""
    blocks = [[v, 1] for v in p]
    out: List[List[float]] = []
    for b in blocks:
        out.append(b)
        while len(out) > 1 and out[-2][0] < out[-1][0]:
            v2, n2 = out.pop()
            v1, n1 = out.pop()
            out.append([(v1 * n1 + v2 * n2) / (n1 + n2), n1 + n2])
    fit: List[float] = []
    for v, n in out:
        fit += [v] * int(n)
    return fit


def median_from_ladder(strikes: List[Tuple[str, float, float, float]]) -> Optional[Tuple[float, float]]:
    """(implied median, density at the median) from one event-day's strikes.
    Density is probability per unit of the underlying, used later to turn a
    predicted underlying move into a contract-price move."""
    bins = [s for s in strikes if s[0] == "bin"]
    if bins:
        parts = []
        for kind, lo, hi, p in strikes:
            parts.append((lo if kind != "lt" else -math.inf, hi if kind != "gt" else math.inf, p))
        parts.sort(key=lambda t: (t[0], t[1]))
        total = sum(p for _, _, p in parts)
        if not 0.7 <= total <= 1.3:
            return None
        cum = 0.0
        for lo, hi, p in parts:
            q = p / total
            if cum + q >= 0.5 and q > 0:
                if math.isinf(lo) or math.isinf(hi):
                    return None  # median sits in an open tail: no interior estimate
                width = hi - lo or 1e-9
                return lo + (0.5 - cum) / q * width, q / width
            cum += q
        return None
    pts = sorted((lo if kind == "gt" else hi, p if kind == "gt" else 1 - p)
                 for kind, lo, hi, p in strikes)
    if len(pts) < 2:
        return None
    xs = [x for x, _ in pts]
    ps = pav_decreasing([p for _, p in pts])
    for i in range(len(xs) - 1):
        if ps[i] >= 0.5 >= ps[i + 1] and ps[i] > ps[i + 1] and xs[i + 1] > xs[i]:
            w = (ps[i] - 0.5) / (ps[i] - ps[i + 1])
            return xs[i] + w * (xs[i + 1] - xs[i]), (ps[i] - ps[i + 1]) / (xs[i + 1] - xs[i])
    return None


def _ts(v: Optional[str]) -> Optional[int]:
    return int(datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()) if v else None


def series_index(conn: sqlite3.Connection, series: str) -> Dict[int, dict]:
    """day -> {event, median, density, spread} for the front event, plus the
    within-event change from the previous day."""
    meta = {}
    close_of: Dict[str, int] = {}
    for t, ev, st, fl, cp, sub, close in conn.execute(
        "SELECT ticker, event_ticker, strike_type, floor_strike, cap_strike, yes_sub_title, close_time "
        "FROM markets WHERE series_ticker = ?", (series,)
    ):
        k = parse_strike(st, fl, cp, sub)
        if k:
            meta[t] = (ev, k)
            close_of[ev] = max(close_of.get(ev, 0), _ts(close) or 0)

    by_ev_day: Dict[Tuple[str, int], list] = defaultdict(list)
    spreads: Dict[Tuple[str, int], list] = defaultdict(list)
    day_end: Dict[int, int] = {}
    ph = ",".join("?" * len(meta)) or "''"
    for t, end_ts, bid, ask in conn.execute(
        f"SELECT ticker, end_ts, yes_bid_close, yes_ask_close FROM candles WHERE ticker IN ({ph})",
        list(meta),
    ):
        if bid is None or ask is None or ask - bid > MAX_SPREAD:
            continue
        ev, (kind, lo, hi) = meta[t]
        day = (end_ts - 6 * 3600) // DAY  # candles end at ET midnight: 04:00 or 05:00 UTC
        day_end[day] = end_ts
        by_ev_day[(ev, day)].append((kind, lo, hi, (bid + ask) / 2))
        spreads[(ev, day)].append((abs((bid + ask) / 2 - 0.5), ask - bid))

    med: Dict[Tuple[str, int], Tuple[float, float]] = {}
    for key, strikes in by_ev_day.items():
        r = median_from_ladder(strikes)
        if r:
            med[key] = r

    days = sorted({d for _, d in med})
    out: Dict[int, dict] = {}
    for d in days:
        live = [ev for (ev, dd) in med if dd == d and close_of.get(ev, 0) - day_end[d] >= DAY]
        if not live:
            continue
        ev = min(live, key=lambda e: close_of[e])
        m, dens = med[(ev, d)]
        prev = med.get((ev, d - 1))
        near50 = min(spreads[(ev, d)])[1]
        out[d] = {"event": ev, "median": m, "density": dens, "spread": near50,
                  "change": (m - prev[0]) if prev else None}
    return out


def cluster_ols(y: np.ndarray, X: np.ndarray, groups: List[str]):
    n, k = X.shape
    XtX_inv = np.linalg.inv(X.T @ X)
    beta = XtX_inv @ X.T @ y
    u = y - X @ beta
    meat = np.zeros((k, k))
    gidx: Dict[str, List[int]] = defaultdict(list)
    for i, g in enumerate(groups):
        gidx[g].append(i)
    for rows in gidx.values():
        s = X[rows].T @ u[rows]
        meat += np.outer(s, s)
    G = len(gidx)
    scale = (G / (G - 1)) * ((n - 1) / (n - k)) if G > 1 else 1.0
    V = scale * XtX_inv @ meat @ XtX_inv
    return beta, np.sqrt(np.diag(V)), G


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description="Pilot lead-lag test (pilot_spec.md)")
    ap.add_argument("--db", default="kalshi.db")
    ap.add_argument("--fee-per-contract", type=float, default=None,
                    help="round-trip fee in dollars per contract near 50c, from the fee schedule")
    args = ap.parse_args(argv)

    with open("pilot_spec.md", "rb") as fh:
        sha = hashlib.sha256(fh.read()).hexdigest()
    print(f"pilot_spec.md sha256 {sha[:12]}... {'matches' if sha == SPEC_SHA else 'DOES NOT MATCH the frozen spec'}")
    if sha != SPEC_SHA:
        return 1

    conn = sqlite3.connect(args.db)
    idx = {s: series_index(conn, s) for _, a, b, _ in PAIRS for s in (a, b)}
    for s, d in idx.items():
        ch = [v["change"] for v in d.values() if v["change"] is not None]
        moving = sum(abs(c) > 1e-9 for c in ch)
        print(f"  index {s:<10} {len(d):>4} days, {len(ch):>4} with a change, "
              f"{moving / max(len(ch), 1):.0%} of those non-zero, "
              f"{len({v['event'] for v in d.values()})} events")

    print(f"\n{'pair':<5}{'leader -> laggard':<26}{'b':>10}{'t':>7}{'pass':>6}{'days':>6}{'events':>7}"
          f"{'same-day r':>11}")
    results = []
    for pid, lead, lag, sign in PAIRS:
        L, G = idx[lead], idx[lag]
        rows = []
        for d, v in G.items():
            nxt = G.get(d + 1)
            ld = L.get(d)
            if not nxt or not ld or v["change"] is None or nxt["change"] is None or ld["change"] is None:
                continue
            rows.append((nxt["change"], ld["change"], v["change"], nxt["event"], nxt["density"], nxt["spread"]))
        if len(rows) < 10:
            print(f"{pid:<5}{lead + ' -> ' + lag:<26}  too few joint days ({len(rows)})")
            continue
        y = np.array([r[0] for r in rows])
        X = np.column_stack([np.ones(len(rows)), [r[1] for r in rows], [r[2] for r in rows]])
        beta, se, n_ev = cluster_ols(y, X, [r[3] for r in rows])
        b, t = beta[1], beta[1] / se[1] if se[1] > 0 else float("nan")
        ok = (np.sign(b) == sign) and t * sign >= 2
        same_day = np.corrcoef([r[1] for r in rows], [r[2] for r in rows])[0, 1]
        print(f"{pid:<5}{lead + ' -> ' + lag:<26}{b:>10.4f}{t:>7.2f}{'YES' if ok else 'no':>6}"
              f"{len(rows):>6}{n_ev:>7}{same_day:>11.2f}")
        sd_lead = float(np.std(X[:, 1]))
        dens = float(np.median([r[4] for r in rows]))
        spread = float(np.median([r[5] for r in rows]))
        results.append((pid, lead, lag, b, sd_lead, dens, spread))

    print("\nSize of the predicted move vs cost (one-standard-deviation leader move):")
    for pid, lead, lag, b, sd_lead, dens, spread in results:
        move_cents = abs(b) * sd_lead * dens * 100
        fee = f"{args.fee_per_contract * 100:.1f}c" if args.fee_per_contract is not None else "n/a"
        print(f"  {pid} {lag}: predicted price move {move_cents:.2f}c vs typical spread "
              f"{spread * 100:.1f}c + round-trip fee {fee}")
    print("\nVariants tested: 2 (pilot, pre-specified). Counted toward the final tally.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
