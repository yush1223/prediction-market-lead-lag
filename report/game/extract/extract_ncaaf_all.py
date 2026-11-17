import sqlite3, sys, json, random, re
from collections import defaultdict
from datetime import datetime, timezone
sys.path.insert(0, ".")
from sports_test import entity_of
from pilot_test import _ts
db = sqlite3.connect("file:kalshi.db?mode=ro", uri=True)
GAME, SEASON, MOVE, H = "KXNCAAFGAME", "KXNCAAFWINS", 0.20, 6
from zoneinfo import ZoneInfo
def et_label(h):
    d = datetime.fromtimestamp(h * 3600, timezone.utc).astimezone(ZoneInfo("America/New_York"))
    return d.strftime("%a, %b ") + str(d.day) + d.strftime(", %Y \u00b7 ") + (d.strftime("%I").lstrip("0") or "12") + d.strftime(" %p ET")


def quotes(series):
    meta = {t: (entity_of(series, t, ev), ev, _ts(c), sub, fl) for t, ev, c, sub, fl in db.execute(
        "SELECT ticker, event_ticker, close_time, yes_sub_title, floor_strike FROM markets WHERE series_ticker=? AND volume>=100", (series,))}
    q = defaultdict(dict)
    ph = ",".join("?" * len(meta))
    for t, e, b, a in db.execute(f"SELECT ticker,end_ts,yes_bid_close,yes_ask_close FROM candles_1h WHERE ticker IN ({ph})", list(meta)):
        if b is not None and a is not None:
            q[t][e // 3600] = (b, a)
    return meta, q

gm, gq = quotes(GAME); sm, sq = quotes(SEASON)
titles = dict(db.execute("SELECT event_ticker, title FROM events WHERE series_ticker IN (?,?)", (GAME, SEASON)))
season_by_team = defaultdict(list)
for t, (ent, ev, close, sub, fl) in sm.items():
    season_by_team[ent].append(t)

eps = []
for t, (ent, ev, close, sub, fl) in gm.items():
    if ent not in season_by_team: continue
    best = None
    for h, (b, a) in gq[t].items():
        p = gq[t].get(h - 1)
        if not p or a - b > 0.2 or p[1] - p[0] > 0.2 or close - (h * 3600) < 3600: continue
        d = (b + a) / 2 - (p[0] + p[1]) / 2
        if abs(d) < MOVE: continue
        # season strike nearest 50c at hour h, quoted at h and h+H with spread <= 0.2
        cands = []
        for st in season_by_team[ent]:
            q0, q1 = sq[st].get(h), sq[st].get(h + H)
            if not q0 or not q1 or q0[1] - q0[0] > 0.2 or q1[1] - q1[0] > 0.2: continue
            cands.append((abs((q0[0] + q0[1]) / 2 - 0.5), st))
        if not cands: continue
        st = min(cands)[1]
        if best is None or abs(d) > abs(best[1]): best = (h, d, st)
    if not best: continue
    h, d, st = best
    def path(qd, lo, hi):
        out, last = [], None
        for k in range(lo, hi + 1):
            v = qd.get(k, last); last = v
            out.append(None if v is None else [round(v[0], 2), round(v[1], 2)])
        return out
    g = path(gq[t], h - 5, h + H); s = path(sq[st], h - 5, h + H)
    if any(x is None for x in s[5:]) or g[5] is None: continue
    dirn = 1 if d > 0 else -1
    q0, q6 = s[5], s[5 + H]
    pnl_with = (q6[0] - q0[1]) if dirn > 0 else (q0[0] - q6[1])          # taker round trip in swing direction
    mid_move = ((q6[0] + q6[1]) - (q0[0] + q0[1])) / 2 * dirn
    eps.append({"team": ent, "game": titles.get(ev, ev), "season": titles.get(sm[st][1], sm[st][1]),
                "strike": sm[st][3], "date": datetime.fromtimestamp(h * 3600, timezone.utc).strftime("%Y-%m-%d"),
                "hour": datetime.fromtimestamp(h * 3600, timezone.utc).strftime("%H:00 UTC"),
                "swing": round(d, 2), "when": et_label(h), "game_path": g, "season_path": s,
                "pnl_with": round(pnl_with, 2), "mid_move": round(mid_move, 3)})

eps.sort(key=lambda e: e["date"])
n = len(eps)
avg = lambda k: sum(e[k] for e in eps) / n
summary = {"n": n, "avg_pnl_with": round(avg("pnl_with"), 4), "avg_mid_move": round(avg("mid_move"), 4),
           "share_profitable": round(sum(e["pnl_with"] > 0 for e in eps) / n, 3),
           "avg_spread": round(sum(e["season_path"][5][1] - e["season_path"][5][0] for e in eps) / n, 4)}

def complete(e):
    sp = e["season_path"]
    if any(q is None for q in sp) or sp[0][1] - sp[0][0] > 0.2: return False
    lead = e.get("game_path") or e.get("oil_path")
    return all(x is not None for x in lead[:6])
pool = [e for e in eps if complete(e)]
print("complete moments:", len(pool), "of", len(eps))
random.seed(20261115)
sample = sorted(random.sample(pool, len(pool)), key=lambda e: e["date"])
json.dump({"summary": summary, "episodes": sample}, open(sys.argv[1], "w"))
print(summary)
print("example:", {k: sample[0][k] for k in ("team", "game", "season", "strike", "date", "swing", "pnl_with", "mid_move")})
