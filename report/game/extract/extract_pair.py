"""Real lead-lag moments for one series pair from the frozen sign map.

usage: python3 extract_pair.py OUT.json LEADER LAGGARD SIGN [MOVE]
Run from the project folder (reads kalshi.db read-only).

A moment: some leader market ("above X" type, the strike nearest 50c an hour earlier)
moves at least MOVE in mid price within one hour. The laggard is the "above Y" market
nearest 50c at that hour, quoted (spread <= 20c) at that hour and 6 hours later, in an
event that is still open 6 hours later. "With the news" = the sign-map direction:
buy laggard YES if SIGN * leader move > 0, else buy NO. One moment per leader event per day,
the biggest move. Same taker P&L as the sports moments: buy at ask, sell at bid 6h later.
"""
import json, random, sqlite3, sys
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

OUT, LEAD, LAG, SIGN = sys.argv[1], sys.argv[2], sys.argv[3], (1 if sys.argv[4] == "+" else -1)
MOVE = float(sys.argv[5]) if len(sys.argv) > 5 else 0.15
H = 6
db = sqlite3.connect("file:kalshi.db?mode=ro", uri=True)
ts = lambda v: int(datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()) if v else None


def et_label(h):
    d = datetime.fromtimestamp(h * 3600, timezone.utc).astimezone(ZoneInfo("America/New_York"))
    return d.strftime("%a, %b ") + str(d.day) + d.strftime(", %Y · ") + (d.strftime("%I").lstrip("0") or "12") + d.strftime(" %p ET")


def load(series):
    meta = {}
    for t, ev, title, sub, fl, close in db.execute(
            "SELECT m.ticker, m.event_ticker, e.title, m.yes_sub_title, m.floor_strike, m.close_time FROM markets m "
            "JOIN events e ON e.event_ticker = m.event_ticker WHERE m.series_ticker = ? AND m.volume >= 100 AND m.strike_type = 'greater'", (series,)):
        meta[t] = dict(ev=ev, title=title, sub=sub, strike=fl, close=ts(close))
    q = defaultdict(dict)
    if meta:
        ph = ",".join("?" * len(meta))
        for t, e, b, a in db.execute(f"SELECT ticker, end_ts, yes_bid_close, yes_ask_close FROM candles_1h WHERE ticker IN ({ph})", list(meta)):
            if b is not None and a is not None and a - b <= 0.2:
                q[t][e // 3600] = (b, a)
    by_ev = defaultdict(list)
    for t, m in meta.items():
        by_ev[m["ev"]].append(t)
    return meta, q, by_ev


lm, lq, lev = load(LEAD)
gm, gq, gev = load(LAG)
mid = lambda x: (x[0] + x[1]) / 2

# laggard candidates by hour: markets quoted at h and h+H, event open past h+H
lag_hours = defaultdict(list)
for t, qs in gq.items():
    for h in qs:
        if h + H in qs and (gm[t]["close"] or 0) > (h + H) * 3600:
            lag_hours[h].append(t)

best = {}
for ev, tickers in lev.items():
    hours = sorted({h for t in tickers for h in lq[t]})
    for h in hours:
        # the leader market nearest 50c an hour before, quoted at both hours
        c = [(abs(mid(lq[t][h - 1]) - .5), t) for t in tickers if h - 1 in lq[t] and h in lq[t]]
        if not c:
            continue
        t = min(c)[1]
        d = mid(lq[t][h]) - mid(lq[t][h - 1])
        if abs(d) < MOVE or not lag_hours.get(h):
            continue
        g = min(lag_hours[h], key=lambda u: abs(mid(gq[u][h]) - .5))
        if not .05 <= mid(gq[g][h]) <= .95:
            continue
        key = (ev, h // 24)
        if key not in best or abs(d) > abs(best[key][1]):
            best[key] = (h, d, t, g)


def path(qd, lo, hi):
    out, last = [], None
    for k in range(lo, hi + 1):
        v = qd.get(k, last); last = v
        out.append(None if v is None else [round(v[0], 2), round(v[1], 2)])
    return out


eps = []
for (ev, _), (h, d, t, g) in best.items():
    lp, sp = path(lq[t], h - 5, h), path(gq[g], h - 5, h + H)
    if any(x is None for x in sp) or any(x is None for x in lp):
        continue
    dirn = SIGN * (1 if d > 0 else -1)
    q0, q6 = sp[5], sp[5 + H]
    eps.append({"lead_title": lm[t]["title"], "lead_sub": lm[t]["sub"], "lead_strike": lm[t]["strike"],
                "lag_title": gm[g]["title"], "lag_sub": gm[g]["sub"], "lag_strike": gm[g]["strike"],
                "lag_close": datetime.fromtimestamp(gm[g]["close"], timezone.utc).strftime("%Y-%m-%d"),
                "date": datetime.fromtimestamp(h * 3600, timezone.utc).strftime("%Y-%m-%d"), "when": et_label(h),
                "swing": round(d, 2), "up": dirn > 0, "lead_path": [round(mid(x), 3) for x in lp], "season_path": sp,
                "pnl_with": round((q6[0] - q0[1]) if dirn > 0 else (q0[0] - q6[1]), 2),
                "mid_move": round((mid(q6) - mid(q0)) * dirn, 3), "spread": round(q0[1] - q0[0], 2)})

eps.sort(key=lambda e: e["date"])
n = len(eps)
if not n:
    print("no moments"); json.dump({"summary": {"n": 0}, "episodes": []}, open(OUT, "w")); sys.exit()
summary = {"n": n, "avg_pnl_with": round(sum(e["pnl_with"] for e in eps) / n, 4), "avg_mid_move": round(sum(e["mid_move"] for e in eps) / n, 4),
           "share_profitable": round(sum(e["pnl_with"] > 0 for e in eps) / n, 3), "avg_spread": round(sum(e["spread"] for e in eps) / n, 4)}
json.dump({"summary": summary, "episodes": eps}, open(OUT, "w"))
print(LEAD, "->", LAG, summary)
e = eps[len(eps) // 2]
print("example:", {k: e[k] for k in ("lead_title", "lead_sub", "lag_title", "lag_sub", "when", "swing", "up", "pnl_with")})
