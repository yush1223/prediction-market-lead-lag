# Spec v5: overnight hourly suite (frozen before the new hourly data)

Tally before v5: 52 tests. Inputs, all frozen and hashed beforehand:
sign_map.csv (9a551c0a...), sign_map_ext.csv (fe37c629...).
Data: hourly candles (period 60) for every "indexable" non-sports series
passing census (>= 70% of its volume>=100 markets parse as threshold/range
strikes; 71 series). Index: v2 rules computed per hour, unchanged.
Regression: sum_{j=1..k} dLag[h+j] = a + b*dLead[h] + c*dLag[h] + e,
SEs clustered by laggard event of dLag[h+1]. Untestable: < 100 joint hours
in the search period or < 8 clusters. Split: first 65% of joint hours search,
last 35% holdout. Each family has its own null; holdout is used once.

FAMILY 1 - signed (evidence). Pairs: series-matched sign_map.csv rows not
tested in v3 (all except E01 E06 E23 E38 E40) plus all sign_map_ext.csv
rows. Horizons {1, 6}. Statistic s = t * sign. Null: 500 permutations of dLead
within each test's search period, max s over the family; threshold = 95th
pct (seed 20261110). Survivors refit once on holdout; pass = s >= 2.

FAMILY 2 - reverse placebo. Every series-matched pair in sign_map.csv and
sign_map_ext.csv (incl. the five v3 pairs) with leader and laggard swapped.
Horizons {1, 6}. Two-sided |t|. Null: 500 permutations, max |t|, 95th pct.
Survivors to holdout once; pass = |t| >= 2 with the search sign. Expectation
if lead-lag is real and one-way: far fewer reverse passes than family 1.

FAMILY 3 - broad sweep (exploratory). Every census-testable pair among the
indexable series, both directions, horizons {1, 3, 6, 24}. Two-sided p from
the clustered t with G-1 df. Benjamini-Hochberg at q = 0.10 over the family
= discoveries; each discovery refit once on holdout; confirm = same sign and
|t| >= 2. Placebo comparison: share of tests with |t| >= 2 (search) among
"story" pairs (in either sign map, either direction) vs "no-story" pairs.
Reference null: 100 permutations, max |t|. Discoveries are leads, not results.

Sports (family 4) gets its own spec after this run (team matching).
Report every family's full distribution, thresholds, survivors, holdout
outcomes, predicted move vs spread for threshold laggards, and the tally.
No fees, no P&L.
