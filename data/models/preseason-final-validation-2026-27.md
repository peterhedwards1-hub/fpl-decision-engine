# Preseason final squad — 2026-27

Generated 2026-08-19T20:14:50.413798+00:00. Horizon GW1–GW8. Total runtime 1218.88s.

**This squad is provisional.** It stands until the final reliable pre-deadline team-news rerun, and no further.

## 1. Data coverage and provenance

- Premier League seasons imported: 2021-22, 2022-23, 2023-24, 2024-25, 2025-26, 2026-27
- Usable season transitions: 2021-22->2022-23, 2022-23->2023-24, 2023-24->2024-25, 2024-25->2025-26
- Live snapshot source: `official-fpl-api` retrieved 2026-08-19T17:28:32.928456+00:00
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
- Eligible promoted candidates: 101
- Coverage: 0.0 (minimum 0.6)

Coverage is insufficient. The existing player model is kept and Championship role evidence is reported as an audit field only, because applying it to some players and not their direct competitors would distort a squad decision more than leaving the whole cohort on the positional prior.

## 4. Goalkeeper pair

Every eligible goalkeeper pair is enumerated, each pair's best weekly orientation and exact value computed, and that value carried into the same objective the outfield players are selected under. Goalkeepers appear in the objective once and are excluded from the starter, captain and bench-quality terms, so no goalkeeper's points are counted twice. The nomination is pinned inside the solve, not swapped afterwards.

Appearance states are independent, as everywhere else in the optimiser. Two goalkeepers at the same club, or a first choice and their own understudy, would violate it and the protection would be overstated.

| GW | nominated | pair value | other orientation | uplift over starter alone | lower standalone starts |
| --- | --- | --- | --- | --- | --- |
| 1 | Pickford | 4.539 | 4.146 | 0.238 | no |
| 2 | Leno | 3.906 | 3.65 | 0.9776 | yes |
| 3 | Leno | 4.3 | 3.85 | 1.0269 | yes |
| 4 | Pickford | 3.885 | 3.538 | 0.2029 | no |
| 5 | Pickford | 4.501 | 3.94 | 0.2217 | no |
| 6 | Pickford | 4.108 | 4.044 | 0.2398 | no |
| 7 | Leno | 4.357 | 4.105 | 1.1002 | yes |
| 8 | Leno | 3.823 | 3.338 | 0.8879 | yes |

- Pair chosen: Leno, Pickford
- Chosen when goalkeepers are valued singly: Palmer, Pickford
- Squad changed by the pair treatment: yes
- Total substitution-protection value over the horizon: 4.8949 points
- A Gameweek nominates the lower standalone goalkeeper: yes

Substitution protection is worth P(nominated goalkeeper records no minutes) x the reserve's expected points. When the minutes model puts a first-choice goalkeeper's appearance probability at one, that product is zero and the pair treatment cannot move the selection however it is implemented. The number above says whether that is the case here.

## 5. Eligibility audit

- Priced candidates: 595
- Eligible after the mean-appearance guardrail (0.5): 211
- Excluded: 384

## 6. Candidate frontier

- Requested 40, produced 61 distinct complete squads in 969.55s
- Ranked together with the bank levels and forced inclusions: 62 squads; the recommendation came from `frontier`
- Distinct starting elevens among them: 24
- Exact rescoring reorders the solver's ranking: **yes** (61 of 61 moved; largest move 55)
- Exact rescoring changes the winner: **yes**
- Balanced staged convergence (every family expanded per stage): **reached** — Practical convergence reached: the final expansions of every family produced no new winning squad and no material improvement.

## 7. Bank frontier

Money in the bank is never given a points value. A bank-preserving squad within 0.25 expected points over the horizon is reported as flexibility-equivalent, and does not replace the maximum-value primary squad: equivalent within noise is not better.

| minimum bank | exact GW1–8 value | GW1 value | cost | bank | changes | value sacrificed | flexibility-equivalent |
| --- | --- | --- | --- | --- | --- | --- | --- |
| £0.0m | 430.403 | 53.86 | £100.0m | £0.0m | 0 | 0.0 | yes |
| £0.5m | 420.238 | 52.982 | £99.5m | £0.5m | 2 | 10.165 | no |
| £1.0m | 420.235 | 53.019 | £99.0m | £1.0m | 1 | 10.168 | no |

## 8. Arsenal defender counterfactuals

- Eligible Arsenal defenders: 4
- Excluded by the availability guardrail: 3

| defender | price | mean appearance | GW1 xP | GW1–8 xP | forced squad value | value gap | squad changes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| White | £5.5m | 0.659 | 2.98 | 21.136 | 422.148 | 8.255 | 2 |
| Gabriel | £8.0m | 0.7788 | 3.955 | 27.899 | 417.076 | 13.327 | 3 |
| Calafiori | £5.5m | 0.5143 | 1.751 | 12.429 | 414.75 | 15.653 | 2 |
| Hincapie | £5.5m | 0.5969 | 2.301 | 16.443 | 417.978 | 12.425 | 2 |

Best squad containing any Arsenal defender: White at 422.148 (8.255 behind the recommendation)

**Verdict: not_absent.** The recommended squad contains 1 ARS player(s) — Ødegaard — so there is no absence to explain. The counterfactual table below still reports what forcing each defender in costs.

## 9. Concentration tests

Single factors only. No combination of two perturbations is run: with one opening decision per season there is no way to tell a real interaction from a coincidence.

The baseline is the unconstrained single solve under the live model, because every perturbed run is one too. The recommended squad is chosen from a wider exactly ranked pool and may differ from it.

| test | exact value | squad changes | Man Utd triple | Bournemouth double | no Arsenal | goalkeeper pair | captain |
| --- | --- | --- | --- | --- | --- | --- | --- |
| baseline | 417.076 | — | no | yes | no | Leno, Pickford | Haaland |
| manchester_united_attack_minus_10_percent | 414.51 | 4 | no | no | no | Leno, Pickford | Haaland |
| bournemouth_attack_minus_10_percent | 419.692 | 5 | no | no | no | Leno, Pickford | Haaland |
| differentiated_promoted_prior | not run | The differentiated promoted prior was not adopted, so the live model already uses the fixed prior. Running the two against each other here would compare a model with itself; the historical comparison in the promoted-prior section is the evidence. | | | | | |
| promoted_player_role_treatment | not run | The role treatment was not validated — no usable Championship player-level role evidence is available — so there is no second model to run. Reporting a null result here would imply the treatment was tested and found neutral, which is not what happened. | | | | | |

## 10. Final squad

Projection run 26 under `rates-rules-corrected-v4-preseason-carry-forward-v2-promoted-fixed`. Cost £100.0m, bank £0.0m. GW1 expected 53.86, exact GW1–8 decision value 430.403, linear objective 378.809.

| player | club | pos | price | GW1–8 xP | role |
| --- | --- | --- | --- | --- | --- |
| Gvardiol | MCI | DEF | £5.5m | 22.807 | XI |
| Thiaw | NEW | DEF | £5.0m | 24.234 | XI |
| Truffert | BOU | DEF | £5.5m | 26.239 | XI |
| Haaland | MCI | FWD | £15.5m | 44.303 | XI (C) |
| Watkins | AVL | FWD | £8.0m | 34.831 | XI |
| Pickford | EVE | GK | £5.5m | 30.061 | XI (V) |
| Cunha | MUN | MID | £8.0m | 30.76 | XI |
| Foden | MCI | MID | £7.0m | 29.056 | XI |
| Mbeumo | MUN | MID | £8.0m | 33.182 | XI |
| Rayan | BOU | MID | £6.5m | 27.167 | XI |
| Ødegaard | ARS | MID | £6.5m | 26.362 | XI |
| Leno | FUL | GK | £4.5m | 23.438 | bench 1 |
| Evanilson | BOU | FWD | £6.0m | 24.186 | bench 2 |
| O'Shea | IPS | DEF | £4.0m | 15.603 | bench 3 |
| Mitchell | CRY | DEF | £4.5m | 20.752 | bench 4 |

## 11. Meaningful alternatives

| alternative | exact value | gap | cost | bank | changes |
| --- | --- | --- | --- | --- | --- |
| alternative_1 | 429.157 | 1.246 | £100.0m | £0.0m | 1 |
| alternative_2 | 428.831 | 1.572 | £100.0m | £0.0m | 3 |
| alternative_3 | 423.501 | 6.902 | £100.0m | £0.0m | 3 |

## 12. Robust and model-sensitive selections

Compared across 6 squads: the recommendation, every completed concentration run and every feasible bank level.

- Robust (in every one): Foden, Haaland, Leno, Mbeumo, O'Shea, Pickford, Thiaw, Truffert, Watkins, Ødegaard
- Moderate: Cunha, Evanilson, Guéhi, Gvardiol, Hirst, Mitchell, Rayan
- Model-sensitive (in exactly one): Davis, Gakpo, Saka

## 13. Warnings and unresolved limitations

- The promoted-player role treatment was not adopted: Coverage is insufficient. The existing player model is kept and Championship role evidence is reported as an audit field only, because applying it to some players and not their direct competitors would distort a squad decision more than leaving the whole cohort on the positional prior.
