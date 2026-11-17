"""Build report/beat_the_spread.html from game_template.html and the extracted moments in data/.

Run from the project folder:  python3 report/game/assemble.py
Also writes test copies to report/game/test/build/ (one page per kind of round that opens
straight into a round, for headless screenshots, plus check_sync.js for the stubbed playtest).
"""
import json, os, re

G = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(G, "test", "build")
os.makedirs(OUT, exist_ok=True)
load = lambda n: json.load(open(os.path.join(G, "data", n + ".json")))
data = {"sports": load("episodes"), "nfl": load("nfl"), "cbb": load("ncaamb"), "oil": load("oil"),
        "golf": load("golf"), "hormuz": load("hormuz"), "gold": load("gold"), "cpi": load("cpi")}
html = open(os.path.join(G, "game_template.html")).read().replace("__DATA__", json.dumps(data, separators=(",", ":")))
open("report/beat_the_spread.html", "w").write(html)
os.makedirs("docs", exist_ok=True)
open("docs/index.html", "w").write(html)  # served by GitHub Pages

skel = lambda body: ('<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" '
                     'content="width=device-width,initial-scale=1,viewport-fit=cover"></head><body>' + body + '</body></html>')
for name, make, pool in (("k_ncaaf", "ncaaf", "sports"), ("k_nfl", "nfl", "nfl"), ("k_cbb", "cbb", "cbb"), ("k_oil", "oil", "oil"),
                         ("k_golf", "golf", "golf"), ("k_hormuz", "hormuz", "hormuz"), ("k_gold", "gold", "gold"), ("k_cpi", "cpi", "cpi")):
    js = f"setTimeout(()=>{{newGame(); G.rounds[0]={make}(DATA.{pool}.episodes[3]); startRound();}},200);"
    open(os.path.join(OUT, name + ".html"), "w").write(skel(html.replace("\ntitle();\n", "\ntitle();\n" + js + "\n")))

s = html.index("<script>") + 8
e = html.index("</script>", s)
js = html[s:e]
open(os.path.join(OUT, "check.js"), "w").write(js)
t = js.replace("async function choose", "function choose")
t = re.sub(r"await wait\([^)]*\);?", "", t).replace("title();", "")
open(os.path.join(OUT, "check_sync.js"), "w").write(t)
print("built", len(html), "bytes; pools:", {k: len(v["episodes"]) for k, v in data.items()})
