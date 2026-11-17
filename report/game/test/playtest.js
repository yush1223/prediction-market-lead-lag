// Appended after harness3.js + check_sync.js: plays 40 full games and checks the deal.
flush();
var problems = [], kinds = {};
for (var g = 0; g < 40; g++) {
  newGame(); startRound(); flush();
  if (G.rounds.length !== ROUNDS) problems.push("rounds " + G.rounds.length);
  for (var j = 1; j < G.rounds.length; j++) if (G.rounds[j].kind === G.rounds[j - 1].kind) problems.push("adjacent " + G.rounds[j].kind);
  G.rounds.forEach(function (r) { kinds[r.kind] = (kinds[r.kind] || 0) + 1; if (r.from == null || r.to == null || !r.q) problems.push("bad round " + r.kind) });
  for (var i = 0; i < ROUNDS; i++) {
    choose(["yes", "no", "skip"][(g + i) % 3]); flush();
    if (G.log.length !== i + 1) throw new Error("round not settled " + i);
    if (i < ROUNDS - 1) { G.i++; startRound(); flush() } else { end(); flush() }
  }
}
var sample = [ncaaf(DATA.sports.episodes[0]).q, nfl(DATA.nfl.episodes[0]).q, cbb(DATA.cbb.episodes[0]).q, oil(DATA.oil.episodes[0]).q];
"40 games ok; kinds per 40 games " + JSON.stringify(kinds) + "; problems: " + (problems.length ? problems.slice(0, 5).join("; ") : "none") + " || " + sample.join(" | ");
