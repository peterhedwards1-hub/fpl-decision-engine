# Preseason team strength — 2026-27

Generated 2026-08-04T22:15:18.005176+00:00. Horizon 8 Gameweeks.

## Verdict

- Decision gate: **PASS**
- Selected preseason model: **carry_forward** (`rates-rules-corrected-v4-preseason-carry-forward`)
- Scope: preseason opening-squad decision only
- Usable transitions: 2021-22->2022-23, 2022-23->2023-24, 2023-24->2024-25, 2024-25->2025-26

## Aggregate early-season accuracy (GW1–GW8)

| model | goals RMSE | goals MAE | goals bias | CS Brier |
| --- | --- | --- | --- | --- |
| flat | 1.2611 | 0.9583 | -0.0848 | 0.1853 |
| carry_forward | 1.1646 | 0.8945 | -0.0027 | 0.172 |
| opponent_adjusted | 1.1718 | 0.8969 | 0.011 | 0.1731 |

## Decision gate

| criterion | passed | detail |
| --- | --- | --- |
| improves_early_team_goal_error | yes | Aggregate GW1-GW8 team-goal RMSE or MAE improves on the flat control. |
| clean_sheet_brier_not_materially_worse | yes | Clean-sheet Brier score does not worsen by more than 0.005. |
| opening_squad_decision_not_worse | yes | Mean realised GW1-GW8 opening-squad points improve, or fall by no more than 0.5 per season; or squad regret improves. |
| acceptable_across_multiple_transitions | yes | The candidate performs acceptably on more than one usable season transition. |
| separates_established_from_promoted | yes | The candidate gives established and promoted clubs different preseason strengths, which the flat control structurally cannot. |
| no_severe_new_calibration_defect | yes | Absolute goal bias stays below 0.35, and the clean-sheet Brier score does not worsen materially. |

The candidate counts as effectively neutral on the decision measure when it gives up no more than 0.5 realised points over GW1-GW8 per historical season. This threshold was declared before any decision-level result was read.

## Revised opening squad

Cost 100.0m. GW1 expected 54.934. Eight-Gameweek decision value 450.032. Validated: True.

| player | club | pos | price | GW1–8 xP | role |
| --- | --- | --- | --- | --- | --- |
| Davis | IPS | DEF | 4.0 | 18.518 | XI |
| O'Shea | IPS | DEF | 4.0 | 19.641 | XI |
| Thiaw | NEW | DEF | 5.0 | 27.46 | XI |
| Evanilson | BOU | FWD | 6.0 | 31.234 | XI |
| Haaland | MCI | FWD | 15.5 | 47.552 | XI (C) |
| Watkins | AVL | FWD | 8.0 | 37.21 | XI |
| Leno | FUL | GK | 4.5 | 32.854 | XI |
| B.Fernandes | MUN | MID | 12.0 | 43.027 | XI (V) |
| Cunha | MUN | MID | 8.0 | 35.668 | XI |
| Mbeumo | MUN | MID | 8.0 | 38.588 | XI |
| Rayan | BOU | MID | 6.5 | 33.875 | XI |
| Palmer | IPS | GK | 4.0 | 26.348 | bench 1 |
| Robinson | FUL | DEF | 4.5 | 24.587 | bench 2 |
| Crooks | HUL | MID | 4.5 | 16.243 | bench 3 |
| Truffert | BOU | DEF | 5.5 | 29.789 | bench 4 |

## Truffert, O'Shea and Muñoz

### Muñoz (CRY, DEF)

- GW1 fixture: away at EVE
- Opponent expected goals: flat 1.4953 → carry-forward 1.3318
- Clean-sheet probability: flat 0.2242 → carry-forward 0.264
- GW1–8 projection: flat 26.336 → carry-forward 25.822 (-0.514)
- In squad: flat True → carry-forward False; change attributed to team_strength

### O'Shea (IPS, DEF)

- GW1 fixture: at home to SUN
- Opponent expected goals: flat 1.1918 → carry-forward 1.1952
- Clean-sheet probability: flat 0.3037 → carry-forward 0.3026
- GW1–8 projection: flat 23.292 → carry-forward 19.641 (-3.651)
- In squad: flat True → carry-forward True; change attributed to team_strength

### Truffert (BOU, DEF)

- GW1 fixture: away at MCI
- Opponent expected goals: flat 1.4953 → carry-forward 2.0481
- Clean-sheet probability: flat 0.2242 → carry-forward 0.129
- GW1–8 projection: flat 30.884 → carry-forward 29.789 (-1.095)
- In squad: flat True → carry-forward True; change attributed to team_strength

## Robustness

- Runs: 4
- Objective spread: 9.402
- Captaincy changes across runs: 0
- Classification: {'robust': 14, 'moderate': 1, 'model_sensitive': 2}
