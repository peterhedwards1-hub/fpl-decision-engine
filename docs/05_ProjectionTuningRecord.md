# Projection tuning record — 2025/26

**Study date:** 2026-07-29  
**Study name:** `fpl-rates-two-stage-v2`  
**Status:** Candidate selected for promotion; not yet the production default

## Purpose

The study tested whether the transparent rates projection model could be improved by
tuning its scoring-rate shrinkage, team strength, recent-form weighting and two-stage
expected-minutes parameters. The search objective emphasised accuracy among the top 100
projected players because those rankings drive squad, transfer and captaincy decisions.

The work added:

- a leakage-controlled walk-forward projection backtest;
- a two-stage expected-minutes model that estimates appearance probability separately
  from minutes conditional on appearing;
- a 990-player-minute-per-team constraint for every fixture;
- persisted prediction-level results, configurations, source revisions and fingerprints;
- Optuna-based reproducible hyperparameter search;
- separate development and held-out validation windows;
- failure cleanup so an unsuccessful trial cannot retain partial prediction rows.

## Data actually used

The operational database contained these seasons at study time:

| Season | Players | Fixtures | Player-fixture rows |
|---|---:|---:|---:|
| 2022/23 | 778 | 380 | 26,505 |
| 2023/24 | 865 | 380 | 29,725 |
| 2025/26 | 841 | 380 | 29,747 |

This was **not a five-season evaluation**.

The tuning targets and scored outcomes came only from 2025/26:

- development window: GW2–25;
- untouched validation window: GW26–38;
- forecast horizon: one Gameweek;
- completed candidate trials: 30;
- failed trials: one initial infrastructure failure, retained in the Optuna audit log.

For a 2025/26 forecast, the model's career-rate calculation can use linked player history
from the imported 2022/23 and 2023/24 seasons, as well as 2025/26 results strictly before
the forecast origin. Recent-form and team-strength inputs use only prior Gameweeks from
the target season. The database did not contain 2021/22 or 2024/25, so the model did not
use the last five completed seasons.

The historical source does not contain trustworthy pre-deadline availability snapshots
or a timestamped schedule archive. The study therefore used the `performance_only`
evidence policy, ignored historically reconstructed availability fields and disclosed
that the final fixture slate was used.

## Selected candidate

Trial 13 achieved the best development objective:

```json
{
  "player_rate_prior_minutes": 1776.650037050099,
  "minutes_prior_matches": 6.0,
  "team_prior_matches": 11.870184562035677,
  "home_attack_multiplier": 1.0680722197944925,
  "away_attack_multiplier": 0.8512934695622035,
  "minimum_team_multiplier": 0.6,
  "maximum_team_multiplier": 1.5,
  "minutes_model": "two_stage",
  "recent_gameweeks": 4,
  "recent_evidence_weight": 1.845760710001814,
  "appearance_prior_matches": 3.2516466478759654,
  "appearance_prior_probability": 0.4044943812940328,
  "conditional_minutes_prior_appearances": 1.248676119370052,
  "team_minutes_per_fixture": 990.0,
  "enforce_team_minutes": true
}
```

Trials 29 and 30 produced the next-best development scores and shared the broad pattern
of stronger scoring-rate shrinkage, modest recent-form weighting and a low away-attack
multiplier. This provides some evidence that trial 13 was not an isolated result.

## Held-out result

Negative changes below mean lower error and therefore improvement. Bias is actual minus
expected points, so a negative bias indicates overprojection.

| Held-out GW26–38 metric | Untuned v2 | Trial 13 | Change | Relative result |
|---|---:|---:|---:|---:|
| Overall points MAE | 1.3719 | 1.3173 | -0.0546 | 4.0% better |
| Overall points RMSE | 2.2536 | 2.1492 | -0.1044 | 4.6% better |
| Overall absolute points bias | 0.6171 | 0.4688 | -0.1483 | 24.0% better |
| Top-100 points MAE | 3.7036 | 3.3290 | -0.3746 | 10.1% better |
| Top-100 absolute points bias | 2.0995 | 1.3892 | -0.7103 | 33.8% better |
| Top-50 points MAE | 4.2603 | 3.7193 | -0.5410 | 12.7% better |
| Top-15 points MAE | 5.3089 | 4.3138 | -0.9951 | 18.7% better |
| Minutes MAE | 19.7349 | 21.0624 | +1.3275 | 6.7% worse |

The candidate improved all recorded decision-facing points metrics. Its tradeoff was less
accurate allocation of minutes between players. Total expected player-minutes remained
physically consistent at approximately 1,980 per match.

## Decision and safeguards

Trial 13 is recommended for promotion under a new model version rather than silently
changing `rates-two-stage-v2`. The existing v2 configuration and all backtest records
must remain available for reproducibility.

The GW26–38 validation window has now been inspected and must not be repeatedly used to
choose additional variants. Doing so would turn it into tuning data. The next stronger
qualification should use other seasons or future genuinely unseen results.

To claim five-season evaluation, the project must:

1. import the missing 2021/22 and 2024/25 data;
2. verify cross-season player identity links and season-specific scoring rules;
3. evaluate with rolling season or expanding-window folds rather than pooling future
   observations into earlier forecasts;
4. retain at least one final season or future period untouched for model selection;
5. compare projection accuracy and downstream squad/transfer regret across folds.

Until then, the accurate description is: **three seasons were available as potential
historical evidence, while model selection was evaluated on one season with a held-out
13-Gameweek period.**
