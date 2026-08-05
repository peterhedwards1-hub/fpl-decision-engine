# Opening-squad candidate search — 2026-27

Generated 2026-08-05T21:18:41.903398+00:00. Horizon 8 Gameweeks. Total runtime 4629.48s.

**Optimiser validation only.** No live squad is declared from this run. The squad in section 6 is the best the search found, reported as evidence about the search.

## 1. What went wrong

The previous frontier generated forty distinct complete squads and every one fielded the same eleven in all eight Gameweeks. The cause is in the objective, not in the code: the linear objective prices a legal eleven and its captain, and bench players appear in it nowhere. Every squad sharing a weekly XI and completing itself with any affordable legal reserves therefore has *exactly* the same objective value — all forty scored 429.962 — so excluding complete fifteens walked a tie set of interchangeable £4.0m fodder, none of whom ever started.

That also explains the two symptoms that looked like separate problems. The linear ranking was pure tie-break noise, so "exact rescoring reordered 39 of 40" was never evidence that the two objectives disagree about structure. And the exact winner sat *below* the linear optimum, which exclusion cannot reach at all; it turned up only because a forced-inclusion diagnostic happened to solve a differently constrained problem.

## 2. Where the exact-minus-linear gap comes from

Exact value adds four things the linear objective omits: outfield autosub activation, the goalkeeper substitution, bench order and the vice-captain fallback. Decomposed Gameweek by Gameweek for the two squads that mattered, the gap is almost entirely outfield autosubs, and — crucially — it is not a constant:

| squad | linear XI+captain | exact | uplift | outfield autosub | goalkeeper | vice fallback |
| --- | --- | --- | --- | --- | --- | --- |
| previous frontier best | 428.097 | 449.041 | 20.944 | 10.787 | 0.000 | 10.157 |
| previous exact winner (forced-Gabriel) | 423.779 | 449.495 | 25.716 | 16.294 | 0.000 | 9.422 |

The winner gave up 4.3 linear points and bought 5.5 points of autosub value. No weekly rotation, terminal value or appearance re-estimation is involved, and the goalkeeper term was zero in both because the nominated goalkeeper's appearance probability was one — which turns out to be a property of those two squads rather than of the model.

## 3. Search comparison

| strategy | squads | distinct XIs | distinct GK pairs | distinct linear values | best exact | runtime |
| --- | --- | --- | --- | --- | --- | --- |
| legacy_8 | 8 | 1 | 3 | 1 | 447.863 | 38.28s |
| frontier_40 | 40 | 1 | 3 | 1 | 448.86 | 169.27s |
| mixed | 192 | 61 | 14 | 68 | 455.106 | 2078.73s |

- Exact value gained over the forty-candidate frontier: **6.246**
- Exact value gained over the eight-candidate frontier: **7.243**
- The mixed pool contains every squad the older searches found: {'legacy_8': True, 'frontier_40': True}

## 4. The combined pool

- Raw candidates 305, unique complete squads 192
- Distinct starting elevens 61, distinct goalkeeper pairs 14
- Exact-minus-linear uplift ranges 18.44 to 36.755 (spread 18.315)
- Widest declared slack band 16.0; covers the observed uplift spread: **no**
- Generation runtime 1963.98s

| family | candidates first found here | candidates reachable |
| --- | --- | --- |
| A0_seeded | 40 | 40 |
| A_complete_squads | 10 | 50 |
| B_distinct_xis | 33 | 50 |
| C_slack_bands | 31 | 39 |
| D_forced | 50 | 65 |
| E_structural | 18 | 25 |
| F_perturbations | 10 | 23 |

## 5. Convergence

The search has NOT converged by the declared criterion. The best squad below is the best found, not the best that exists.

| pool size | best exact | winner changed | improvement | distinct XIs | winning family |
| --- | --- | --- | --- | --- | --- |
| 40 | 448.86 | no | None | 1 | A0_seeded |
| 100 | 450.354 | yes | 1.494 | 13 | C_slack_bands |

Practical convergence only. No claim of global nonlinear optimality is made or implied: the exact objective is not the one the solver optimises, so no solver proof covers it.

## 6. Forced-diagnostic escape test

30 forced runs. No forced-player diagnostic beat the pool's own winner from outside it.

## 7. Best squad found (optimiser-validation result only)

Exact GW1–8 value **455.106**, linear objective 424.92, uplift 36.755. Cost £100.0m. Found by `slack_8_reserve` in family `C_slack_bands`.

Goalkeeper pair Raya, Leno with 6.254 points of substitution protection. Captain Haaland, vice Mbeumo.

| player | club | pos | price | GW1–8 xP | GW1 |
| --- | --- | --- | --- | --- | --- |
| Mukiele | SUN | DEF | £5.5m | 27.004 | XI |
| Mykolenko | EVE | DEF | £4.5m | 23.363 | XI |
| Thiaw | NEW | DEF | £5.0m | 26.477 | XI |
| Haaland | MCI | FWD | £15.5m | 47.552 | XI |
| Watkins | AVL | FWD | £8.0m | 36.963 | XI |
| Raya | ARS | GK | £6.0m | 29.444 | XI |
| Cunha | MUN | MID | £8.0m | 35.668 | XI |
| Foden | MCI | MID | £7.0m | 30.867 | XI |
| Iwobi | FUL | MID | £5.5m | 28.491 | XI |
| Mbeumo | MUN | MID | £8.0m | 38.588 | XI |
| Rayan | BOU | MID | £6.5m | 33.74 | XI |
| Robinson | FUL | DEF | £4.5m | 24.587 | bench |
| Truffert | BOU | DEF | £5.5m | 29.789 | bench |
| Evanilson | BOU | FWD | £6.0m | 30.633 | bench |
| Leno | FUL | GK | £4.5m | 32.854 | bench |

## 8. Historical comparison

| season | legacy_8 exact | frontier_40 exact | mixed exact | gain | mixed XIs | legacy realised | frontier realised | mixed realised |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 2022-23 | 379.528 | 379.528 | 379.528 | 0.0 | 14 | 394.0 | 394.0 | 394.0 |
| 2023-24 | 441.915 | 442.838 | 442.838 | 0.0 | 12 | 474.0 | 474.0 | 474.0 |
| 2024-25 | 445.608 | 445.192 | 445.608 | 0.416 | 11 | 469.0 | 422.0 | 469.0 |
| 2025-26 | 419.877 | 419.877 | 419.877 | 0.0 | 22 | 294.0 | 294.0 | 294.0 |

Realised points are one opening squad per season — four draws from a wide distribution. They are reported because they were asked for and discounted because four observations cannot separate two search strategies.

## 9. Acceptance

| criterion | result |
| --- | --- |
| contains_everything_the_old_search_found | True |
| no_forced_diagnostic_escape | False |
| meaningful_starting_xi_diversity | True |
| convergence_reported_for_the_live_season | True |
| live_search_converged | False |
| live_runtime_seconds | 2286.3 |
| passed | False |

## 10. Exact-objective feasibility

**Implemented — tighter linear surrogate, used for generation only.** Reserve quality — every squad member's expected points in the Gameweeks they do not start — maximised inside a declared slack band on the primary objective. It is the linear quantity most closely correlated with the autosub value the primary objective omits, and it is already built for the existing bench tie-break, so nothing new had to be modelled. It is the family that finds the exact winner on the live 2026/27 candidate set. Generation only. It never enters the exact decision value and cannot affect a ranking.

**Not taken — an exact reserve term in the solver objective.** The exact autosub contribution is a sum over the joint appearance states of ten outfield starters and three outfield substitutes — 8192 states — where which substitutes activate depends on which starters blanked and on formation legality after the swap. It is not a linear function of the selection variables and linearising it would need a variable per state per Gameweek. *Not possible without a major optimiser rewrite.*

**Not taken — precomputed legal lineup and bench configurations.** Enumerating legal elevens for a fixed fifteen is cheap — a few hundred — but the exact weekly score for each one still costs a joint-state integration, and the outer problem is choosing the fifteen from hundreds of players, not the eleven from fifteen. *Useful only after selection, which is where it is already used.*

**Not taken — decomposition or column generation.** A Dantzig-Wolfe or branch-and-price formulation with squads as columns and the exact value as the column cost would price the right objective. The pricing subproblem is the nonlinear part and would need its own approximation, and the whole thing replaces the optimiser. *Recorded as later work. It is the principled fix and it is out of scope here.*

Until one of those lands, the search is a generate-and-score procedure and no solver proof covers the exact objective. Nothing in this module claims global nonlinear optimality.
