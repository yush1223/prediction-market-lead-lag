# Beat the Spread — game source

The published game is `report/beat_the_spread.html` (one self-contained file).
Everything needed to rebuild or change it lives here.

| Path | What it is |
|---|---|
| `game_template.html` | The game: HTML, CSS and JS, with a `__DATA__` placeholder for the moments |
| `assemble.py` | Fills the placeholder from `data/` and writes `report/beat_the_spread.html` |
| `data/*.json` | Real moments per kind of round, plus a `summary` over every moment found |
| `extract/` | Scripts that pulled those moments out of `kalshi.db` (read-only) |
| `test/` | Stub DOM (`harness3.js`) and a 40-game simulated playtest (`playtest.js`) |

## Rebuild

From the project folder:

```bash
python3 report/game/assemble.py
```

## Playtest (macOS, no Node needed)

```bash
cd report/game/test && cat harness3.js build/check_sync.js playtest.js > build/pt_run.js && osascript -l JavaScript -e "ObjC.import('Foundation'); (function(){ var src=\$.NSString.stringWithContentsOfFileEncodingError('$PWD/build/pt_run.js', 4, null).js; return eval(src) })()"
```

It should print `40 games ok ... problems: none`.

## Data

| File | Kind | Fast market → slow market | Extracted with |
|---|---|---|---|
| `episodes.json` | ncaaf | KXNCAAFGAME → KXNCAAFWINS | `extract/extract_ncaaf_all.py` |
| `nfl.json` | nfl | KXNFLGAME → KXNFLWINS | `extract/extract_sport.py data/nfl.json KXNFLGAME KXNFLWINS 999 0` |
| `ncaamb.json` | cbb | KXNCAAMBGAME → KXMARMAD | `extract/extract_sport.py data/ncaamb.json KXNCAAMBGAME KXMARMAD 999 0.05` |
| `golf.json` | golf | KXPGAR1LEAD → KXPGATOUR | `extract/extract_sport.py data/golf.json KXPGAR1LEAD KXPGATOUR 999 0.03` |
| `oil.json` | oil | KXWTIW → KXAAAGASW | `extract/extract_oil_all.py` |
| `hormuz.json` | hormuz | KXHORMUZWEEKLY → KXBRENTW (sign −) | `extract/extract_pair.py data/hormuz.json KXHORMUZWEEKLY KXBRENTW -` |
| `gold.json` | gold | KXGOLDW → KXSILVERW | `extract/extract_pair.py data/gold.json KXGOLDW KXSILVERW +` |
| `cpi.json` | cpi | KXCPI → KXFED | `extract/extract_pair.py data/cpi.json KXCPI KXFED +` |

Run extract scripts from the project folder (they read `kalshi.db` and import `sports_test.py` / `pilot_test.py`).
All pairs come from the frozen sign maps (`sign_map.csv`, `sign_map_ext.csv`).

Rules baked into the game:
- P&L is a taker round trip: buy at the ask, sell at the bid 6 hours later, 100 shares. No fees.
- Quotes with a spread over 20¢ are treated as no quote (last good quote carries forward), as in the research.
- The game only deals moments where the slow market moves at least 2¢ during the 6 hours after the trade;
  the "What happened?" averages use every moment (`summary` in each data file).
- The chart's price scale is fixed per round and centred on the price at "now", so it cannot reveal direction.
