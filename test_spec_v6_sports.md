# Spec v6: sports game -> season lead-lag, matched by team/player (frozen
# before any sports price data)

Pairs: every sports row of sign_map.csv (sha256 9a551c0a...) that passed
census: S01 KXNFLGAME->KXNFLWINS, S05 KXNCAAFGAME->KXNCAAFWINS,
S06 KXNCAAFGAME->KXNCAAFTOPAPRANK, S07 KXNCAAMBGAME->KXMARMAD,
S08 KXNCAAWBGAME->KXWMARMAD, S09 KXPGAR1LEAD->KXPGATOUR,
S10 KXPGAR3LEAD->KXPGATOUR. All sign +.

Entity matching: the entity code is the last ticker segment for per-entity
markets (game markets, AP top-rank, championship, golf), and the event
ticker suffix after the season digits for win-total ladders
(KXNFLWINS-27IND -> IND). A pair uses only entities present on both sides.
Coverage (entities matched) is reported.

Data: hourly candles, markets with volume >= 100.
Index per entity, v2 rules with ONE deviation: front event = the entity's
nearest-closing event with at least 1 HOUR (not 1 day) before close,
because game markets close when the game ends and the game is the news.
Win-total ladders: change in P(>= strike) of the strike nearest 0.5 at h-1.
Single-market events (one market per entity): change in its mid.
Changes only within one event; skip spread > 0.20.

Regression, pooled over a pair's entities, rows sorted by time:
  sum_{j=1..k} dLag[h+j] = a + b*dLead[h] + c*dLag[h] + e, k in {1, 6}
SEs clustered by laggard event (entity-season / entity-tournament).
Untestable: < 100 search hours or < 8 clusters. Split 65/35 by time.
F4 signed: s = t*sign; null 500 permutations, max s, 95th pct (seed
20261110); survivors to holdout once, pass s >= 2.
F4r reversed placebo: laggard -> leader, two-sided, same null procedure.
Report all tests, thresholds, survivors, holdout, coverage, tally.
No fees, no P&L.
