import sqlite3, sys, json, random
from collections import defaultdict
from datetime import datetime, timezone
sys.path.insert(0, ".")
from pilot_test import _ts, parse_strike
db = sqlite3.connect("file:kalshi.db?mode=ro", uri=True)
H = 6

from zoneinfo import ZoneInfo
def et_label(h):
    d = datetime.fromtimestamp(h * 3600, timezone.utc).astimezone(ZoneInfo("America/New_York"))
    return d.strftime("%a, %b ") + str(d.day) + d.strftime(", %Y \u00b7 ") + (d.strftime("%I").lstrip("0") or "12") + d.strftime(" %p ET")

def load(series):
    meta = {}
    for t, ev, st, fl, cp, sub, c in db.execute("SELECT ticker,event_ticker,strike_type,floor_strike,cap_strike,yes_sub_title,close_time FROM markets WHERE series_ticker=? AND volume>=100", (series,)):
        k = parse_strike(st, fl, cp, sub)
        if k: meta[t] = (ev, k, _ts(c))
    q = defaultdict(dict)
    ph = ",".join("?" * len(meta))
    for t, e, b, a in db.execute(f"SELECT ticker,end_ts,yes_bid_close,yes_ask_close FROM candles_1h WHERE ticker IN ({ph})", list(meta)):
        if b is not None and a is not None: q[t][e // 3600] = (b, a)
    return meta, q

wm, wq = load("KXWTIW"); gm, gq = load("KXAAAGASW")
close_ev = defaultdict(int)
for t, (ev, k, c) in wm.items(): close_ev[ev] = max(close_ev[ev], c or 0)
by_ev = defaultdict(list)
for t, (ev, k, c) in wm.items(): by_ev[ev].append(t)

def ev_level(ev, h, common=None):
    w, x = [], []
    for t in (common or by_ev[ev]):
        v = wq[t].get(h)
        if not v or v[1] - v[0] > 0.2: continue
        kind, lo, hi = wm[t][1]
        w.append((v[0] + v[1]) / 2); x.append(hi if kind == "lt" else lo if kind == "gt" else (lo + hi) / 2)
    return (sum(a * b for a, b in zip(w, x)) / sum(w), len(w)) if w and sum(w) > 0 else (None, 0)

hours = sorted({h for t in wq for h in wq[t]})
moves = []
for h in hours:
    live = [ev for ev in by_ev if close_ev[ev] - h * 3600 >= 86400 and any(h in wq[t] for t in by_ev[ev])]
    if not live: continue
    ev = min(live, key=lambda e: close_ev[e])
    common = [t for t in by_ev[ev] if h in wq[t] and (h - 1) in wq[t]]
    if len(common) < 3: continue
    a, _ = ev_level(ev, h - 1, common); b, _ = ev_level(ev, h, common); lvl, n = ev_level(ev, h)
    if a is None or b is None or lvl is None: continue
    moves.append((h, b - a, lvl, ev))
big = sorted(abs(m[1]) for m in moves)
cut = big[int(len(big) * 0.99)]
print("oil hourly moves", len(moves), "99th pct |$|", round(cut, 2))

gas_ev = defaultdict(list)
for t, (ev, k, c) in gm.items(): gas_ev[ev].append(t)
eps, used_days = [], set()
for h, d, lvl, wev in sorted(moves, key=lambda m: -abs(m[1])):
    if abs(d) < cut: break
    day = h // 24
    if day in used_days: continue
    cands = []
    for t, (ev, k, c) in gm.items():
        if k[0] != "gt" or (c or 0) - h * 3600 < 86400: continue
        q0, q1 = gq[t].get(h), gq[t].get(h + H)
        if not q0 or not q1 or q0[1] - q0[0] > 0.2 or q1[1] - q1[0] > 0.2: continue
        cands.append((abs((q0[0] + q0[1]) / 2 - 0.5), t))
    if not cands: continue
    t = min(cands)[1]
    path, last = [], None
    for kk in range(h - 5, h + H + 1):
        v = gq[t].get(kk, last); last = v
        path.append(None if v is None else [round(v[0], 2), round(v[1], 2)])
    if any(p is None for p in path[5:]): continue
    used_days.add(day)
    dirn = 1 if d > 0 else -1
    q0, q6 = path[5], path[5 + H]
    opath = [ev_level(wev, k)[0] for k in range(h - 5, h + 1)]
    opath = [None if v is None else round(v, 2) for v in opath]
    eps.append({"kind": "oil", "when": et_label(h), "oil_path": opath, "oil_from": round(lvl - d, 2), "oil_to": round(lvl, 2), "swing": round(d, 2),
                "strike": gm[t][1][1], "settle": datetime.fromtimestamp(gm[t][2], timezone.utc).strftime("%b %-d, %Y"),
                "date": datetime.fromtimestamp(h * 3600, timezone.utc).strftime("%Y-%m-%d"),
                "season_path": path, "pnl_with": round((q6[0] - q0[1]) if dirn > 0 else (q0[0] - q6[1]), 2),
                "mid_move": round(((q6[0] + q6[1]) - (q0[0] + q0[1])) / 2 * dirn, 3),
                "spread": round(q0[1] - q0[0], 2)})
n = len(eps)
summ = {"n": n, "avg_pnl_with": round(sum(e["pnl_with"] for e in eps) / n, 4), "avg_mid_move": round(sum(e["mid_move"] for e in eps) / n, 4),
        "share_profitable": round(sum(e["pnl_with"] > 0 for e in eps) / n, 3), "avg_spread": round(sum(e["spread"] for e in eps) / n, 4)}

def complete(e):
    sp = e["season_path"]
    if any(q is None for q in sp) or sp[0][1] - sp[0][0] > 0.2: return False
    lead = e.get("game_path") or e.get("oil_path")
    return all(x is not None for x in lead[:6])
pool = [e for e in eps if complete(e)]
print("complete moments:", len(pool), "of", len(eps))
random.seed(20261115)
sample = sorted(random.sample(pool, min(16, len(pool))), key=lambda e: e["date"])
json.dump({"summary": summ, "episodes": sample}, open(sys.argv[1], "w"))
print(summ); print({k: sample[0][k] for k in ("oil_from", "oil_to", "strike", "settle", "date", "pnl_with", "mid_move", "spread")})
