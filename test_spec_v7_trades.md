# Spec v7: stale-quote fill check on AAA gas weekly (frozen before any trades)

Question: after a large oil move, did traders actually get filled at stale
AAA gas prices, and did those fills profit as gas caught up? If no such fills
exist, a market-making or stale-quote strategy has no counterparty.
Tally before v7: 6,880.

Data: every trade in KXAAAGASW markets with volume >= 100 (/historical/trades
and /markets/trades, paginated), during the span covered by the 1-minute
candles already stored for KXWTIW and KXAAAGASW (candles_1m).
Oil moves: 5-minute bars, v2 index for KXWTIW exactly as in spec v4
(forward-filled 1-minute quotes). A "big move" bar = |dLead| in the top 5% of
non-zero bar changes; direction d = sign(dLead).
Gas mid at time T: the same ticker's last 1-minute candle at or before T,
mid = (bid+ask)/2; skipped if spread > 0.20, if no candle in the prior 24h,
or if the market closed before T.
Taker markout per contract at horizon H: m = s * (mid(t+H) - yes_price),
s = +1 if taker_side = yes, -1 if no. (Positive = taker gained vs later mid.)
Groups, by trade time t:
  A aligned  : within 60 min after the end of a big-move bar, and s == d
               (all AAA gas weekly strikes are "above"; oil up => YES up)
  B against  : within 60 min after a big-move bar, and s != d
  C baseline : not within 60 min after any big-move bar
Primary: contract-weighted mean m at H = 2h for A minus C; t-stat from a
regression of m on group dummies weighted by contracts, clustered by gas
event. Pass = A - C > 0 with t >= 2. One test; tally becomes 6,881.
Descriptive: same at H = 30 min and 6h; trade counts, contracts and
summed markout dollars per group; share of A with m > 0; the largest
single contributing A trade (count x m). No fees (taker fees would reduce A).
