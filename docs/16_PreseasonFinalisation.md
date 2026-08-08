# Finalising the 2026/27 opening squad

**Implementation date:** 2026-08-05
**Preseason production model:** `rates-rules-corrected-v4-preseason-carry-forward-promoted-fixed`
**Modules:** `src/fpl_engine/championship.py`, `src/fpl_engine/promoted_roles.py`,
`src/fpl_engine/preseason_final.py`, `src/fpl_engine/optimisation.py`,
`src/fpl_engine/live/mirror.py`
**Command:** `fpl-history finalise-preseason-squad`
**Artifacts:** `data/models/preseason-final-validation-2026-27.json`,
`data/models/preseason-final-squad-2026-27.json`,
`data/models/preseason-final-validation-2026-27.md`
**Relates to:** `15_PreseasonTeamStrength.md`, `13_TeamStrengthModel.md`,
`14_OperatingPlaybook.md`

---

## 0. What this does not revisit

The flat-versus-carry-forward comparison is settled and is not re-run. The
projection engine is not redesigned. Everything below sits on top of the
already validated `team_strength_carry_forward` preseason model and changes
four things that survive it.

## 1. Differentiated promoted-club priors

### The defect

Carry-forward gave seventeen clubs a rating from their own previous season and
gave the three promoted clubs one shared declared prior: 0.85 of league-average
attack, 1.20 of league-average goals conceded. A club that scored ninety-seven
Championship goals and a club that scraped through the play-offs come out
identical.

### The mechanism

For each promoted club, from the Championship season that ran alongside the
previous Premier League season:

```
attack_relative  = club Championship goals for per match
                   / Championship average goals for per team per match
defence_relative = club Championship goals against per match
                   / Championship average goals against per team per match

factor_i     = 1 + w x (relative_i - 1)
multiplier_i = base x factor_i / mean_j(factor_j)
```

with `base` 0.85 for attack and 1.20 for defence, and `w` one of 0.25, 0.50 or
0.75. At `w = 0` this is exactly the incumbent prior, which is what makes the
comparison a single-field change.

**The cohort normalisation is not cosmetic.** The mean of `attack_relative` is
one across the *division*, not across the three clubs that came up — promoted
clubs are the top of the division they left, and their relatives average well
above one. Applied literally the formula lifts the promoted cohort's mean
attack multiplier from 0.85 to about 0.97, which is not "varying around the
baseline" but replacing it with a claim that promoted clubs are nearly league
average. Dividing by the cohort mean factor keeps the cohort exactly on the
declared prior at every weight and lets the weight change only the spread
inside it.

Two declared bounds, deliberately asymmetric: promoted attack is capped at 1.00
and promoted defensive vulnerability floored at 1.00. Championship evidence may
say which promoted club is likelier to cope; it may never say a promoted club
is better than an average established one. Bounds are applied after
normalisation and can move the realised cohort mean, so the realised mean is
reported rather than assumed.

### The data

`data/reference/championship-seasons.json`, five Championship seasons
(2021/22–2025/26), per-club regular-season goals for and against, with source
name, URL, revision, a SHA-256 of the retrieved bytes and a retrieval
timestamp. Play-off matches are excluded: a play-off run is three to five
knockout matches and would distort a rate computed over a 46-match season. The
importer is `fpl-history import-championship`; the tables are
`championship_seasons`, `championship_team_seasons` and
`championship_team_aliases`. Premier League to Championship club naming is an
explicit alias table in the data file, not a fuzzy match.

### The verdict: not adopted

Validated across all four usable transitions with leave-one-transition-out
selection. The result splits, and the split is the finding:

| model | promoted attack RMSE | promoted attack MAE | promoted defence RMSE | overall RMSE |
| --- | --- | --- | --- | --- |
| fixed | 0.8617 | 0.7162 | 1.4972 | 1.1646 |
| w = 0.25 | 0.8523 | 0.7113 | 1.5062 | 1.1658 |
| w = 0.50 | 0.8467 | 0.7076 | 1.5218 | 1.1687 |
| w = 0.75 | 0.8469 | 0.7056 | 1.5356 | 1.1715 |

Championship goals scored carry into the Premier League well enough to improve
a promoted club's attacking forecast at every tested weight. Championship goals
conceded do not: differentiating defensive vulnerability makes the forecast
worse, monotonically in the weight. Netted over both sides the differentiated
prior is slightly behind the fixed one, and the leave-one-transition-out
selector picks the fixed prior on three of four held-out seasons.

The gate therefore fails and the fixed prior is retained. That is the declared
fallback, not a workaround.

The asymmetry is a real open question and is recorded as one: a variant that
differentiates attack and leaves defensive vulnerability flat was not tested,
because it was not among the declared candidates and inventing it after seeing
which half worked is how a result gets manufactured.

## 2. Promoted-player role evidence

### The mechanism

Appearances, starts, substitute appearances, minutes and share of team minutes,
shrunk toward the positional prior at a declared fifteen team-matches, moving
only appearance probability, start probability, expected minutes and 60-minute
probability.

The scoring firewall is structural rather than conventional. The
`championship_player_roles` table has no column for goals, assists, clean
sheets or points, and `load_role_document` *refuses* a file that carries one
rather than ignoring it — a file with a goals column was built for a different
purpose and loading it silently would leave the next reader believing the
conversion had been considered.

Identity matching uses an official FPL code when the source supplies one and
otherwise requires an exact normalised name **within the same club**. A name
matching two players matches neither. A missed match leaves a player on the
prior they already had; a wrong match attributes a real footballer's season to
someone else, and those risks are not comparable.

### The verdict: no data, so audit only

No public source of English Championship player-level minutes was reachable
from the execution environment. The FPL API, FBref, football-data.co.uk,
Understat and Transfermarkt are all blocked by the egress policy; the
GitHub-hosted sources that are reachable (`openfootball/england`,
`statsbomb/open-data`, `vaastav/Fantasy-Premier-League`) carry match results,
non-English competitions and Premier League data respectively. None carries
Championship player minutes.

Coverage is therefore zero, the declared 60% coverage gate cannot be met, the
existing player model is kept unchanged, and Championship role evidence is
reported as an audit field with no rows in it. The mechanism, its shrinkage,
its identity rules and its coverage gate are implemented and tested; what is
missing is data, and that is stated rather than approximated.

This is the prompt's own declared fallback. It is not a null result: "the
treatment was applied and changed nothing" and "there was nothing to apply" are
different claims, and only the second is true.

## 3. Goalkeepers as a pair

### The defect

A goalkeeper was valued alone. That is wrong in a specific way: exactly one of
a manager's two goalkeepers plays, and the reserve is not a reserve in the
ordinary sense — when the nominated starter records zero minutes the substitute
replaces them automatically, because no legal formation exists without a
goalkeeper. So the quantity owned is a pair:

```
value_if_A_starts = A unconditional xP + P(A records zero minutes) x B unconditional xP
value_if_B_starts = B unconditional xP + P(B records zero minutes) x A unconditional xP
```

A goalkeeper with the lower standalone projection can be the correct nomination
when their own appearance is doubtful and the partner behind them is strong,
because the pair collects in both states.

### The implementation

Compact and exact, and inside the solve rather than after it:

1. every eligible goalkeeper pair is enumerated;
2. each pair's best orientation and exact value is computed Gameweek by
   Gameweek;
3. one binary per pair enters the same objective the outfield players are
   selected under, linked to both goalkeepers' squad variables, with exactly
   one pair selected;
4. goalkeepers are removed from the ordinary starter, captain and
   bench-quality terms, so no goalkeeper's points can be counted twice;
5. the pair's per-Gameweek orientation is pinned to the starter variable by
   constraint, so the reported lineup is the one that was priced and every
   constraint written against the starting XI reads a real lineup.

The exact weekly revaluation already replaced an absent goalkeeper with the
substitute, so the objective and the rescoring now price the pair identically —
a consistency check rather than a coincidence, and one the tests assert.

A tie-break term carries the reserve's standalone value in the second
lexicographic stage only. Without it, a nominated goalkeeper whose appearance
probability saturates at 1.0 makes the pair value identical for every possible
partner, and the reserve would be settled by the deterministic index tie-break
rather than by who is actually the better substitute.

**The independence assumption is preserved and stated.** Appearance states are
independent, as everywhere else in the optimiser. Two goalkeepers at the same
club, or a first choice and their own understudy, would violate that and the
protection would be overstated.

### Effect on the live run

Reported, including when it is nothing. In the 2026/27 run the nominated
goalkeeper's appearance probability is exactly 1.0 under the current minutes
model, so the protection term is zero and the pair treatment cannot move the
selection however it is implemented. That is a property of the minutes model,
not of the pair valuation, and the artifact says so with the number attached.
The tests demonstrate the mechanism on cases where the probability is not one.

## 4. The broadened squad search

### Frontier

Forty distinct **complete squads**, each solve excluding every fifteen already
produced. The previous behaviour also required a different starting eleven,
which is right for "give me alternatives a manager can act on" and wrong for a
frontier: two squads with the same XI and different benches are different
autosub and rotation propositions.

Every candidate is rescored exactly — goalkeeper-pair orientation, legal
autosubs, bench order, captain and vice-captain fallback — and ranked by exact
decision value, with cost breaking ties before the deterministic identifier
tie-break. Whether exact rescoring reorders the solver's own ranking is
reported rather than assumed; in the live run it does, and it changes the
winner.

### The ranking pool

The frontier, every feasible bank level and every forced-inclusion
counterfactual are ranked **together** on exact value. The solver maximises a
linear objective that prices neither autosubs nor bench order, so a squad it
reached only under a side constraint can exactly beat the one it reached
without any. Leaving those out would mean recommending a squad already known to
be worse than one computed minutes earlier. The artifact records which part of
the pool the recommendation came from.

### Bank

Levels at £0.0m, £0.5m and £1.0m held back. Each level is the best *exactly
valued* squad, over everything enumerated, leaving at least that much unspent —
not merely whatever a budget-reduced solve returned, which is a linear optimum
and would overstate the price of flexibility.

Money in the bank is never converted into points; there is no defensible
exchange rate for a transfer that has not happened at a price that has not
moved. A bank-preserving squad within 0.25 expected points over eight
Gameweeks is labelled flexibility-equivalent and does **not** replace the
maximum-value primary squad: equivalent within noise is not better, and the
recommendation should not move on a rounding.

### Arsenal counterfactuals

Each eligible Arsenal defender is forced into the squad and the rest of the
squad is re-solved around them, rather than substituted into a finished squad.
The reported gap is then the true cost of ownership and the displacement chain
shows who paid for it. Price, individual projection, minutes or eligibility and
broader budget allocation are checked separately against the numbers that would
have to be true for each to be the cause. Fixture difficulty is not reported as
a separable cause: over eight Gameweeks it is already inside each defender's
horizon projection.

### Concentration tests

Four, run one at a time and never crossed: Manchester United attack −10%,
Bournemouth attack −10%, fixed against differentiated promoted priors, and
incumbent against the promoted-player role treatment. The attack perturbation
is applied to the club's *rating*, so it travels through expected goals into
their own attackers, their opponents' clean sheets, bonus and defensive
contribution; scaling a finished points total would move the players and leave
the rest of the league believing something else.

The concentration baseline is the unconstrained single solve, because every
perturbed run is one too. The recommendation comes from the wider exactly
ranked pool and may differ from that baseline; the artifact says so rather than
reporting the difference as a stress-test effect.

A factorial sweep is deliberately not run. With one opening decision per season
there is no way to tell a real interaction from a coincidence.

## 5. Live data provenance

**Updated 2026-08-08.** Earlier runs of this document were produced inside an
execution environment whose egress policy refused the connection to
`fantasy.premierleague.com` before any request was made, so those snapshots
were collected from a pinned public mirror
(`vaastav/Fantasy-Premier-League` at an immutable commit SHA) and recorded
under the ingestion source name `vaastav-fpl-mirror` — never
`official-fpl-api`. That constraint does not hold from a normal home network:
`fpl_engine.live.mirror.official_api_reachable()` confirms the official host
answers, and every ingestion run recorded against this database — including
runs before this one (ingestion runs 6, 7 and 13, dated 2026-07-30 through
2026-08-05) — is `source_name: official-fpl-api`,
`is_official_api: true`. The squad in this artifact rests on ingestion run 14,
retrieved 2026-08-05T22:54:28Z directly from
`https://fantasy.premierleague.com/api/bootstrap-static/`
(`content_sha256` recorded in `data_coverage.snapshot_provenance`).

The mirror path (`--mirror-source-ref`) remains implemented and refuses to run
when the official API is reachable, precisely so it cannot be reached for by
habit once it is no longer necessary. It is documented here because a future
rerun from a restricted environment (a CI runner, a sandboxed agent) will hit
the same egress refusal and need it again — in that case, two derivations are
declared on the mirror capture and must be re-stated: Gameweek deadlines are
reconstructed as 90 minutes before each Gameweek's first kick-off (the mirror
publishes no `events` collection, so this is the published FPL rule, not a
measurement), and preseason season-to-date counters are zeroed (the mirrored
player file for a season with no finished fixture still carries the *previous*
season's totals for continuing players, and the projection path does not read
them).

**This artifact carries no mirror warning.** Prices, availability and
fixtures come from a direct official capture, retrieved less than a day before
this run.

## 6. Reproducing it

```bash
fpl-history --database data/fpl.sqlite3 import-championship
fpl-history --database data/fpl.sqlite3 finalise-preseason-squad 2026-27 \
  --horizon 8 --frontier-size 40
```

In order: read provenance and coverage, discover transitions, score every
promoted-prior weight on each of them, apply the gate, generate the live
projection under the selected model, build the eligible candidate set, audit
role coverage, enumerate the frontier, solve the bank levels and forced
inclusions, rank everything exactly, run the concentration tests, and write the
three artifacts. The gate is applied before the live projection is generated,
so the live run cannot influence the decision that authorised it.

## 7. Remaining limitations

- **No Championship player minutes.** The role treatment is implemented and
  untestable against data. See section 2.
- **The promoted prior's two halves disagree.** Attack differentiation helps,
  defence differentiation hurts, and the untested attack-only variant is an
  open question rather than a recommendation.
- **The goalkeeper-pair correction is inert in this particular run**, because
  the minutes model puts a first-choice goalkeeper's appearance probability at
  exactly 1.0. That saturation is itself questionable and is out of scope here.
- **Ninety-six promoted-club observations.** Four transitions, three clubs, one
  eight-Gameweek window each. The promoted-prior evidence is thin by
  construction and cannot be thickened without more seasons.
- **One opening decision per season.** The historical squad-level evidence is
  four observations and is reported as secondary throughout.
- **The squad is provisional.** It stands until the final reliable pre-deadline
  team-news rerun.
