# Preseason final squad — 2026-27

Generated 2026-08-02T12:00:00+00:00. Horizon GW1–GW4. Total runtime 170.08s.

**This squad is provisional.** It stands until the final reliable pre-deadline team-news rerun, and no further.

## 1. Data coverage and provenance

- Premier League seasons imported: 2025-26, 2026-27
- Usable season transitions: none
- Live snapshot source: `synthetic` retrieved 2026-08-01T00:00:00+00:00
- Direct official API capture: no

| Championship season | clubs | matches | mean goals per club-match | source |
| --- | --- | --- | --- | --- |
| 2025-26 | 4 | 92 | 1.2391 | synthetic |

## 2. Promoted-club priors

- Gate: **FAIL**
- Selected: **fixed**
- No usable season transition exists, so no weight can be validated and the declared fixed prior is retained.

| criterion | passed |
| --- | --- |

Live priors for 2026-27 (Championship 2025-26, cohort mean attack 0.85 against a declared 0.85):

| club | Championship attack relative | defence relative | attack multiplier | defence multiplier |
| --- | --- | --- | --- | --- |
| Newly Promoted | 1.614 | 0.5263 | 0.85 | 1.2 |

## 3. Promoted-player role evidence

- Treatment applied: **none**
- Stored Championship role rows: 0
- Eligible promoted candidates: 15
- Coverage: 0.0 (minimum 0.6)

Coverage is insufficient. The existing player model is kept and Championship role evidence is reported as an audit field only, because applying it to some players and not their direct competitors would distort a squad decision more than leaving the whole cohort on the positional prior.

## 4. Goalkeeper pair

Every eligible goalkeeper pair is enumerated, each pair's best weekly orientation and exact value computed, and that value carried into the same objective the outfield players are selected under. Goalkeepers appear in the objective once and are excluded from the starter, captain and bench-quality terms, so no goalkeeper's points are counted twice. The nomination is pinned inside the solve, not swapped afterwards.

Appearance states are independent, as everywhere else in the optimiser. Two goalkeepers at the same club, or a first choice and their own understudy, would violate it and the protection would be overstated.

| GW | nominated | pair value | other orientation | uplift over starter alone | lower standalone starts |
| --- | --- | --- | --- | --- | --- |
| 1 | p1-GK1 | 4.091 | 4.091 | 0.8023 | no |
| 2 | p1-GK1 | 4.335 | 4.335 | 0.8502 | no |
| 3 | p1-GK1 | 4.335 | 4.335 | 0.8502 | no |
| 4 | p1-GK1 | 4.335 | 4.335 | 0.8502 | no |

- Pair chosen: p2-GK0, p3-GK0
- Chosen when goalkeepers are valued singly: p3-GK0, p7-GK0
- Squad changed by the pair treatment: yes
- Total substitution-protection value over the horizon: 2.8093 points
- A Gameweek nominates the lower standalone goalkeeper: no

Substitution protection is worth P(nominated goalkeeper records no minutes) x the reserve's expected points. When the minutes model puts a first-choice goalkeeper's appearance probability at one, that product is zero and the pair treatment cannot move the selection however it is implemented. The number above says whether that is the case here.

## 5. Eligibility audit

- Priced candidates: 120
- Eligible after the mean-appearance guardrail (0.6): 120
- Excluded: 0

## 6. Candidate frontier

- Requested 3, produced 14 distinct complete squads in 139.72s
- Ranked together with the bank levels and forced inclusions: 15 squads; the recommendation came from `frontier`
- Distinct starting elevens among them: 13
- Exact rescoring reorders the solver's ranking: **yes** (13 of 14 moved; largest move 11)
- Exact rescoring changes the winner: **yes**
- Balanced staged convergence (every family expanded per stage): **reached** — Practical convergence reached: the final expansions of every family produced no new winning squad and no material improvement.

## 7. Bank frontier

Money in the bank is never given a points value. A bank-preserving squad within 0.25 expected points over the horizon is reported as flexibility-equivalent, and does not replace the maximum-value primary squad: equivalent within noise is not better.

| minimum bank | exact GW1–8 value | GW1 value | cost | bank | changes | value sacrificed | flexibility-equivalent |
| --- | --- | --- | --- | --- | --- | --- | --- |
| £0.0m | 170.875 | 46.171 | £89.0m | £11.0m | 0 | 0.0 | yes |
| £0.5m | 170.875 | 46.171 | £89.0m | £11.0m | 0 | 0.0 | yes |
| £1.0m | 170.875 | 46.171 | £89.0m | £11.0m | 0 | 0.0 | yes |

## 8. Arsenal defender counterfactuals

- Eligible Arsenal defenders: 0
- Excluded by the availability guardrail: 0

| defender | price | mean appearance | GW1 xP | GW1–8 xP | forced squad value | value gap | squad changes |
| --- | --- | --- | --- | --- | --- | --- | --- |

Best squad containing any Arsenal defender: none was feasible

**Verdict: no_eligible_defender.** No ARS defender survives the eligibility guardrail, so the absence is an availability outcome, not a valuation one.

## 9. Concentration tests

Single factors only. No combination of two perturbations is run: with one opening decision per season there is no way to tell a real interaction from a coincidence.

The baseline is the unconstrained single solve under the live model, because every perturbed run is one too. The recommended squad is chosen from a wider exactly ranked pool and may differ from it.

| test | exact value | squad changes | Man Utd triple | Bournemouth double | no Arsenal | goalkeeper pair | captain |
| --- | --- | --- | --- | --- | --- | --- | --- |
| baseline | 170.037 | — | no | no | yes | p2-GK0, p3-GK0 | p3-GK0 |
| manchester_united_attack_minus_10_percent | not run | MUN is not in the 2026-27 team list, so this test could not be run. | | | | | |
| bournemouth_attack_minus_10_percent | not run | BOU is not in the 2026-27 team list, so this test could not be run. | | | | | |
| differentiated_promoted_prior | not run | The differentiated promoted prior was not adopted, so the live model already uses the fixed prior. Running the two against each other here would compare a model with itself; the historical comparison in the promoted-prior section is the evidence. | | | | | |
| promoted_player_role_treatment | not run | The role treatment was not validated — no usable Championship player-level role evidence is available — so there is no second model to run. Reporting a null result here would imply the treatment was tested and found neutral, which is not what happened. | | | | | |

## 10. Final squad

Projection run 1 under `rates-rules-corrected-v4-preseason-carry-forward-promoted-fixed`. Cost £89.0m, bank £11.0m. GW1 expected 46.171, exact GW1–8 decision value 170.875, linear objective 146.395.

| player | club | pos | price | GW1–8 xP | role |
| --- | --- | --- | --- | --- | --- |
| p2-DEF2 | C2 | DEF | £5.0m | 11.35 | XI |
| p2-DEF3 | C2 | DEF | £5.0m | 11.35 | XI |
| p2-DEF4 | C2 | DEF | £5.0m | 11.35 | XI |
| p3-DEF4 | C3 | DEF | £5.0m | 10.258 | XI |
| p8-FWD0 | C8 | FWD | £7.5m | 9.017 | XI |
| p1-GK1 | C1 | GK | £4.5m | 13.744 | XI |
| p3-MID0 | C3 | MID | £6.5m | 11.544 | XI (C) |
| p3-MID3 | C3 | MID | £6.5m | 11.544 | XI (V) |
| p4-MID2 | C4 | MID | £6.5m | 11.874 | XI |
| p4-MID3 | C4 | MID | £6.5m | 11.874 | XI |
| p4-MID4 | C4 | MID | £6.5m | 11.874 | XI |
| p1-GK0 | C1 | GK | £4.5m | 13.744 | bench 1 |
| p1-DEF0 | C1 | DEF | £5.0m | 12.669 | bench 2 |
| p8-FWD1 | C8 | FWD | £7.5m | 9.017 | bench 3 |
| p8-FWD2 | C8 | FWD | £7.5m | 9.017 | bench 4 |

## 11. Meaningful alternatives

| alternative | exact value | gap | cost | bank | changes |
| --- | --- | --- | --- | --- | --- |
| alternative_1 | 170.83 | 0.045 | £89.0m | £11.0m | 6 |

## 12. Robust and model-sensitive selections

Compared across 4 squads: the recommendation, every completed concentration run and every feasible bank level.

- Robust (in every one): p1-DEF0, p1-GK0, p1-GK1, p2-DEF2, p2-DEF3, p2-DEF4, p3-DEF4, p3-MID0, p3-MID3, p4-MID2, p4-MID3, p4-MID4, p8-FWD0, p8-FWD1, p8-FWD2
- Moderate: none
- Model-sensitive (in exactly one): none

## 13. Warnings and unresolved limitations

- The live snapshot did not come from a direct official FPL API capture. Prices, availability and fixtures are as good as the mirror behind them and no better.
- No usable historical transition was available to validate the differentiated promoted prior.
- The promoted-player role treatment was not adopted: Coverage is insufficient. The existing player model is kept and Championship role evidence is reported as an audit field only, because applying it to some players and not their direct competitors would distort a squad decision more than leaving the whole cohort on the positional prior.
