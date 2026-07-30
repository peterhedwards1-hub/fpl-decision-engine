# Strongest-model implementation route

**Status date:** 2026-07-30  
**Incumbent:** `rates-rules-corrected-v4`  
**Database schema:** 15

## Implemented

### Five-season historical foundation

The pinned Vaastav revision is now imported for every season from 2021/22 through
2025/26:

| Season | Players | Fixtures | Player-fixture rows |
|---|---:|---:|---:|
| 2021/22 | 737 | 380 | 25,447 |
| 2022/23 | 778 | 380 | 26,505 |
| 2023/24 | 865 | 380 | 29,725 |
| 2024/25 | 784 | 380 | 27,283 |
| 2025/26 | 841 | 380 | 29,747 |

The 2024/25 source includes assistant-manager chip elements labelled `AM`. They are not
footballers and the adapter now excludes them explicitly. The import reported 322 such
Gameweek rows.

Historical rule files inherit a shared pre-2025 projection-scoring contract. Defensive
contribution points are disabled before their 2025/26 introduction. The historical files
are intended for projection replay; their chip metadata is not used by model tuning.

### Versioned incumbent

The held-out winner from study `fpl-rates-two-stage-v2`, trial 13, is promoted as
`rates-two-stage-v3`. The untuned v2 configuration remains named and reproducible for
comparison. The exact parameters and original held-out result remain in
`05_ProjectionTuningRecord.md`.

The production default is now `rates-rules-corrected-v4`. It preserves those tuned
parameters while applying the audited scoring semantics: Poisson threshold probabilities
for defensive contributions, penalty events, and expected complete pairs for
goal-concession deductions. V2 and v3 remain named and reproducible for comparison.
The old held-out score does not validate v4: the locked multi-season comparison must be
rerun before claiming that its inherited calibration remains optimal.

### Rolling multi-season selection

`tune-projections-rolling` evaluates every candidate with walk-forward forecasts across
multiple development seasons, then compares the winner against v3 on one validation
season. Its objective combines the sample-weighted mean development score with 25% of
the gap to the candidate's worst development season. This discourages a candidate from
winning through one unusually favourable season. The study stores its scope and locks
model selection immediately before the first validation query. A crash during validation
can therefore be resumed without reopening tuning. After validation finishes, the study
is permanently locked and cannot be resumed against the same holdout.

The next predeclared study should use:

- development: 2021/22, 2022/23 and 2023/24;
- locked validation: 2024/25;
- 2025/26: external context only, because its GW26–38 result has already been inspected.

Run from PowerShell:

```powershell
.\.venv\Scripts\python.exe -m fpl_engine.history.cli `
  --database data/fpl.sqlite3 `
  tune-projections-rolling `
  --development-seasons 2021-22 2022-23 2023-24 `
  --validation-season 2024-25 `
  --origin-start 2 `
  --origin-end 38 `
  --horizon 1 `
  --trials 100 `
  --study-name fpl-rates-rolling-robust-v4-2024-holdout
```

This is deliberately a separate study with expanded parameter ranges. It may take a
substantial time and will persist prediction-level audit records. For this command,
`--trials 100` means a target of 100 completed trials: rerunning the same command after an
interruption fills only the remaining trials. Once validation completes, the holdout lock
rejects any further tuning.

The primary search remains a one-Gameweek forecast test. That isolates player scoring
and minutes calibration without multiplying every search trial by the planning horizon.
Three- and five-Gameweek forecast qualification, learned challengers and downstream
squad/transfer/captaincy regret remain predeclared promotion gates after this search;
the one-Gameweek winner is not automatically promoted.

### Learned challenger gate

`train-boosted-challenger` fits a histogram gradient-boosting points calibrator on the
winning development backtests and evaluates it on the matching locked validation
backtest. It uses base expected points, expected minutes, uncertainty, fixture count,
horizon, season progress and position indicators.

The command rejects:

- missing or failed backtest runs;
- a validation run included in training;
- training seasons that do not precede validation;
- base-model configuration differences between training and validation.

After the rolling command finishes, copy its three
`development_backtest_run_ids` and its
`challenger_validation.backtest_run_id` into:

```powershell
.\.venv\Scripts\python.exe -m fpl_engine.history.cli `
  --database data/fpl.sqlite3 `
  train-boosted-challenger `
  --training-run-ids <2021-run> <2022-run> <2023-run> `
  --validation-run-id <2024-challenger-run> `
  --artifact data/models/boosted-points-v1.joblib
```

The command writes a local model artifact and JSON metadata with features, seed, base
configuration, season scope and run IDs. It reports overall and per-origin top-100 MAE
and bias changes. The artifact is a challenger only; it is not silently loaded into live
forecasts.

### Team-news layer

Team news is separate from the base performance model but is part of the final forecast:

1. generate a pre-news v3 projection;
2. import strict schema-v2 research JSON;
3. validate player identity, source, timestamps and adjustment bounds;
4. review every item, recording decision maker, time and rationale;
5. generate pre/post-news projection runs from the same data state;
6. prevent the final weekly decision from using a run that omits a current accepted item;
7. score the pair after the Gameweek and report the change in points and minutes MAE.

The reusable research contract is `prompts/team-news-v2.md`. It captures source tier,
direct URL, publication time, evidence/model area, suggested minutes delta, adjustment
basis, decision question, expiry, prompt version and research-run ID. Version-1 evidence
remains readable.

Negative paired MAE change means the reviewed news layer improved the forecast. These
paired results are included in model health and displayed in the weekly workflow.

## Component challengers and forward gate

The remaining engineering route in `09_ModellingAndEvaluationPlan.md` is now implemented
and measured in `11_ComponentModelsAndForwardQualification.md`.

- The logistic four-part playing-time hurdle is the leading forward candidate. It
  materially improved probability and minutes scores on historical design data, but its
  global top-one sensitivity worsened.
- The coherent share-xG challenger removed team-strength double-counting and passed its
  reconciliation tests, but failed historical RMSE/top-player bias design checks.
- Joint squad simulation, proper distribution scoring, constrained OOF ensembles,
  continuous transfer replay, empirical free-transfer option value and automatic chip
  timing are available.
- Schema 15 immutably records candidate configs and promotion policies.

Three candidate declarations were made before 2026/27 GW1 outcomes. Corrected v4 remains
the incumbent.

## Remaining route to the strongest model

The remaining route is now evidence collection and a narrow player-allocation redesign,
not another broad modelling build:

1. capture paired incumbent/candidate projections throughout 2026/27;
2. score hurdle calibration, points, distributions and decisions as outcomes arrive;
3. estimate free-transfer option value only after enough actual actions exist;
4. use the player-event oracle to redesign player shares/rates if Stage 3 is revisited;
5. learn news/availability calibration only from timestamped prospective examples;
6. promote only through the immutable forward gate, with no position regression.

The robust rolling study completed 100 trials and selected trial 71. On the locked
2024/25 validation season it improved overall points MAE by 0.0214 and minutes MAE by
0.8243, but improved top-100 MAE by only 0.0025 while worsening top-100 absolute bias by
0.0991. The first `absolute_error` learned challenger reduced MAE but introduced severe
underprediction, confirming that conditional-median training is not an acceptable
expected-value model. Neither challenger is promoted.

Historical reconstructed data cannot measure news uplift because exact pre-deadline news
is absent. Live 2026/27 capture is therefore essential and cannot be replaced later.
