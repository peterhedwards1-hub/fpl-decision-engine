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

### Status terminology and databases

This document distinguishes three states that must not be inferred from one
another: **committed** means a JSON declaration exists in the repository;
**registered** means an immutable row exists in the target database before the
deadline; **captured** means that registered declaration has a matching
pre-deadline forecast run. A committed challenger is neither registered nor
captured merely because a current-season snapshot exists.

The historical development database used for the evaluations in this document
is separate from the live `data/fpl.sqlite3`. The live database now contains
2026/27 ingestion snapshots and can support a registration/capture origin.
Always inspect its registration and prospective-capture-status records before
using a forward slot; the table above records the historical qualification
workflow, not a claim about every current checkout or snapshot.

A fourth candidate configuration, `opponent-adjusted-team-strength-v1`, is written and
historically evaluated but **not registered, and not currently recommended for
registration**. Its design, results and remaining uncertainties are in
`13_TeamStrengthModel.md`.

Its controlled four-variant evaluation found the pattern this gate exists to catch:
the candidate has the best player-points MAE of the four and the worst owned-captain
regret, transfer regret and continuous policy points. The team-strength component
itself is sound and improves team goals in both available seasons; the share-based
allocator it is paired with introduces a top-15 bias of +0.74 against the incumbent's
+0.23, and opponent adjustment only repairs a third of that. Fix the allocator and
re-run before spending a capture slot.

The same evaluation established where forecast error actually lives: 92% of squared
player-points error sits with players whose minutes were mis-called, and a minutes
oracle removes 17% of RMSE, against a 1% spread across all four team-strength and
allocation variants. **`playing-time-hurdle-logistic-v1` is the higher-value
candidate** and has still never been run end-to-end into points.

### What a declaration now contains

`ProjectionModelConfig` alone stopped being a complete model specification when team
strength grew its own constants and a contextual-adjustment manifest. Both were
previously supplied at runtime from code defaults, so a registered candidate's
SHA-256 could stay identical while an edit to a default changed every forecast it
produced — preregistration in name only.

`ModelDeclaration` (`src/fpl_engine/declaration.py`) is now the hashed unit and
carries the projection config, the team-strength settings and the adjustment
manifest together. `register_forward_candidate`, `capture_gameweek_forecasts`,
`run_forward_candidate_pair` and `ProjectionBacktester` all carry all three through,
so a capture is the declared model rather than a truncation of it.

Contextual adjustments now require `reviewed_at` (timezone-aware) and `reviewed_by`,
and `ModelDeclaration.adjustments_before(cutoff)` makes "this judgement predates the
deadline it is scored against" checkable rather than assumed.

**The three registered candidates keep their digests.** A declaration carrying
nothing beyond the projection config serialises to the bare config dictionary, which
is exactly what was hashed before, and a test pins that.

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

`replay_backtest_transfer_continuity` drives that replay from any completed backtest run,
so the continuity machinery is no longer reachable only from hand-assembled weeks. It
takes each origin's same-Gameweek forecast, selects the opening squad at the first
replayed Gameweek, and carries one squad forward from there. The candidate universe is
fixed at the opening Gameweek, since a player who first appears mid-window cannot be
carried by a replay that requires an identical candidate set every week.

`score_squad_gameweek` — the exact autosub, bench-order and captain-fallback scorer — is
now shared with `evaluate_legal_squad_regret`, which previously summed the forecast XI
and never substituted a blanking starter. Both sides of that regret measure are replayed
through it, so realised and hindsight points are on one scoring convention.

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

## Scoring a candidate once outcomes exist

`project-forward-candidate` freezes a pre-deadline forecast. Scoring it is a separate
step, and until 2026-07-31 the gate could not reach `preseason-priors-v1` at all:
`backtest-projections` assembled its configuration from the incumbent defaults plus a
fixed flag list, and nine declared fields — every Stage 3a field among them — had no
flag. The gate compares a run's persisted configuration to the declaration key by key,
so every run produced this way was rejected with "Challenger configuration differs from
declaration" regardless of how the model performed.

`backtest-projections --model-config` now runs a full declared configuration; individual
flags still override single fields on top of it. `backtest-forward-candidate` wraps that
for the gate specifically, producing both runs the gate needs over one identical scope:

```powershell
fpl-history --database data/fpl.sqlite3 backtest-forward-candidate `
  preseason-priors-v1 `
  --incumbent-config config/model_candidates/preseason-priors-v1-incumbent.json `
  --origin-start 1 --origin-end 8 --horizon 1
```

The challenger runs the declaration verbatim under the registered model version; the
incumbent runs the declared control over the same origins; both are forced to
`pre_deadline_only`. The command refuses a control equal to the candidate, a control
sharing the candidate's model version, and rules from the wrong season, so a pair cannot
fail the gate's scope, version or configuration checks through operator error. It also
rebuilds the declared configuration before spending a backtest and fails if it no longer
round-trips through `ProjectionModelConfig` — a field added to the dataclass after
registration would otherwise be discovered only after both runs had completed.

`config/model_candidates/preseason-priors-v1-incumbent.json` is the declared control: it
is `rates-rules-corrected-v4` exactly, so it differs from the candidate in
`team_strength_carry_forward` and `cold_start_prior` and nothing else. Committing it
rather than deriving it keeps the comparison attributable to Stage 3a and stops the
control being retuned later.

Decision evidence is then measured from that pair:

```powershell
fpl-history --database data/fpl.sqlite3 build-decision-evidence `
  --incumbent-run <id> --challenger-run <id> `
  --owned-captain-regret-change <value> --transfer-regret-change <value> `
  --output data/preseason-priors-v1-decision-evidence.json
```

`legal_squad_regret_change` is computed directly from `evaluate_legal_squad_regret` over
both runs, which must cover identical origins. Owned-captain and continuous-transfer
regret have no replay producer yet, so both stay required operator inputs rather than
being defaulted to zero — a silent zero would pass two of the five decision gates on no
evidence. Building those two producers is the remaining gap in Stage 3a gate readiness.

The resulting file feeds `evaluate-forward-candidate --decision-evidence`.

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
