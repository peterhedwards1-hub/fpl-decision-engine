# The opening-squad candidate search

**Implementation date:** 2026-08-05
**Modules:** `src/fpl_engine/candidate_search.py`,
`src/fpl_engine/frontier_validation.py`, `src/fpl_engine/optimisation.py`
**Command:** `fpl-history validate-opening-squad-search`
**Artifacts:** `data/models/opening-squad-search-validation-2026-27.json`,
`data/models/opening-squad-search-validation-2026-27.md`
**Relates to:** `16_PreseasonFinalisation.md`

---

## 1. The failure

The 2026/27 finalisation generated forty distinct complete squads. All forty
fielded **the same eleven in all eight Gameweeks**. Only the four bench slots
varied, and every player who varied was a £4.0m–£4.5m reserve who never started
in any candidate.

This is not a bug in the enumeration. It is what the objective implies.

The linear objective CBC maximises is, per Gameweek, the expected points of a
legal eleven plus its captain, plus the goalkeeper-pair term and any declared
terminal value. **Outfield bench players appear in it nowhere.** So two squads
that share a weekly XI and complete themselves with any affordable legal set of
four reserves have *identical* objective values. In the live run every one of
the forty scored **429.962** to the last decimal.

The frontier excluded complete fifteens. Each solve therefore returned another
member of that tie set. The number of tied completions — four reserve slots
drawn from dozens of interchangeable cheap players at the right positions and
prices — runs to thousands, so forty solves never came close to leaving it.

### The two symptoms this explains

**"Exact rescoring reordered 39 of 40, and the linear #1 fell to rank 39."**
Every candidate had the same linear objective, so the linear ranking was
entirely tie-break order. Reordering it carried no information about the two
objectives disagreeing. That statistic should not have been read as evidence,
and is corrected here.

**"The exact winner was found only by the forced-Gabriel diagnostic."** The
winning squad's linear objective was 424.094, nearly six points *below* the
optimum. No sequence of exclusions from the optimum can reach a squad that is
not in the tie set. The diagnostic found it because forcing a player in changes
the problem, not because the diagnostic was clever.

## 2. Where the exact-minus-linear gap comes from

Exact decision value adds four things the linear objective omits: outfield
autosub activation, the goalkeeper substitution, bench ordering and the
vice-captain fallback. Decomposed Gameweek by Gameweek:

| squad | linear (XI + captain) | exact | uplift | outfield autosub | goalkeeper | vice fallback |
| --- | --- | --- | --- | --- | --- | --- |
| best normal-frontier squad | 428.097 | 449.041 | 20.944 | 10.787 | 0.000 | 10.157 |
| forced-Gabriel winner | 423.779 | 449.495 | 25.716 | 16.294 | 0.000 | 9.422 |

The winner gives up 4.318 linear points and buys 5.507 points of outfield
autosub value. Nothing else moves: no weekly rotation effect, no terminal
value, and the vice-captain term is slightly *worse*.

The essential point is that **the uplift is not a constant**. Across the mixed
pool it ranges over roughly thirteen points. A squad that far below the linear
optimum can still win on exact value, so any search that only enumerates near
the linear optimum is looking in the wrong place.

The goalkeeper term being 0.000 in both is itself an artefact of the frontier,
not a property of the model: both squads nominate a goalkeeper whose appearance
probability is exactly 1.0, so substitution protection is worth nothing. The
mixed search finds squads that nominate **Raya over Leno** despite Raya's lower
standalone expectation, because the pair collects in the states Raya misses —
worth **6.254 points** over the horizon. The previous report's conclusion that
the goalkeeper-pair treatment was inert was wrong, and it was wrong because the
candidate search never reached a squad where it mattered.

## 3. The replacement

Candidates come from six declared families, run in a declared order.

| family | what it does | why |
| --- | --- | --- |
| A `complete_squads` | exclude complete fifteens | the incumbent behaviour, kept so the new search provably contains everything the old one could find |
| B `distinct_xis` | exclude the starting eleven too | stops the search walking bench permutations of one lineup |
| C `slack_bands` | pin the objective at `optimum − δ` and maximise reserve quality | **the family that matters**: the only one that can cross the tie set |
| D `forced` | force each Arsenal defender, the top unselected players per position, players in the linear leaders, and each incumbent player out | the diagnostics become part of the pool instead of an accident |
| E `structural` | bank floors, defender price bands, defensive spend bands, premium-midfielder counts, no triple-up | shapes a manager might want for reasons a projection cannot see |
| F `perturbations` | tiny declared per-player objective offsets | shakes apart exact ties |

Declared slack bands: **0.25, 0.5, 1.0, 2.0, 4.0, 8.0, 16.0**. The first five
were the specified set; the last two exist because the measured uplift spread
is about thirteen points and a band set narrower than the spread cannot reach
the exact optimum however many candidates it draws. Whether the widest band
still dominates the realised spread is recomputed and reported every run, so
this stays a checked claim.

### Generation never touches valuation

Every family only decides which squads are *produced*. Ranking is the unchanged
exact decision value with cost breaking ties, and every candidate is rescored
by the same function whatever constraint generated it.

Two details enforce that rather than merely asserting it. The generator returns
**bare squad memberships** — no numbers at all — because a generator that
reported its own objective would report the band edge it was pinned against
rather than the squad's own value; this was an actual bug, caught because the
reported linear value came out exactly equal to `optimum − δ` every time.
And the perturbation vectors are removed before anything is scored.

### Cost

Model construction is extracted into `build_squad_model`, shared by the scorer
and the generator so the two cannot drift. `enumerate_squad_ids` runs **one**
solve per candidate instead of the scorer's three, and exact rescoring happens
once per *unique* squad after deduplication. That is what makes a
five-hundred-squad pool affordable at all.

## 4. Acceptance and convergence are two different claims

They were once conflated in one `passed` flag. They are now reported as two
separate verdicts, because a search can be a sound replacement for the old one
without having exhausted the space.

**Search acceptance** — the mixed search is a sound replacement — holds only if
all of these do, recomputed every run:

1. the pool contains every squad the older searches found;
2. no forced-player diagnostic finds a better exact squad from outside the pool;
3. the pool contains meaningful starting-eleven diversity;
4. runtime remains usable before a deadline;
5. tests and Ruff pass.

**Convergence** — expanding the search further would not find a better squad —
is the separate, stronger claim, and it is never reported as passed unless its
own test passes.

### The convergence test was wrong, and is fixed

The stages used to be nested prefixes of the pool taken in *global generation
order*. That looked principled — family A alone reproduces the incumbent
frontier, so the first prefix is the old behaviour — but it broke at a large
frontier. Families A and B are generated first, and at `--frontier-size 500`
they each emit five hundred candidates, so every prefix small enough to test
(≤ 500) was filled entirely by those two frontier-reproducing families while the
slack-band family that actually finds the exact winner sat *beyond* the prefix.
The test then declared convergence on a pool that did not contain the winning
squad at all — a false pass.

The stages are now **fractions of every family**: stage `f` contains the first
`ceil(f · n)` candidates of each family in that family's own generation order,
for `f` in 25/50/75/100 %. The stages stay genuinely nested, every stage expands
*every* family, and the final stage is the whole pool — so the winning squad is
always inside the last stage and "no new winner across the final expansions"
means what it says. Practical convergence is still: two successive stages with no
new winning squad and under 0.05 exact points of improvement.

Run under the corrected test, the live 2026/27 search does **not** converge: the
winning slack-band squad appears only at the 100 % stage, a 4.7-point jump over
the 75 % stage. The confidence in that squad comes from two independent
full-scale searches agreeing on it, not from a convergence proof.

**No claim of global nonlinear optimality is made anywhere.** The exact
objective is not the one the solver optimises, so no solver proof covers it.
This is a generate-and-score procedure with a stated convergence criterion.

### Generation is time-capped

Each individual generation solve is capped (30 s). The linear objective's
degeneracy — every completion of one weekly XI ties exactly — otherwise lets the
solver spend arbitrarily long proving optimality over an interchangeable tie set,
which turned a search into a multi-hour or hung run. When the cap is hit with a
feasible incumbent, that incumbent is accepted as a proposed candidate rather
than discarded: safe, because generation only proposes and every candidate is
exact-rescored before ranking. The exact-scoring solves are never capped.

### Uplift is measured against the solver's objective

The reported uplift is `exact decision value − solver objective` — the quantity
the slack bands are declared in, so it is the diagnostic that says whether the
band set is wide enough to reach the exact optimum. `exact − lineup points` is
retained as a separate field for the narrower "value beyond the eleven that
started" question.

### Goalkeeper captaincy is no longer excluded from generation

The linear generation guide used to bar goalkeepers from its captain term. That
never changed any *reported* value — the exact captaincy enumeration always
evaluated the nominated goalkeeper as a captain candidate, so a goalkeeper who
out-scored the outfield starters would already be captained in the exact
decision value — but it biased which squads were generated. It is removed. It
was never a double-count: a goalkeeper earns its base value through the pair
term rather than the starter term, so a captain term on a starting goalkeeper is
the same single doubling every outfield captain receives. The exact objective is
untouched; only the set of squads generation can reach is affected.

## 5. Exact-objective feasibility

Asked because the honest fix for "the solver optimises the wrong thing" is to
make it optimise the right thing.

**Implemented — a tighter linear surrogate, for generation only.** Reserve
quality maximised inside a slack band. It is the linear quantity most closely
correlated with the omitted autosub value, it reuses the existing bench
tie-break expression, and it is the family that finds the exact winner on the
live candidate set.

**Not taken — an exact reserve term in the solver objective.** The exact autosub
contribution sums over the joint appearance states of ten outfield starters and
three outfield substitutes — 8192 states — and which substitutes activate
depends on which starters blanked *and* on formation legality after the swap. It
is not a linear function of the selection variables, and linearising it needs a
variable per state per Gameweek. Not possible without a major rewrite.

**Not taken — precomputed lineup and bench configurations.** Enumerating legal
elevens for a fixed fifteen is cheap, but the outer problem is choosing the
fifteen from hundreds of players, and each configuration still needs a
joint-state integration. Useful only after selection, which is where it is
already used.

**Later work — decomposition or column generation.** Squads as columns with the
exact value as the column cost would price the right objective. The pricing
subproblem is the nonlinear part and would need its own approximation. This is
the principled fix and it replaces the optimiser, so it is recorded rather than
attempted.

## 6. Running it

```bash
fpl-history --database data/fpl.sqlite3 validate-opening-squad-search 2026-27 \
  --horizon 8 --mixed-scale 1.0 --historical-scale 0.1
```

The production frontier used by `finalise-preseason-squad` now routes through
the same families; `--frontier-size` becomes a target pool size that the family
structure rounds up.

## 7. Counterfactuals, searched not solved

The finalise report answers its counterfactual questions — bank levels, the
Arsenal-defender cost — with a single budget-reduced or forced-in linear solve.
That understates every squad it touches for exactly the reason this whole
document exists: one solve returns the linear optimum, and the exact winner sits
*below* it. Re-run as full mixed searches (every family, exact-rescored), the
answers move a long way, and always in the same direction — the true cost of a
constraint is smaller than one solve makes it look, because the constrained
search recovers autosub value the constrained solve cannot see.

On the direct official 2026/27 capture (eligible set of 242, GW1–8):

| question | one solve | full mixed search | what the search recovered |
| --- | --- | --- | --- |
| unconstrained best | 448.294 | **455.106** | the reference itself was understated by 6.8 |
| **Haaland forbidden** | 437.705 | 439.370 | — |
| **Haaland premium** | 10.589 | **15.736** | the premium is half again as large as one solve implies |
| bank £0.5m held | 446.471 (cost 8.635) | 452.656 (**cost 2.45**) | 6.2 |
| bank £1.0m held | 445.857 (cost 9.249) | 450.795 (**cost 4.31**) | 4.9 |
| strongest Arsenal defender (Gabriel) forced in | 449.239 (cost 5.867) | 453.755 (**cost 1.35**) | 4.5 |

The decision content changes, not just the decimals. Owning Haaland is worth
about **fifteen and a half** points over eight Gameweeks, not ten. Holding half a
million in the bank costs about **two and a half** points, not eight. And forcing
in the best Arsenal defender costs about **one and a third** points, not six —
close enough to free that the earlier "not worth it" verdict is overturned. The
Haaland-required and Haaland-forbidden searches are the two runs item 5 asks for;
neither is inferred from a single solve. (The Gabriel-forced search is also the
one run of the seven that reaches practical convergence, because forcing an £8.0m
defender into a £100m squad constrains the space enough for the winner to settle.)

### Why Foden beats Gakpo — the winner decomposed

The winning squad (455.106) and the previous frontier-40 squad (450.354) are the
same fifteen but for one midfielder: the winner starts **Foden**, the previous
squad **Gakpo**. Gakpo has *more* raw horizon points (31.908 vs 30.867) and is a
nailed-on starter (appearance 1.00 vs Foden's 0.735), and the linear objective
duly prefers the Gakpo squad by about a point (419.35 vs 418.35). The exact value
reverses it, entirely through autosubs:

| squad | linear (XI+captain) | autosub value | captain | exact |
| --- | --- | --- | --- | --- |
| Foden (winner) | 418.35 | **27.36** | 7.47 | **455.106** |
| Gakpo | 419.35 | 21.61 | 7.47 | 450.354 |

Foden gives up 1.0 linear point and buys **5.76** points of autosub value, for a
net **+4.75**. The mechanism is the counter-intuitive part: Foden is *doubtful*,
and it is precisely his ~26 % blank rate that pays. When a starter blanks, a legal
substitute is drawn from a strong bench (Evanilson £6.0m, Truffert £5.5m), so the
squad's bench value is realised far more often. Decomposed Gameweek by Gameweek
the Foden squad's autosub contribution beats the Gakpo squad's in every one of the
eight weeks. This is the entire thesis of the search in a single swap — a squad
trading linear XI quality for autosub value, invisible to the solver and decisive
on exact value. It is also, honestly, a **higher-variance** proposition: the edge
rides on a doubtful player activating the bench, and the optimiser maximises
expectation, not its spread.

## 8. Open items

- **Promoted-club attack-only prior.** Championship goals scored improved
  promoted clubs' attacking forecasts at every tested weight while goals
  conceded made their defensive forecasts worse. A variant differentiating
  attack alone was not tested and is recorded as future work, not implemented.
- **No global optimality.** See section 4.
- **Runtime.** A full five-hundred-squad pool takes tens of minutes. A
  pre-deadline rerun should use a reduced scale and say so.
