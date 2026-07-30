# Component models and forward qualification

**Implementation date:** 2026-07-30  
**Production incumbent:** `rates-rules-corrected-v4`  
**Database schema:** 15

## Outcome

The remainder of the modelling plan is implemented as challenger and evaluation
infrastructure. No historical result promoted a model. Three candidates were registered
before 2026/27 GW1 outcomes:

| Candidate | Registered model version | Purpose |
|---|---|---|
| `playing-time-hurdle-logistic-v1` | `rates-playing-time-hurdle-logistic-v1` | Appearance/start/60/conditional minutes |
| `team-share-xg-v1` | `rates-team-share-xg-v1` | Coherent team xG and player shares |
| `defensive-empirical-v1` | `rates-defensive-empirical-v1` | Forward DC hit-rate calibration |

Their canonical configs and common gate policy live in
`config/model_candidates/`. Schema 15 stores the registration time, config SHA-256 and
policy immutably.

The first valid GW1 candidate projection runs were generated from ingestion run 6:
projection runs 1 (playing time), 2 (share xG) and 3 (defensive contributions), each with
4,512 player-Gameweek rows across the eight-Gameweek horizon.

## Stage 2 evidence

Both families train four separate targets and calibrate classifiers only on strictly
chronological OOF predictions. Historical availability is excluded because its timing is
unknown.

2024/25 design results:

| Metric | Existing empirical hurdle | Logistic | Histogram gradient |
|---|---:|---:|---:|
| Appearance Brier | 0.123705 | **0.099424** | 0.095949 |
| Appearance log loss | 0.411050 | **0.327619** | 0.316565 |
| 60-minute Brier | 0.114158 | **0.092240** | 0.090243 |
| Expected-minutes RMSE | 28.0331 | **24.0093** | 23.6509 |
| Expected-minutes bias | -10.3028 | **-0.5325** | -0.8704 |
| Sensitivity points RMSE change | — | **-0.1441** | -0.1496 |
| Global top-one regret change | — | **+0.3243** | +0.5676 |
| Top-15 regret change | — | **-9.6216** | -8.6757 |

Logistic was selected for forward testing because its decision trade-off was better and
its artifact is smaller and more interpretable. The points figures are component-backed
sensitivities, not a full historical rerun with the learned model.

Artifacts:

- `data/models/playing-time-hurdle-logistic-v1.json`
- `data/models/playing-time-hurdle-hgb-v1.json`

The selected logistic joblib is small and retained so the registered forward candidate
is runnable from a clean checkout. The larger, unselected HGB joblib is reproducible and
ignored by Git.

## Stage 3 evidence

`team_share_expected` removes the old double-count:

1. exponentially decay and shrink team xG for/against;
2. opponent-adjust the fixture team expectation;
3. estimate player goal and assist weights;
4. normalise them within each club;
5. allocate the team total and apply configured scoring rules.

Tests enforce sum-to-one goal shares and reconciliation to team xG. On 2024/25, run 364
changed incumbent RMSE from 2.0885 to 2.0945, while bias improved from -0.0407 to
+0.0141. Top-100 bias worsened from +0.1104 to +0.4510 actual-minus-predicted. This
design therefore remains unpromoted.

The component oracles are more useful than another broad search:

- model RMSE: 2.0604 on single-fixture component rows;
- actual team-goal RMSE: 2.0023;
- actual player-goal/assist RMSE: 1.3352.

This indicates that player allocation/rates have much more remaining value than team goal
totals.

## Stage 4

`simulation.py` supplies:

- shared Poisson fixture scorelines;
- hurdle appearance/minutes draws;
- conditional scorer/assister allocation;
- Gamma-Poisson defensive counts;
- shared clean sheets and goals conceded;
- configured FPL player scoring;
- exact autosubs, captain fallback, Bench Boost and Triple Captain;
- squad distributions and pairwise win probabilities;
- empirical CRPS, 50/80/95% coverage, threshold Brier scores and PIT bins.

`ensemble.py` fits a deliberately small least-squares ensemble with non-negative weights
that sum to one. Every training row declares each component model's training cutoff; a
cutoff at or after the target period is rejected as leakage.

These implementations produce the required objects but are not called calibrated until
forward outcomes demonstrate proper-score and coverage performance.

## Decision layer

- `replay_transfer_continuity` carries the model-owned squad, bank and free transfers
  through consecutive Gameweeks, deducts hits, and scores the forecast lineup with exact
  autosubs and captain fallback.
- `free_transfer_option_value` prices expected future hits avoided from an empirical
  transfer-need distribution. `empirical_transfer_need_distribution` learns that
  distribution from recorded actual actions and fails when the sample is insufficient.
- `recommend_chip_timing` evaluates every supplied future Gameweek and computes each
  option against the best remaining later opportunity.

The transfer comparator is same-state, one-Gameweek hindsight rather than a globally
clairvoyant season policy. Selling-price profit history is not reconstructed in the
replay; supplied origin prices are used.

## Forward gate

`evaluate-forward-candidate` requires:

- an immutable pre-registration;
- the registered model version and exact canonical config;
- matched incumbent/challenger scope;
- completed `pre_deadline_only` runs;
- a target deadline after registration;
- a forward season;
- minimum overall and every-position samples;
- RMSE improvement with a paired moving-block 95% upper bound at or below zero;
- bounded bias, Brier and log-loss changes;
- non-worsening global top-one and top-15 regret;
- supplied legal-squad, owned-captain and continuous-transfer evidence.

An accumulating failure leaves the candidate declared. `--finalize-failure` is required
to make rejection permanent; passing all gates marks it qualified.

Generate the next frozen candidate run with:

```powershell
fpl-history --database data/fpl.sqlite3 project-forward-candidate `
  playing-time-hurdle-logistic-v1 --start-gameweek 1 --horizon 8
```

The command fails after the deadline. The prospective completeness report requires one
version- and config-matched run for every registered candidate after each deadline.

## What happens next

There is no remaining hidden historical validation set. The engineering loop is now:

1. capture every exact pre-deadline 2026/27 snapshot;
2. generate incumbent and registered-candidate runs from the same snapshot;
3. record the final decision and actual action;
4. ingest completed outcomes;
5. score forecast, distribution and continuous-decision evidence;
6. qualify or reject only when the predeclared sample thresholds are met.

The immediate model priority is the logistic playing-time candidate. The share-xG design
should not receive another broad team-strength search; if revisited, change player
allocation/rate features in response to the oracle evidence.
