"""Overnight hourly suite, implementing test_spec_v5_overnight.md.

Family 1: signed pairs (evidence). Family 2: reversed pairs (placebo).
Family 3: broad two-sided sweep over every indexable pair (exploratory).

    python3 hourly_suite.py --db kalshi.db --out report/
    python3 hourly_suite.py --selftest        # synthetic data, checks the machinery
"""

from __future__ import annotations

import argparse
import csv
import os
import sqlite3
import sys
from typing import Dict, List, Optional, Tuple

import numpy as np
from scipy import stats

SPEC_SHA = "38ec27b383e87435e888fa313e3957d5148fda231259835a1387787861fa06e2"
SIGN_MAP_SHA = "9a551c0a596c6e72a264b32de29731e37c9e4cca74d3485e98bcde48932fe5a2"
EXT_SHA = "fe37c6293fdb85e5e55b87ed65dcb16db9223ba2616a795e4419fa2c3705cb5f"
V3_PAIRS = {"E01", "E06", "E23", "E38", "E40"}
SPLIT, SEED, MIN_HOURS, MIN_CLUSTERS = 0.65, 20261110, 100, 8
PRIOR_TALLY = 52


# --------------------------------------------------------------------------- data

class Series:
    """One series on a common hourly axis: change (nan = none), event code, spread."""

    def __init__(self, n: int) -> None:
        self.chg = np.full(n, np.nan)
        self.ev = np.full(n, -1, dtype=np.int64)
        self.spread = np.full(n, np.nan)


def load_series(conn: sqlite3.Connection, names: List[str]) -> Tuple[Dict[str, Series], int]:
    from lead_lag_test import series_changes
    raw = {s: series_changes(conn, s, table="candles_1h", period=3600) for s in names}
    hours = [h for d in raw.values() for h in d]
    h0, h1 = min(hours), max(hours)
    codes: Dict[str, int] = {}
    out = {}
    for s, d in raw.items():
        ser = Series(h1 - h0 + 2)
        for h, v in d.items():
            i = h - h0
            ser.ev[i] = codes.setdefault(v["event"], len(codes))
            if v["change"] is not None:
                ser.chg[i] = v["change"]
            if v.get("spread") is not None:
                ser.spread[i] = v["spread"]
        out[s] = ser
    return out, h0


def design(lead: Series, lag: Series, k: int):
    """Rows for one test, in time order: y, x_lead, x_lag, cluster, spread."""
    n = len(lag.chg)
    ok_lag = ~np.isnan(lag.chg)
    filled = np.where(ok_lag, lag.chg, 0.0)
    cs = np.concatenate([[0.0], np.cumsum(filled)])
    cv = np.concatenate([[0], np.cumsum(ok_lag)])
    h = np.arange(n - k)
    fut_sum = cs[h + k + 1] - cs[h + 1]
    fut_ok = (cv[h + k + 1] - cv[h + 1]) == k
    mask = ok_lag[h] & ~np.isnan(lead.chg[h]) & fut_ok
    idx = h[mask]
    return (fut_sum[mask], lead.chg[idx], lag.chg[idx], lag.ev[idx + 1], lag.spread[idx + 1])


def cluster_t(y, x, z, g) -> Tuple[float, float, int]:
    """b and clustered t on the lead coefficient; G clusters."""
    X = np.column_stack([np.ones_like(x), x, z])
    XtX = X.T @ X
    try:
        inv = np.linalg.inv(XtX)
    except np.linalg.LinAlgError:
        return 0.0, 0.0, 0
    beta = inv @ (X.T @ y)
    u = y - X @ beta
    _, gi = np.unique(g, return_inverse=True)
    G = gi.max() + 1
    S = np.zeros((G, 3))
    np.add.at(S, gi, X * u[:, None])
    n = len(y)
    scale = (G / (G - 1)) * ((n - 1) / (n - 3)) if G > 1 else 1.0
    V = scale * inv @ (S.T @ S) @ inv
    se = np.sqrt(max(V[1, 1], 0.0))
    return float(beta[1]), float(beta[1] / se) if se > 0 else 0.0, int(G)


# --------------------------------------------------------------------------- tests

class Test:
    def __init__(self, family: str, pid: str, lead: str, lag: str, k: int, sign: int, story: bool):
        self.family, self.pid, self.lead, self.lag, self.k = family, pid, lead, lag, k
        self.sign, self.story = sign, story
        self.res: Dict[str, object] = {}


def prepare(t: Test, S: Dict[str, Series]) -> Optional[tuple]:
    if t.lead not in S or t.lag not in S:
        return None
    y, x, z, g, sp = design(S[t.lead], S[t.lag], t.k)
    cut = int(len(y) * SPLIT)
    search = (y[:cut], x[:cut], z[:cut], g[:cut], sp[:cut])
    hold = (y[cut:], x[cut:], z[cut:], g[cut:], sp[cut:])
    t.res.update(search_hours=cut, holdout_hours=len(y) - cut,
                 search_clusters=len(np.unique(g[:cut])) if cut else 0)
    if cut < MIN_HOURS or t.res["search_clusters"] < MIN_CLUSTERS:
        t.res["status"] = "untestable"
        return None
    b, tv, G = cluster_t(*search[:4])
    t.res.update(status="tested", b=b, t=tv, G=G)
    sp_ok = search[4][~np.isnan(search[4])]
    if len(sp_ok):
        t.res["pred_move_c"] = abs(b) * float(np.std(search[1])) * 100
        t.res["spread_c"] = float(np.median(sp_ok)) * 100
    return search, hold


def null_max(prepared: List[tuple], stat, reps: int, rng) -> np.ndarray:
    out = np.full(reps, -np.inf)
    for r in range(reps):
        for t, (search, _h) in prepared:
            y, x, z, g, _ = search
            _, tv, _ = cluster_t(y, rng.permutation(x), z, g)
            out[r] = max(out[r], stat(t, tv))
    return out


def holdout(t: Test, hold: tuple) -> None:
    y, x, z, g, _ = hold
    if len(y) < MIN_HOURS // 2 or len(np.unique(g)) < MIN_CLUSTERS // 2:
        t.res.update(holdout_t=None, holdout_pass=False)
        return
    _, tv, _ = cluster_t(y, x, z, g)
    t.res["holdout_t"] = tv
    if t.family == "F1":
        t.res["holdout_pass"] = tv * t.sign >= 2
    else:
        t.res["holdout_pass"] = abs(tv) >= 2 and np.sign(tv) == np.sign(t.res["t"])


def run_family(name: str, tests: List[Test], S, stat, reps: int, rng, lines: List[str]) -> List[Test]:
    prepared = []
    for t in tests:
        p = prepare(t, S)
        if p:
            prepared.append((t, p))
    tested = [t for t, _ in prepared]
    if not tested:
        lines.append(f"{name}: no testable tests")
        return tests
    if name.startswith("F3"):
        p = np.array([2 * stats.t.sf(abs(t.res["t"]), max(t.res["G"] - 1, 1)) for t in tested])
        order = np.argsort(p)
        m = len(p)
        passed = np.zeros(m, bool)
        below = np.where(p[order] <= 0.10 * (np.arange(1, m + 1) / m))[0]
        if len(below):
            passed[order[: below.max() + 1]] = True
        for t, pv, d in zip(tested, p, passed):
            t.res.update(p=pv, discovery=bool(d))
        survivors = [(t, h) for (t, (_s, h)), d in zip(prepared, passed) if d]
        ref = null_max(prepared, lambda t, tv: abs(tv), reps, rng)
        thr = None
    else:
        ref = null_max(prepared, stat, reps, rng)
        thr = float(np.percentile(ref, 95))
        survivors = [(t, h) for t, (_s, h) in prepared if stat(t, t.res["t"]) > thr]
    for t, h in survivors:
        holdout(t, h)

    vals = np.array([stat(t, t.res["t"]) for t in tested])
    lines.append(f"\n{'=' * 78}\n{name}\n{'=' * 78}")
    lines.append(f"  tests defined {len(tests)}, testable {len(tested)}")
    lines.append(f"  statistic distribution: min {vals.min():.2f}  25% {np.percentile(vals, 25):.2f}  "
                 f"median {np.median(vals):.2f}  75% {np.percentile(vals, 75):.2f}  max {vals.max():.2f}")
    lines.append(f"  naive |stat| >= 2: {int((np.abs(vals) >= 2).sum())} of {len(vals)} "
                 f"({(np.abs(vals) >= 2).mean():.1%})")
    if thr is not None:
        lines.append(f"  shuffled null ({reps}): best-noise median {np.median(ref):.2f}, 95th pct {thr:.2f} <- threshold")
    else:
        lines.append(f"  reference null ({reps}): max |t| median {np.median(ref):.2f}, 95th pct "
                     f"{np.percentile(ref, 95):.2f}; BH q=0.10 discoveries: {len(survivors)}")
        story = [t for t in tested if t.story]
        nost = [t for t in tested if not t.story]
        for lab, grp in (("story", story), ("no-story (placebo)", nost)):
            if grp:
                r = np.mean([abs(t.res["t"]) >= 2 for t in grp])
                lines.append(f"  |t| >= 2 among {lab:<19}: {r:.1%} of {len(grp)}")
    lines.append(f"  survivors -> holdout: {len(survivors)}")
    for t, _h in sorted(survivors, key=lambda th: -abs(th[0].res["t"])):
        ht = t.res.get("holdout_t")
        lines.append(f"    {t.pid:<14} {t.lead} -> {t.lag} {t.k}h  search t {t.res['t']:.2f}  holdout "
                     f"{('%.2f' % ht) if ht is not None else 'n/a'}  -> "
                     f"{'PASS' if t.res.get('holdout_pass') else 'fail'}"
                     + (f"  (pred {t.res['pred_move_c']:.2f}c vs spread {t.res['spread_c']:.1f}c)"
                        if 'pred_move_c' in t.res else ""))
    top = sorted(tested, key=lambda t: -stat(t, t.res["t"]))[:15]
    lines.append("  top 15 by statistic:")
    for t in top:
        lines.append(f"    {t.pid:<14} {t.lead:>22} -> {t.lag:<24} {t.k:>2}h  stat {stat(t, t.res['t']):6.2f}"
                     f"  hours {t.res['search_hours']:>6}  events {t.res['G']:>4}")
    return tests


# --------------------------------------------------------------------------- main

def sha(path: str) -> str:
    import hashlib
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def indexable_series(conn: sqlite3.Connection) -> List[str]:
    from pilot_test import parse_strike
    with open("report/series_passing.csv") as fh:
        sp = [r for r in csv.DictReader(fh) if r["category"] != "Sports"]
    out = []
    for r in sp:
        rows = conn.execute("SELECT strike_type, floor_strike, cap_strike, yes_sub_title FROM markets "
                            "WHERE series_ticker = ? AND volume >= 100", (r["series"],)).fetchall()
        if rows and sum(1 for x in rows if parse_strike(*x)) / len(rows) >= 0.7:
            out.append(r["series"])
    return sorted(out)


def build_tests(indexable: List[str]) -> Tuple[List[Test], List[Test], List[Test]]:
    with open("sign_map.csv") as fh:
        sm = [r for r in csv.DictReader(fh) if r["matching"] == "series"]
    with open("sign_map_ext.csv") as fh:
        ext = list(csv.DictReader(fh))
    story = {frozenset((r["leader"], r["laggard"])) for r in sm + ext}
    sg = lambda r: 1 if r["sign"] == "+" else -1
    f1 = [Test("F1", r["pair_id"], r["leader"], r["laggard"], k, sg(r), True)
          for r in sm + ext if r["pair_id"] not in V3_PAIRS for k in (1, 6)]
    f2 = [Test("F2", r["pair_id"] + "-rev", r["laggard"], r["leader"], k, sg(r), True)
          for r in sm + ext for k in (1, 6)]
    idx = set(indexable)
    f3 = []
    with open("report/testable_pairs.csv") as fh:
        for r in csv.DictReader(fh):
            a, b = r["series_a"], r["series_b"]
            if a in idx and b in idx:
                for lead, lag in ((a, b), (b, a)):
                    for k in (1, 3, 6, 24):
                        f3.append(Test("F3", f"{lead}>{lag}", lead, lag, k, 1,
                                       frozenset((a, b)) in story))
    return f1, f2, f3


def write_csv(path: str, tests: List[Test]) -> None:
    rows = [{"family": t.family, "pair": t.pid, "leader": t.lead, "laggard": t.lag, "horizon_h": t.k,
             "sign": t.sign, "story": t.story, **t.res} for t in tests]
    keys = sorted({k for r in rows for k in r})
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)


def selftest() -> int:
    """Two synthetic series where B follows A with a 3-hour drift, plus noise
    series. The signed family must find A->B; reversed and noise must not."""
    rng = np.random.default_rng(1)
    n = 6000
    S = {}
    a = Series(n)
    a.chg = rng.normal(0, 1, n)
    a.ev = np.arange(n) // 168
    b = Series(n)
    drift = np.convolve(a.chg, [0, 0.15, 0.15, 0.15], mode="full")[:n]
    b.chg = rng.normal(0, 1, n) + drift
    b.ev = np.arange(n) // 168
    b.spread = np.full(n, 0.04)
    S["A"], S["B"] = a, b
    for i in range(6):
        c = Series(n)
        c.chg = rng.normal(0, 1, n)
        c.ev = np.arange(n) // 168
        S[f"N{i}"] = c
    lines: List[str] = []
    f1 = [Test("F1", "AB", "A", "B", k, 1, True) for k in (1, 6)] + \
         [Test("F1", f"N{i}", f"N{i}", f"N{i + 1}", k, 1, False) for i in range(5) for k in (1, 6)]
    run_family("F1 selftest", f1, S, lambda t, tv: tv * t.sign, 100, np.random.default_rng(SEED), lines)
    f2 = [Test("F2", "BA", "B", "A", k, 1, True) for k in (1, 6)]
    run_family("F2 selftest", f2, S, lambda t, tv: abs(tv), 100, np.random.default_rng(SEED), lines)
    print("\n".join(lines))
    ab = [t for t in f1 if t.pid == "AB"]
    ok = all(t.res.get("holdout_pass") for t in ab) and not any(t.res.get("holdout_pass") for t in f2)
    print("\nSELFTEST", "PASSED" if ok else "FAILED")
    return 0 if ok else 1


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description="Overnight hourly suite (v5)")
    ap.add_argument("--db", default="kalshi.db")
    ap.add_argument("--out", default="report")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    for path, want in (("test_spec_v5_overnight.md", SPEC_SHA), ("sign_map.csv", SIGN_MAP_SHA),
                       ("sign_map_ext.csv", EXT_SHA)):
        got = sha(path)
        print(f"{path} sha256 {got[:12]}... {'matches' if got == want else 'DOES NOT MATCH'}")
        if got != want:
            return 1

    conn = sqlite3.connect(args.db)
    indexable = indexable_series(conn)
    f1, f2, f3 = build_tests(indexable)
    names = sorted({t.lead for t in f1 + f2 + f3} | {t.lag for t in f1 + f2 + f3})
    S, _h0 = load_series(conn, names)
    print(f"loaded {len(S)} series; tests defined: F1 {len(f1)}, F2 {len(f2)}, F3 {len(f3)}")

    rng = np.random.default_rng(SEED)
    lines = [f"Overnight hourly suite v5 - {len(indexable)} indexable series"]
    run_family("F1 SIGNED PAIRS (evidence)", f1, S, lambda t, tv: tv * t.sign, 500, rng, lines)
    run_family("F2 REVERSED PAIRS (placebo)", f2, S, lambda t, tv: abs(tv), 500, rng, lines)
    run_family("F3 BROAD SWEEP (exploratory, two-sided)", f3, S, lambda t, tv: abs(tv), 100, rng, lines)
    n_tested = sum(1 for t in f1 + f2 + f3 if t.res.get("status") == "tested")
    lines.append(f"\nVariants tested in v5: {n_tested}; tally {PRIOR_TALLY} + {n_tested} = "
                 f"{PRIOR_TALLY + n_tested}. No fees, no P&L.")
    report = "\n".join(lines)
    print(report)
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "v5_overnight_report.txt"), "w") as fh:
        fh.write(report + "\n")
    write_csv(os.path.join(args.out, "v5_overnight_tests.csv"), f1 + f2 + f3)
    return 0


if __name__ == "__main__":
    sys.exit(main())
