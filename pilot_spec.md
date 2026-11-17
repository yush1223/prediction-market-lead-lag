# Pilot test spec (frozen before any price data)

Pairs (from sign_map.csv, sha256 9a551c0a...fe5a2):
- E01  KXWTIW   -> KXAAAGASW   expected sign +
- E23  KXTNOTEW -> KXFRM       expected sign +

Series index: per series-day, the implied median of the underlying. For the
front event (nearest settlement at least 1 day away), take each strike's mid =
(yes_bid_close + yes_ask_close)/2, skip strikes with spread > 0.20, build
P(underlying > strike) (1 - p for "less" strikes; cumulate buckets for
"between" strikes), and linearly interpolate the strike where it crosses 0.5.
Daily change = median_t - median_{t-1} within the same event only (no roll jumps).

Regression, per pair, on days where both changes exist:
  dLag[t+1] = a + b * dLead[t] + c * dLag[t] + e
b is the delayed response. Standard errors clustered by laggard event.
One lag (1 day). No thresholds. No sweep.

Pass: sign(b) matches the sign map and t >= 2.
Report: b, t, days, independent events, and the predicted laggard move in
cents against spread + Kalshi fees. These 2 tests count toward the final tally
and are reported as a pilot.
