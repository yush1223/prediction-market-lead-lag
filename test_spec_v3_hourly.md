# Lead-lag test spec v3: hourly (frozen before any hourly data was downloaded)

Context: v2 (daily, test_spec_v2.md sha256 f04c3c53...) found no pair beating
its shuffled null (report/test_v2_output.txt). v3 asks whether a lag exists
inside the day. Running tally before v3: 37 tests (35 v2 + 2 v1).

Pairs, chosen before any hourly data by one rule: both sides price a
continuously-updating quantity (market price or poll average), so the leader
does not settle at the moment its news arrives; one or two per group, highest
confidence available. Signs from sign_map.csv (sha256 9a551c0a...fe5a2).
- E01 KXWTIW     -> KXAAAGASW     +
- E06 KXWTIW     -> KXBRENTW      +
- E23 KXTNOTEW   -> KXFRM         +
- E40 KXAPRPOTUS -> KX538APPROVE  +
- E38 KXGOLDW    -> KXSILVERW     +
Caveat recorded up front: the daily v2 search results for these pairs were
already seen (E01 0.82, E06 0.06, E40 -0.27, E38 -1.03, E23 untestable).

Data: hourly candles (period_interval 60), same markets as v2 (volume >= 100).
Index: v2 index unchanged, computed per hour instead of per day (front event =
nearest settlement at least 1 day after the hour's close; threshold events:
change in P(> strike) of the strike nearest 0.5 at h-1; range events: change
in expected value over ranges quoted at both h-1 and h; changes only within
one event).

Regression per pair and horizon k in {1, 6} hours (10 tests):
  sum_{j=1..k} dLag[h+j] = a + b * dLead[h] + c * dLag[h] + e
All k future hours must exist. SEs clustered by laggard event of dLag[h+1].
Untestable if fewer than 100 joint hours or fewer than 8 clusters.

Split per test: joint hours by time, first 65% search, last 35% holdout.
Search statistic s = t(b) * sign. Null: 500 replications permuting dLead
within each test's search period, record max s over all testable tests;
threshold = 95th percentile (seed 20261110). Survivors (s > threshold) are
refit once on holdout; pass = s >= 2.

Report all 10 search s values, the null threshold, survivors and holdout
results, hours and clusters per test, and the predicted laggard move vs
typical spread. Tally after v3: 37 + (testable v3 tests). No fees, no P&L.
