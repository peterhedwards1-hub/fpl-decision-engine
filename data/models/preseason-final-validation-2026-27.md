# Preseason final squad — 2026-27

Generated 2026-08-05T12:38:32.020696+00:00. Horizon GW1–GW8. Total runtime 1498.39s.

**This squad is provisional.** It stands until the final reliable pre-deadline team-news rerun, and no further.

## 1. Data coverage and provenance

- Premier League seasons imported: 2021-22, 2022-23, 2023-24, 2024-25, 2025-26, 2026-27
- Usable season transitions: 2021-22->2022-23, 2022-23->2023-24, 2023-24->2024-25, 2024-25->2025-26
- Live snapshot source: `vaastav-fpl-mirror` retrieved 2026-08-05T12:09:07.752399+00:00
- Direct official API capture: no

| Championship season | clubs | matches | mean goals per club-match | source |
| --- | --- | --- | --- | --- |
| 2021-22 | 24 | 552 | 1.2545 | openfootball/england |
| 2022-23 | 24 | 552 | 1.2156 | openfootball/england |
| 2023-24 | 24 | 552 | 1.3406 | openfootball/england |
| 2024-25 | 24 | 552 | 1.2255 | openfootball/england |
| 2025-26 | 24 | 552 | 1.3025 | openfootball/england |

## 2. Promoted-club priors

- Gate: **FAIL**
- Selected: **fixed**
- No tested weight improved promoted-club forecasting reliably enough to displace the declared fixed prior, so the fixed prior is retained.

| criterion | passed |
| --- | --- |
| promoted_evidence_exists | yes |
| promoted_goal_error_improves | no |
| improves_on_multiple_transitions | yes |
| overall_forecasting_not_materially_worse | yes |
| survives_leave_one_transition_out | no |

| model | promoted goals RMSE | promoted goals MAE | promoted bias | overall RMSE | overall CS Brier |
| --- | --- | --- | --- | --- | --- |
| fixed | 1.2215 | 0.8851 | -0.0389 | 1.1646 | 0.172 |
| championship_relative_w0.25 | 1.2237 | 0.887 | -0.0413 | 1.1658 | 0.172 |
| championship_relative_w0.5 | 1.2314 | 0.8914 | -0.0438 | 1.1687 | 0.1721 |
| championship_relative_w0.75 | 1.24 | 0.8967 | -0.0423 | 1.1715 | 0.1722 |

Secondary evidence — what each prior's opening squad actually scored, one observation per season:

| model | mean realised GW1–8 points | seasons |
| --- | --- | --- |
| fixed | 409.5 | 4 |
| championship_relative_w0.25 | 407.25 | 4 |

Difference (championship_relative_w0.25 minus fixed): -2.25 points per season. Four observations; this is not what the gate reads.

Live priors for 2026-27 (Championship 2025-26, cohort mean attack 0.85 against a declared 0.85):

| club | Championship attack relative | defence relative | attack multiplier | defence multiplier |
| --- | --- | --- | --- | --- |
| Coventry City | 1.6189 | 0.751 | 0.85 | 1.2 |
| Hull City | 1.1683 | 1.1015 | 0.85 | 1.2 |
| Ipswich Town | 1.3352 | 0.7844 | 0.85 | 1.2 |

## 3. Promoted-player role evidence

- Treatment applied: **none**
- Stored Championship role rows: 0
- Eligible promoted candidates: 86
- Coverage: 0.0 (minimum 0.6)

Coverage is insufficient. The existing player model is kept and Championship role evidence is reported as an audit field only, because applying it to some players and not their direct competitors would distort a squad decision more than leaving the whole cohort on the positional prior.

## 4. Goalkeeper pair

Every eligible goalkeeper pair is enumerated, each pair's best weekly orientation and exact value computed, and that value carried into the same objective the outfield players are selected under. Goalkeepers appear in the objective once and are excluded from the starter, captain and bench-quality terms, so no goalkeeper's points are counted twice. The nomination is pinned inside the solve, not swapped afterwards.

Appearance states are independent, as everywhere else in the optimiser. Two goalkeepers at the same club, or a first choice and their own understudy, would violate it and the protection would be overstated.

| GW | nominated | pair value | other orientation | uplift over starter alone | lower standalone starts |
| --- | --- | --- | --- | --- | --- |
| 1 | Leno | 4.084 | 3.907 | 0.0 | no |
| 2 | Leno | 4.104 | 2.675 | 0.0 | no |
| 3 | Leno | 4.588 | 3.294 | 0.0 | no |
| 4 | Leno | 3.481 | 3.481 | 0.0 | no |
| 5 | Leno | 3.804 | 3.281 | 0.0 | no |
| 6 | Leno | 4.114 | 3.746 | 0.0 | no |
| 7 | Leno | 4.565 | 2.494 | 0.0 | no |
| 8 | Leno | 4.114 | 3.714 | 0.0 | no |

- Pair chosen: Leno, Palmer
- Chosen when goalkeepers are valued singly: Leno, Palmer
- Squad changed by the pair treatment: no
- Total substitution-protection value over the horizon: 0.0 points
- A Gameweek nominates the lower standalone goalkeeper: no

Substitution protection is worth P(nominated goalkeeper records no minutes) x the reserve's expected points. When the minutes model puts a first-choice goalkeeper's appearance probability at one, that product is zero and the pair treatment cannot move the selection however it is implemented. The number above says whether that is the case here.

## 5. Eligibility audit

- Priced candidates: 567
- Eligible after the mean-appearance guardrail (0.6): 243
- Excluded: 324

## 6. Candidate frontier

- Requested 40, produced 40 distinct complete squads in 961.97s
- Ranked together with the bank levels and forced inclusions: 44 squads; the recommendation came from `forced_Gabriel`
- Distinct starting elevens among them: 1
- Exact rescoring reorders the solver's ranking: **yes** (39 of 40 moved; largest move 38)
- Exact rescoring changes the winner: **yes**

## 7. Bank frontier

Money in the bank is never given a points value. A bank-preserving squad within 0.25 expected points over the horizon is reported as flexibility-equivalent, and does not replace the maximum-value primary squad: equivalent within noise is not better.

| minimum bank | exact GW1–8 value | GW1 value | cost | bank | changes | value sacrificed | flexibility-equivalent |
| --- | --- | --- | --- | --- | --- | --- | --- |
| £0.0m | 449.495 | 55.711 | £100.0m | £0.0m | 0 | 0.0 | yes |
| £0.5m | 447.016 | 55.277 | £99.5m | £0.5m | 6 | 2.479 | no |
| £1.0m | 446.454 | 55.107 | £99.0m | £1.0m | 4 | 3.041 | no |

## 8. Arsenal defender counterfactuals

- Eligible Arsenal defenders: 2
- Excluded by the availability guardrail: 5

| defender | price | mean appearance | GW1 xP | GW1–8 xP | forced squad value | value gap | squad changes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| White | £5.5m | 0.6264 | 2.897 | 20.419 | 447.31 | 2.185 | 5 |
| Gabriel | £8.0m | 0.7827 | 4.1 | 28.729 | 449.495 | 0.0 | 0 |

Best squad containing any Arsenal defender: Gabriel at 449.495 (0.0 behind the recommendation)

**Verdict: not_absent.** The recommended squad contains 1 ARS player(s) — Gabriel — so there is no absence to explain. The counterfactual table below still reports what forcing each defender in costs.

## 9. Concentration tests

Single factors only. No combination of two perturbations is run: with one opening decision per season there is no way to tell a real interaction from a coincidence.

The baseline is the unconstrained single solve under the live model, because every perturbed run is one too. The recommended squad is chosen from a wider exactly ranked pool and may differ from it.

| test | exact value | squad changes | Man Utd triple | Bournemouth double | no Arsenal | goalkeeper pair | captain |
| --- | --- | --- | --- | --- | --- | --- | --- |
| baseline | 448.524 | — | yes | yes | yes | Leno, Palmer | Haaland |
| manchester_united_attack_minus_10_percent | 441.677 | 3 | no | yes | yes | Leno, Palmer | Haaland |
| bournemouth_attack_minus_10_percent | 447.652 | 3 | yes | no | yes | Leno, Palmer | Haaland |
| differentiated_promoted_prior | not run | The differentiated promoted prior was not adopted, so the live model already uses the fixed prior. Running the two against each other here would compare a model with itself; the historical comparison in the promoted-prior section is the evidence. | | | | | |
| promoted_player_role_treatment | not run | The role treatment was not validated — no usable Championship player-level role evidence is available — so there is no second model to run. Reporting a null result here would imply the treatment was tested and found neutral, which is not what happened. | | | | | |

## 10. Final squad

Projection run 2 under `rates-rules-corrected-v4-preseason-carry-forward-promoted-fixed`. Cost £100.0m, bank £0.0m. GW1 expected 55.711, exact GW1–8 decision value 449.495, linear objective 424.094.

| player | club | pos | price | GW1–8 xP | role |
| --- | --- | --- | --- | --- | --- |
| Gabriel | ARS | DEF | £8.0m | 28.729 | XI |
| Mukiele | SUN | DEF | £5.5m | 27.004 | XI |
| Mykolenko | EVE | DEF | £4.5m | 23.363 | XI |
| Evanilson | BOU | FWD | £6.0m | 30.633 | XI |
| Haaland | MCI | FWD | £15.5m | 47.552 | XI (C) |
| Watkins | AVL | FWD | £8.0m | 37.21 | XI |
| Leno | FUL | GK | £4.5m | 32.854 | XI |
| Cunha | MUN | MID | £8.0m | 35.668 | XI |
| Gakpo | LIV | MID | £7.0m | 31.908 | XI |
| Mbeumo | MUN | MID | £8.0m | 38.588 | XI (V) |
| Rayan | BOU | MID | £6.5m | 33.74 | XI |
| Palmer | IPS | GK | £4.0m | 26.348 | bench 1 |
| Robinson | FUL | DEF | £4.5m | 24.587 | bench 2 |
| Truffert | BOU | DEF | £5.5m | 29.789 | bench 3 |
| Dowell | HUL | MID | £4.5m | 17.238 | bench 4 |

## 11. Meaningful alternatives

| alternative | exact value | gap | cost | bank | changes |
| --- | --- | --- | --- | --- | --- |
| alternative_1 | 449.041 | 0.454 | £100.0m | £0.0m | 6 |
| alternative_2 | 449.041 | 0.454 | £100.0m | £0.0m | 6 |
| alternative_3 | 449.041 | 0.454 | £100.0m | £0.0m | 6 |

## 12. Robust and model-sensitive selections

Compared across 6 squads: the recommendation, every completed concentration run and every feasible bank level.

- Robust (in every one): Haaland, Leno, Mbeumo, Palmer, Rayan, Truffert, Watkins
- Moderate: B.Fernandes, Cunha, Destan, Dowell, Evanilson, Gabriel, Gakpo, Iwobi, Mukiele, Mykolenko, O'Shea, Robinson, Thiaw
- Model-sensitive (in exactly one): Davis

## 13. Warnings and unresolved limitations

- The live snapshot did not come from a direct official FPL API capture. Prices, availability and fixtures are as good as the mirror behind them and no better.
- The promoted-player role treatment was not adopted: Coverage is insufficient. The existing player model is kept and Championship role evidence is reported as an audit field only, because applying it to some players and not their direct competitors would distort a squad decision more than leaving the whole cohort on the positional prior.
