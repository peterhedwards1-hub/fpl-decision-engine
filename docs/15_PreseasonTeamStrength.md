# Preseason team strength: replacing the flat prior

**Implementation date:** 2026-08-04
**In-season production incumbent:** `rates-rules-corrected-v4` — **unchanged**
**Preseason production model:** `rates-rules-corrected-v4-preseason-carry-forward`
**Module:** `src/fpl_engine/preseason_strength.py`
**Selector:** `src/fpl_engine/production.py`
**Command:** `fpl-history validate-preseason-strength`
**Artifacts:** `data/models/preseason-strength-validation-2026-27.json`,
`data/models/preseason-squad-comparison-2026-27.json`,
`data/models/preseason-strength-validation-2026-27.md`
**Relates to:** `13_TeamStrengthModel.md`, `14_OperatingPlaybook.md`,
`09_ModellingAndEvaluationPlan.md`

---

## 1. Why flat team strength is unsuitable before Gameweek 1

The production team-strength path rates a club by its raw goals for and against
in the *current* season, shrunk toward the league average. Before a ball is
kicked there is nothing to read. Every club shrinks to exactly the league
average, so every attack and defence multiplier is 1.0 and the only thing
separating two fixtures is which side is at home.

That is not a small inaccuracy. It is the absence of an opinion, and it is
measurable directly without any outcome. At the 2026/27 GW1 origin:

| model | distinct attack multipliers | attack spread | established minus promoted |
| --- | --- | --- | --- |
| flat | 1 | 0.000 | 0.000 |
| carry-forward | 15 | 0.524 | 0.189 |
| opponent-adjusted | 18 | 0.368 | 0.181 |

Under the flat model Manchester City, Bournemouth and newly promoted Hull City
are the same club. An opening-squad optimiser handed those beliefs will buy a
Bournemouth defender for the trip to the Etihad, because as far as the model is
concerned that is a neutral fixture. It did exactly that: the previous
flat-model recommendation started Truffert (Bournemouth) away at Manchester City
with a 0.224 clean-sheet probability — the same number it gave every away side
in the division.

## 2. How simple carry-forward works

For each club that played in the previous season, at the GW1 origin:

```
prior attack rate =
    (previous-season goals scored + prior_matches × previous-season league average)
    / (previous-season matches + prior_matches)

prior defensive vulnerability =
    (previous-season goals conceded + prior_matches × previous-season league average)
    / (previous-season matches + prior_matches)
```

where `prior_matches` is `carry_forward_regression_matches`. Both rates are then
divided by the league average to give multipliers, and clamped to
`[minimum_team_multiplier, maximum_team_multiplier]`.

Clubs are matched across seasons **by name**, because the source reassigns team
numbering every year.

This is the existing `team_strength_carry_forward` route in
`RatesProjectionModel._carry_forward_team_rates`. No second implementation was
added: reusing it is what guarantees the evaluated model and the live model are
the same code.

### Declared constants

Nothing here was fitted. Every value was already declared in the incumbent
configuration:

| constant | value |
| --- | --- |
| `carry_forward_regression_matches` | 12.0 |
| `promoted_team_attack_multiplier` | 0.85 |
| `promoted_team_defence_multiplier` | 1.20 |
| `home_attack_multiplier` | 1.068 |
| `away_attack_multiplier` | 0.851 |
| `minimum_team_multiplier` / `maximum_team_multiplier` | 0.60 / 1.50 |

Venue multipliers are applied to the *fixture*, never to a club's rating, so
home advantage can never be mistaken for team quality.

### Promoted clubs

A promoted club has no top-flight evidence at all, so nothing can be carried
forward. It receives the declared conservative prior — 0.85 of league-average
attack and 1.20 of league-average goals conceded — which is a stated assumption,
not a measurement, and is labelled as such in the artifact. For 2026/27 that
applies to Coventry City, Hull City and Ipswich Town.

## 3. What was compared, and how

Exactly one field separates the control from the candidate:

| | `team_strength_model` | `team_strength_carry_forward` |
| --- | --- | --- |
| control (`flat`) | `raw_goals` | `false` |
| candidate (`carry_forward`) | `raw_goals` | `true` |

Everything else is identical — player rates, minutes model, scoring-event
source, defensive-contribution model, cold-start prior, availability handling,
fixture horizon, optimiser and budget. A test asserts that the two dataclasses
differ in that one field and no other, so the comparison cannot silently pick up
a second change.

The existing opponent-adjusted challenger is scored alongside as a **secondary
reference only**. It changes three things at once and this exercise does not
qualify it; it is never selected for production here.

### Point-in-time safety

Team strength is estimated once, at the GW1 origin, from the previous season
only, and then held **fixed** across GW1–GW8. That is what a preseason forecast
actually is: it is not re-estimated each week, so re-estimating it in the
evaluation would score a model nobody would have used at the deadline.

Three properties are tested rather than asserted:

- the carry-forward prior reads only the *immediately* previous season — a
  lopsided season two years back does not move it;
- playing the target season out, with the table completely inverted, does not
  change any GW1 rating by a single digit;
- no later ingestion run or end-of-season player record enters a GW1 forecast.

### Historical seasons used

Every consecutive season pair in the database was discovered and given a
usability verdict. All four were usable:

- 2021/22 → 2022/23
- 2022/23 → 2023/24
- 2023/24 → 2024/25
- 2024/25 → 2025/26

Nothing was excluded. Had anything been, it would appear in
`validation.excluded_transitions` with the reason, because "we excluded it" and
"it was never there" are different claims. A previous season below 90% completion
cannot support a prior; a target season with fewer than 20 finished GW1–GW8
fixtures cannot score one.

## 4. The decision gate

Six criteria, all of which must pass. Nothing is weighted or traded off.

| # | criterion | result |
| --- | --- | --- |
| 1 | Aggregate GW1–GW8 team-goal RMSE or MAE improves | pass — RMSE 1.2611 → 1.1646, MAE 0.9583 → 0.8945 |
| 2 | Clean-sheet Brier not materially worse (tolerance 0.005) | pass — 0.1853 → 0.1720, an improvement |
| 3 | Opening-squad decision not worse | pass — mean realised GW1–GW8 points 389.25 → 412.25 |
| 4 | Acceptable across more than one usable transition | pass — 4 of 4 |
| 5 | Separates established from promoted clubs | pass — 1 distinct multiplier → 15 |
| 6 | No severe new calibration defect | pass — goal bias −0.085 → −0.003 |

**"Effectively neutral" was defined before any decision-level result was read:**
the candidate may give up no more than **0.5 realised points over GW1–GW8 per
historical season** and still count as neutral. Half a point across eight
Gameweeks is inside the noise of a single autosub. It did not need the
allowance — it gained 23 points a season on average — but the threshold has to
predate the number for it to mean anything.

### Per-season results

| transition | flat RMSE | carry RMSE | flat Brier | carry Brier | realised points Δ | squad regret Δ |
| --- | --- | --- | --- | --- | --- | --- |
| 2021/22 → 2022/23 | 1.4078 | 1.2844 | 0.1898 | 0.1764 | +57 | −36 |
| 2022/23 → 2023/24 | 1.3350 | 1.2089 | 0.1760 | 0.1592 | +38 | −63 |
| 2023/24 → 2024/25 | 1.1674 | 1.0814 | 0.1755 | 0.1648 | +19 | +42 |
| 2024/25 → 2025/26 | 1.1394 | 1.0918 | 0.2006 | 0.1883 | −22 | −23 |

The candidate improves team-goal RMSE, MAE and clean-sheet Brier in **all four**
seasons, in both the GW1–GW4 and GW5–GW8 halves, home and away, and for both
established and promoted clubs. That consistency is the finding.

The decision column is not consistent, and should not be read as if it were.
2024/25 → 2025/26 lost 22 realised points; 2023/24 → 2024/25 gained 19 points on
the squad measure while *losing* 42 on the regret measure. One opening squad per
season is four decisions in total, each a single draw from a high-variance
distribution. The forecast evidence is strong; the decision evidence is
directionally favourable and individually noisy, and the gate is written so the
forecast evidence carries it.

## 5. Why this is a preseason-only production choice

The candidate is better *before* GW1 because that is where the flat model has no
information at all. Once real fixtures exist, the incumbent has evidence from
the season actually being played, and nothing here tested whether a stale
previous-season prior still helps in October. Promoting it globally would be
claiming something that was not measured.

So the change is scoped by decision, not by date:

- `select_preseason_projection_run` returns a run **only at GW1** and only for
  the `-preseason-carry-forward` model version;
- `select_production_projection_run` never had that version in its allow-list,
  so the generic newest-qualified-run rule cannot reach it;
- `select_decision_projection_run` chooses between the two and **returns which
  decision it made**, so a caller cannot report a preseason forecast as an
  in-season one;
- the preseason route is taken only when the validation artifact records a gate
  pass for that exact season and model version.

A test inserts a *newer* preseason run at GW12 and asserts the in-season
selector still returns the incumbent.

## 6. How the squad is regenerated

```powershell
fpl-history --database data/fpl.sqlite3 validate-preseason-strength 2026-27 `
  --horizon 8 --candidate-pool-size 8 `
  --output data/models/preseason-strength-validation-2026-27.json `
  --comparison-output data/models/preseason-squad-comparison-2026-27.json `
  --markdown-output data/models/preseason-strength-validation-2026-27.md
```

In order: discover transitions, score every model on each of them, apply the
gate, then — and only then — generate the live projection under the selected
model, optimise over eight distinct solver-proven candidate squads, exactly
revalue weekly XIs with legal autosubs and captain fallback, and cross-value
every squad under every model.

The gate is applied *before* the live projection is generated, so the live run
cannot influence the decision that authorised it.

Accepted, unexpired reviewed research modifiers are applied to the live run.
Expired and informational-only findings are excluded by `active_modifiers`, so
nothing in this module has to decide which is which.

`preseason-readiness` then picks up the validated run automatically. Check
`decision_context`: it must read `preseason_opening_squad`. If it reads
`in_season_live_projection`, a blocker says the squad still rests on the flat
model.

## 7. How to read the cross-model and robustness results

### Cross-valuation

Two configurations produce expected points on their own scales. "B scores 4
points more than A" is meaningless across scales. What can be compared is what
*one* model thinks of *two* squads:

| valuing model ↓ | flat squad | carry-forward squad | opponent-adjusted squad |
| --- | --- | --- | --- |
| flat | 419.31 | 416.83 | 414.62 |
| carry-forward | 448.97 | 450.13 | 445.78 |
| opponent-adjusted | 395.05 | 390.43 | 392.79 |

Read each **row**. Each model prefers the squad it chose itself, which is the
consistency check that the optimiser is solving the objective the valuation
reads. Reading down a column compares two scales and means nothing.

The flat and carry-forward squads share 12 of 15 players: three in, three out.
`flat_comparison.changed_players` explains every player that any of the three
squads picked and another did not — nine in total — component by component: club,
position, price, both horizon projections, both GW1 points, expected minutes,
appearance probability, opponent expected goals, clean-sheet probability,
attacking points, defensive contribution, and whether the change came from team
strength or from what the rest of the squad could afford.

That last field is a coarse rule, not a decomposition: a horizon move of at least
0.5 points is labelled `team_strength`, a smaller move accompanied by a change of
squad membership is labelled `squad_budget_interaction`. A player sitting near
the boundary is genuinely both, and the artifact carries the components so a
reader can see which.

### Robustness

Four declared runs, not a search: regression at 8, 12 and 16 matches at the
declared promoted priors, plus the preseason appearance cap (0.95) toggled on at
the declared regression strength of 12. Crossing every combination would be
twelve runs and the broad parameter search this work explicitly rules out.

Selections are classified `robust` (in every run), `moderate` (in more than one
but not all) or `model_sensitive` (in exactly one). Every run is deterministic —
same inputs, same solver, no seeded randomness — and a test asserts two
consecutive runs produce byte-identical output.

## 8. Truffert, O'Shea and Muñoz

The three selections the flat model's blindness shows up in most clearly.

| player | club, position | GW1 fixture | flat CS prob | carry CS prob | flat GW1–8 | carry GW1–8 |
| --- | --- | --- | --- | --- | --- | --- |
| Truffert | Bournemouth, DEF | away at Man City | 0.224 | 0.129 | 30.88 | 29.79 |
| O'Shea | Ipswich Town, DEF | home to Sunderland | 0.304 | 0.303 | 23.29 | 19.64 |
| Muñoz | Crystal Palace, DEF | away at Everton | 0.224 | 0.264 | 26.34 | 25.82 |

**Truffert** is the case the whole exercise exists for. The flat model gave a
Bournemouth defender at the Etihad the same 0.224 clean-sheet probability it
gave every away side in the league, and started him. The carry-forward model
knows Manchester City scored the most goals in the division last season, raises
their expected goals from 1.50 to 2.05, and cuts his clean-sheet probability
almost in half. He stays in the squad on price and attacking return, but drops
to bench 4 in every robustness run. A model that starts a Bournemouth defender
at Manchester City because it thinks City are average is making an impossible
claim, and that is now impossible to make.

**O'Shea** loses the most, 3.65 points over the horizon, and his fixture barely
moved. The change is not the opponent — it is Ipswich themselves. As a promoted
club he now carries the declared 1.20 defensive prior instead of league average,
so his own side is expected to concede more all horizon. He still starts,
because at 4.0m a starting defender who plays is worth owning even with a
weakened clean-sheet expectation.

**Muñoz** is the one whose GW1 fixture got *easier* and who still dropped out.
Everton were below league average last season, so a Crystal Palace defender away
at Goodison sees his clean-sheet probability rise from 0.224 to 0.264. His
horizon projection nonetheless falls 0.51, because Crystal Palace themselves are
rated below average across all eight Gameweeks.

A caution on how that is labelled. The artifact's mechanical rule attributes a
change to `team_strength` when the horizon projection moves by at least 0.5, and
Muñoz's −0.514 only just clears that line. The honest reading is that both
mechanisms are present: his own projection barely moved, and he lost his place at
5.5m to a 4.0m Ipswich defender whose relative value the flat model could not
see. Read the label as a coarse first pass, not a decomposition.

## 9. Remaining limitations

- **Previous-season goals are an imperfect measure of a club.** They ignore who
  the goals came against, and a settled squad and a rebuilt one carry the same
  prior. The opponent-adjusted challenger fixes the first and part of the
  second, and scored essentially level on team goals (RMSE 1.1718 against
  1.1646) while doing better on the decision measure (mean realised 425.25
  against 412.25). It is not promoted here because it changes three things at
  once and this evaluation cannot attribute the difference to any one of them.
  It is a real open question, not a settled one.
- **Nothing reads a transfer fee, a manager change or a reputation.** A club
  that spent £200m in the summer carries last season's rating. This is a stated
  limitation, not an oversight: spending is not evidence of quality and inferring
  it would be inventing a signal.
- **The decision evidence is four observations.** One opening squad per season.
  The direction is favourable and the forecast evidence behind it is strong and
  consistent, but a single season can and did go the other way.
- **The promoted prior is a guess.** 0.85 and 1.20 are declared, not estimated,
  and they apply identically to a club that walked the Championship and one that
  scraped a playoff.
- **Historical seasons are design evidence.** Forward 2026/27 captures remain the
  qualification for anything claiming to be better; this gate authorises a
  preseason model choice, not a general promotion.

Previous-season strength is imperfect. It is also historically tested across
four consecutive season transitions and materially better, on every forecast
measure, than assuming every club in the division is identical.
