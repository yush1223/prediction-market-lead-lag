# Lead-lag test spec v2 (frozen before the remaining candles were downloaded)

Supersedes pilot_spec.md (v1, sha256 e8c843df...). v1 ran on E01 and E23 and
was uninformative: its implied-median index failed on single-strike and
incomplete ladders (see report/pilot_v1_results.txt). v1's 2 tests count in
the tally. E01 and E23 are included again below, already seen once under v1.

Pairs: every row of sign_map.csv (sha256 9a551c0a...fe5a2) with
matching == "series" (44 pairs). Direction and sign from the sign map only.

Series index v2, per series-day, front event = nearest settlement at least
1 day after the day's close. Quotes: mid = (bid+ask)/2, skip spread > 0.20.
- Threshold events (only "above"/"below" strikes): on day t-1 pick the
  strike whose P(underlying > strike) is nearest 0.5; change_t = that same
  strike's P(> strike) on t minus on t-1 ("below" strikes: P = 1 - mid).
- Range events (any "between" strikes): expected value over the ranges
  quoted on both t-1 and t, using range midpoints (tails at their edge),
  probabilities renormalized over that common set; change_t = E_t - E_{t-1}.
- Changes only within one event (no roll jumps). Units differ by series;
  the test statistic is unit-free.

Regression per pair, on days where all three changes exist:
  dLag[t+1] = a + b * dLead[t] + c * dLag[t] + e
SEs clustered by laggard event (event of dLag[t+1]). Pairs with fewer than
30 joint days or fewer than 8 clusters are reported as untestable.

Time split per pair: joint days sorted by date; first 65% = search,
last 35% = holdout.

Search statistic: s = t(b) * sign_map_sign (one-sided).
Null: 500 replications; in each, within every pair's search period, permute
dLead dates at random (keeping dLag pairs intact), refit, record max s
across all pairs. Threshold = 95th percentile of that max (seed 20261110).
Survivors: pairs with search s above the threshold.
Holdout: each survivor refit once on its holdout days. Pass = s >= 2.

Report: all pairs' search s (full distribution), the null threshold,
survivors, holdout results, days and clusters per pair, continuity (share
of non-zero daily changes per series), predicted laggard move vs typical
spread. Variants tested: 44 (v2) + 2 (v1 pilot) = 46. No fee model yet
(the user deferred fees); no P&L is claimed.
