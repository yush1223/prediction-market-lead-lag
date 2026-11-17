# Spec v4: minute-level timing profile for E01 (frozen before any minute data)

Context: v3 hourly (test_spec_v3_hourly.md, sha256 16f4d59b...) found E01
KXWTIW -> KXAAAGASW drifting over 6 hours (search s 3.26, holdout s 3.58),
with a predicted move (~1.6c) below the gas spread (~5c). Tally before v4: 45.

Purpose: DESCRIPTIVE. Locate when, within 6 hours, the gas market catches up
to an oil move. v4 uses the same calendar period as v3's holdout, so it
cannot confirm v3; confirmation needs data after 2026-11-10. No result here
is claimed as an independent test, though its horizons are counted.

Pair: E01 only, sign + (sign_map.csv sha256 9a551c0a...fe5a2).
Data: 1-minute candles for KXWTIW and KXAAAGASW markets with volume >= 100.
Candles are emitted only when something changes, so each market's last
quote is carried forward minute by minute until its next candle or close.
Bars: 5 minutes. At each bar end, the v2 index (unchanged rules: front event
nearest settlement at least 1 day away; threshold = change of the strike
nearest 0.5 at the previous bar; range = expected value over ranges quoted
at both bars; changes only within one event; skip spread > 0.20).

Regression per horizon k in {1, 3, 6, 12, 24, 48, 72} bars (5 min .. 6 h):
  sum_{j=1..k} dLag[t+j] = a + b * dLead[t] + c * dLag[t] + e
on all joint bars; SEs clustered by laggard event of dLag[t+1].
Report b, clustered t, and the cumulative predicted move (b x 1 sd of
dLead, in cents) at each horizon: the timing profile.
Reference null: 500 permutations of dLead (seed 20261110), max t across the
7 horizons, 95th percentile reported beside the profile.
Variants counted: 7 (exploratory). Tally after v4: 52. No fees, no P&L.
