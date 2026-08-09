# Preseason final squad — 2026-27

Generated 2026-08-08T19:34:36.301601+00:00. Horizon GW1–GW8. Total runtime 11738.2s.

**This squad is provisional.** It stands until the final reliable pre-deadline team-news rerun, and no further.

## 1. Data coverage and provenance

- Premier League seasons imported: 2021-22, 2022-23, 2023-24, 2024-25, 2025-26, 2026-27
- Usable season transitions: 2021-22->2022-23, 2022-23->2023-24, 2023-24->2024-25, 2024-25->2025-26
- Live snapshot source: `official-fpl-api` retrieved 2026-08-05T22:54:28.381216+00:00
- Direct official API capture: yes

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

Live priors for 2026-27 (Championship 2025-26, cohort mean attack 0.85 against a declared 0.85):

| club | Championship attack relative | defence relative | attack multiplier | defence multiplier |
| --- | --- | --- | --- | --- |
| Coventry City | 1.6189 | 0.751 | 0.85 | 1.2 |
| Hull City | 1.1683 | 1.1015 | 0.85 | 1.2 |
| Ipswich Town | 1.3352 | 0.7844 | 0.85 | 1.2 |

## 3. Promoted-player role evidence

- Treatment applied: **none**
- Stored Championship role rows: 0
- Eligible promoted candidates: 87
- Coverage: 0.0 (minimum 0.6)

Coverage is insufficient. The existing player model is kept and Championship role evidence is reported as an audit field only, because applying it to some players and not their direct competitors would distort a squad decision more than leaving the whole cohort on the positional prior.

## 4. Goalkeeper pair

Every eligible goalkeeper pair is enumerated, each pair's best weekly orientation and exact value computed, and that value carried into the same objective the outfield players are selected under. Goalkeepers appear in the objective once and are excluded from the starter, captain and bench-quality terms, so no goalkeeper's points are counted twice. The nomination is pinned inside the solve, not swapped afterwards.

Appearance states are independent, as everywhere else in the optimiser. Two goalkeepers at the same club, or a first choice and their own understudy, would violate it and the protection would be overstated.

| GW | nominated | pair value | other orientation | uplift over starter alone | lower standalone starts |
| --- | --- | --- | --- | --- | --- |
| 1 | Raya | 4.885 | 4.084 | 0.9029 | yes |
| 2 | Raya | 4.288 | 4.104 | 0.9073 | yes |
| 3 | Raya | 4.68 | 4.588 | 1.0143 | yes |
| 4 | Raya | 4.456 | 3.481 | 0.7696 | no |
| 5 | Raya | 4.305 | 3.804 | 0.841 | yes |
| 6 | Raya | 4.748 | 4.114 | 0.9095 | yes |
| 7 | Leno | 4.565 | 4.559 | 0.0 | no |
| 8 | Raya | 4.787 | 4.114 | 0.9095 | yes |

- Pair chosen: Leno, Lo-Tutala
- Chosen when goalkeepers are valued singly: Leno, Palmer
- Squad changed by the pair treatment: yes
- Total substitution-protection value over the horizon: 0.0 points
- A Gameweek nominates the lower standalone goalkeeper: no

Substitution protection is worth P(nominated goalkeeper records no minutes) x the reserve's expected points. When the minutes model puts a first-choice goalkeeper's appearance probability at one, that product is zero and the pair treatment cannot move the selection however it is implemented. The number above says whether that is the case here.

## 5. Eligibility audit

- Priced candidates: 570
- Eligible after the mean-appearance guardrail (0.6): 242
- Excluded: 328

## 6. Candidate frontier

- Requested 500, produced 856 distinct complete squads in 11621.0s
- Ranked together with the bank levels and forced inclusions: 860 squads; the recommendation came from `frontier`
- Distinct starting elevens among them: 96
- Exact rescoring reorders the solver's ranking: **yes** (856 of 856 moved; largest move 849)
- Exact rescoring changes the winner: **yes**

## 7. Bank frontier

Money in the bank is never given a points value. A bank-preserving squad within 0.25 expected points over the horizon is reported as flexibility-equivalent, and does not replace the maximum-value primary squad: equivalent within noise is not better.

| minimum bank | exact GW1–8 value | GW1 value | cost | bank | changes | value sacrificed | flexibility-equivalent |
| --- | --- | --- | --- | --- | --- | --- | --- |
| £0.0m | 455.106 | 56.774 | £100.0m | £0.0m | 0 | 0.0 | yes |
| £0.5m | 446.471 | 55.148 | £99.5m | £0.5m | 5 | 8.635 | no |
| £1.0m | 445.857 | 55.035 | £99.0m | £1.0m | 4 | 9.249 | no |

## 8. Arsenal defender counterfactuals

- Eligible Arsenal defenders: 2
- Excluded by the availability guardrail: 5

| defender | price | mean appearance | GW1 xP | GW1–8 xP | forced squad value | value gap | squad changes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| White | £5.5m | 0.6264 | 2.897 | 20.419 | 446.737 | 8.369 | 5 |
| Gabriel | £8.0m | 0.7827 | 4.1 | 28.729 | 449.239 | 5.867 | 4 |

Best squad containing any Arsenal defender: Gabriel at 449.239 (5.867 behind the recommendation)

**Verdict: not_absent.** The recommended squad contains 1 ARS player(s) — Raya — so there is no absence to explain. The counterfactual table below still reports what forcing each defender in costs.

## 9. Concentration tests

Single factors only. No combination of two perturbations is run: with one opening decision per season there is no way to tell a real interaction from a coincidence.

The baseline is the unconstrained single solve under the live model, because every perturbed run is one too. The recommended squad is chosen from a wider exactly ranked pool and may differ from it.

| test | exact value | squad changes | Man Utd triple | Bournemouth double | no Arsenal | goalkeeper pair | captain |
| --- | --- | --- | --- | --- | --- | --- | --- |
| baseline | 447.796 | — | yes | yes | yes | Leno, Lo-Tutala | Haaland |
| manchester_united_attack_minus_10_percent | 441.418 | 5 | no | yes | yes | Leno, Palmer | Haaland |
| bournemouth_attack_minus_10_percent | 447.063 | 4 | yes | no | yes | Leno, Palmer | Haaland |
| differentiated_promoted_prior | not run | The differentiated promoted prior was not adopted, so the live model already uses the fixed prior. Running the two against each other here would compare a model with itself; the historical comparison in the promoted-prior section is the evidence. | | | | | |
| promoted_player_role_treatment | not run | The role treatment was not validated — no usable Championship player-level role evidence is available — so there is no second model to run. Reporting a null result here would imply the treatment was tested and found neutral, which is not what happened. | | | | | |

## 10. Final squad

Projection run 18 under `rates-rules-corrected-v4-preseason-carry-forward-promoted-fixed`. Cost £100.0m, bank £0.0m. GW1 expected 56.774, exact GW1–8 decision value 455.106, linear objective 424.92.

| player | club | pos | price | GW1–8 xP | role |
| --- | --- | --- | --- | --- | --- |
| Mukiele | SUN | DEF | £5.5m | 27.004 | XI |
| Mykolenko | EVE | DEF | £4.5m | 23.363 | XI |
| Thiaw | NEW | DEF | £5.0m | 26.477 | XI |
| Haaland | MCI | FWD | £15.5m | 47.552 | XI (C) |
| Watkins | AVL | FWD | £8.0m | 36.963 | XI |
| Raya | ARS | GK | £6.0m | 29.444 | XI |
| Cunha | MUN | MID | £8.0m | 35.668 | XI |
| Foden | MCI | MID | £7.0m | 30.867 | XI |
| Iwobi | FUL | MID | £5.5m | 28.491 | XI |
| Mbeumo | MUN | MID | £8.0m | 38.588 | XI (V) |
| Rayan | BOU | MID | £6.5m | 33.74 | XI |
| Leno | FUL | GK | £4.5m | 32.854 | bench 1 |
| Evanilson | BOU | FWD | £6.0m | 30.633 | bench 2 |
| Robinson | FUL | DEF | £4.5m | 24.587 | bench 3 |
| Truffert | BOU | DEF | £5.5m | 29.789 | bench 4 |

## 11. Meaningful alternatives

| alternative | exact value | gap | cost | bank | changes |
| --- | --- | --- | --- | --- | --- |
| alternative_1 | 450.874 | 4.232 | £100.0m | £0.0m | 5 |
| alternative_2 | 450.576 | 4.53 | £100.0m | £0.0m | 4 |
| alternative_3 | 450.405 | 4.701 | £100.0m | £0.0m | 4 |

## 12. Robust and model-sensitive selections

Compared across 6 squads: the recommendation, every completed concentration run and every feasible bank level.

- Robust (in every one): Haaland, Leno, Mbeumo, Rayan, Truffert, Watkins
- Moderate: B.Fernandes, Cunha, Destan, Dowell, Evanilson, Foden, Iwobi, Mukiele, Mykolenko, O'Shea, Palmer, Phillips, Raya, Robinson, Thiaw
- Model-sensitive (in exactly one): Ajayi, Gakpo

## 13. Warnings and unresolved limitations

- The promoted-player role treatment was not adopted: Coverage is insufficient. The existing player model is kept and Championship role evidence is reported as an audit field only, because applying it to some players and not their direct competitors would distort a squad decision more than leaving the whole cohort on the positional prior.
