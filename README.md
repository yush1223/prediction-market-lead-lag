# Does news travel slowly between prediction markets?

When news moves one prediction market, do related markets lag behind long enough for a trader to profit?
I tested this on Kalshi's public market data, across 51 economically linked market pairs and 6,909 statistical tests.

**Short answer:** the lags exist, but they are too small to trade. The strongest one, from college football game
markets to season-win markets, recovers about 60% of the bid-ask spread. Testable markets turn out to be
efficient markets.

- **Play the game:** [Beat the Spread](https://yush1223.github.io/prediction-market-lead-lag/), a two-minute game that replays real moments from this data
- **Read the report:** [Kalshi_lead_lag_research_report.pdf](report/Kalshi_lead_lag_research_report.pdf)

## What I built

- A data pipeline that pulled Kalshi's public API into a local SQLite database (about 3 GB): 968,000 markets,
  17.3 million hourly prices, 3.66 million minute-level prices and 616,702 individual trades.
- A testing framework built to resist false discoveries:
  - The expected direction for every pair was written down and SHA-256 hashed before any price data existed
    (`sign_map.csv`, `sign_map_ext.csv`).
  - Every test spec (`pilot_spec.md`, `test_spec_v2.md` … `test_spec_v7_trades.md`) was hashed before its data was
    downloaded. The test scripts refuse to run if a spec or sign map has changed.
  - Each search was rerun 500 times on date-shuffled data to measure how good pure noise looks, and survivors were
    checked once on a held-out final 35% of the data.
- A browser game that turns the result into something you can feel: you see the news, bet on the slow market,
  and watch whether it catches up by more than the spread.

## Results

| Market pair | Does the slow market lag? | Size of the catch-up | Spread | Profitable? |
|---|---|---|---|---|
| Economic markets, daily (oil, gas, inflation, jobs, rates) | No | 0.12¢ at best | about 4¢ | No |
| Oil → gas prices, hourly | Yes, but in both directions | about 1.6¢ | about 5¢ | No |
| Gas trades right after big oil moves | No | +0.14¢ per contract, not significant | — | No |
| College football: game → season wins | Yes, one way | about 2.9¢ after the biggest swings | about 5¢ | No |
| NFL: game → season wins | Yes, one way | about 0.6¢ | about 2¢ | No |
| College basketball: game → title | Yes, one way | about 0.1¢ | about 2¢ | No |

## Repository map

| Path | What it is |
|---|---|
| `kalshi.py` | API client: token-bucket rate limiting, cursor pagination, backoff on 429/5xx |
| `pull_universe.py`, `pull_candles.py`, `pull_trades.py` | Download markets, price candles and trades into SQLite |
| `census.py` | Finds which series overlap in time enough to be tested |
| `sign_map.csv`, `sign_map_ext.csv` (+ `.sha256`) | Expected direction for each pair, frozen before any data |
| `pilot_spec.md`, `test_spec_v*.md` (+ `.sha256`) | The frozen spec for each test |
| `pilot_test.py`, `lead_lag_test.py`, `hourly_test.py`, `minute_test.py`, `hourly_suite.py`, `sports_test.py`, `trades_check.py` | The tests, one per spec |
| `overnight.sh` | Runs the long hourly suite |
| `smoke_test.py` | Offline test of the client and pull scripts against a fake exchange |
| `report/` | Result files for every test, plus the PDF report |
| `report/game/` | Source for the game: template, data extracts and a simulated playtest |
| `docs/index.html` | The built game, served by GitHub Pages |

## Reproducing

```bash
pip install -r requirements.txt
python3 smoke_test.py          # offline check, no network
python3 pull_universe.py       # builds kalshi.db's market list from the public API (about 2 hours)
python3 census.py
```

Then run the pull and test scripts in spec order (v1 to v7). Each test checks its spec's hash before running.
The databases are not in the repository because of their size. All data comes from Kalshi's public API,
which needs no key.

Not affiliated with Kalshi. Prices only, no fees modelled; see the report's limitations section.
