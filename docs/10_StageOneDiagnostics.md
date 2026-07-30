# Stage 1 diagnostic results

**Evaluation date:** 2026-07-30  
**Incumbent runs:** 355–359  
**Baseline:** expanding season points per fixture  
**Artifact:** `data/models/stage-one-diagnostics-v1.json`

## Decision

Keep corrected v4 as the production correctness default, but do not claim a conclusive
forecast or decision advantage over the season-average baseline.

The model has better pooled RMSE, which is the appropriate direction for a conditional
mean forecast, and every position has lower RMSE. The 95% bootstrap interval still
slightly crosses zero, however. The global top-one and unconstrained top-15 regret
comparisons are plainly inconclusive.

These results use all five already-inspected seasons and are design evidence only.

## Paired moving-block comparison

The bootstrap resamples seasons and circular blocks of three consecutive target
Gameweeks. Every origin and horizon that predicts a target Gameweek remains in the same
block.

| Metric, model minus baseline | Observed | 80% interval | 95% interval | Interpretation |
|---|---:|---:|---:|---|
| Points RMSE | **−0.0338** | −0.0642 to −0.0066 | −0.0812 to 0.0057 | Encouraging, not conclusive at 95% |
| Absolute points bias | **−0.0265** | −0.0448 to −0.0015 | −0.0529 to 0.0108 | Encouraging, not conclusive at 95% |
| Global top-one regret | +0.3252 | −0.2572 to 0.9222 | −0.5360 to 1.2368 | Inconclusive |
| Unconstrained top-15 regret | +0.0008 | −1.9339 to 2.0939 | −2.9192 to 3.2237 | Inconclusive |

RMSE differences by season are −0.1173, −0.0545, −0.0190, +0.0022 and +0.0084 from
2021/22 through 2025/26. The improvement therefore declines across the window and is not
stable enough to treat the pooled result as a promotion result.

Every position improves against the baseline on pooled RMSE:

| Position | Difference |
|---|---:|
| DEF | −0.0652 |
| FWD | −0.0498 |
| MID | −0.0098 |
| GK | −0.0079 |

## Calibration and residual evidence

- Appearance probability: Brier 0.157800; log loss 0.483299.
- 60-minute probability: Brier 0.141401; log loss 0.430628.
- The highest appearance bin predicts 93.7% but realises 86.9%, indicating
  overconfidence among apparent nailed starters.
- DNP rows average 0.746 projected points against approximately zero realised.
- Played rows average 1.917 projected against 2.952 realised.
- Goalkeepers have the largest position bias: −0.1685 actual minus predicted.
- Top-15 forecasts are optimistic by 0.1095 points per player.

The DNP/played split is not itself a fair promotion metric because it conditions on a
future outcome. It is strong diagnostic evidence that separating appearance from
conditional output is the correct next modelling direction.

## Oracle status

Schema 14 now persists the point components needed for actual-appearance and
actual-minutes sensitivity. Runs 355–359 predate that schema, so their oracle section
correctly reports `requires_component_backtest`.

New component-enabled backtests can calculate:

1. predicted conditional output given the realised appearance;
2. predicted rates evaluated at realised minutes, including the realised 60-minute
   clean-sheet gate.

The share-xG challenger now persists origin-time team expectations and player shares.
Run 364 therefore adds two more sensitivities:

| Forecast | RMSE |
|---|---:|
| Share-xG model | 2.0604 |
| Actual team goals | 2.0023 |
| Actual player goals and assists | 1.3352 |

The team result suggests only a modest ceiling from perfecting the fixture goal total;
the much larger player-event result points toward player allocation/rates. All oracle
results are interacting sensitivities rather than additive error attribution.

## Defensive-contribution challenger

The forward-only `empirical_2025_minutes_band` challenger replaces the Poisson threshold
tail with observed 2025/26 hit rates split below and above 60 minutes. The incumbent is
unchanged, and the backtester rejects this challenger on 2025/26 and earlier. Its first
valid comparison is therefore prospective 2026/27 defender calibration and selection.

## Prospective qualification

The first guarded 2026/27 official snapshot was captured on 2026-07-30 as ingestion run
6, before the GW1 deadline of 2026-08-21 17:30 UTC. The durable collection workflow is
scheduled on Monday, Thursday and Friday during season months. Scheduled captures retain
their actual pre-deadline or final post-Gameweek classification; manually requested
pre-deadline evidence can use the fail-closed flag.

Use:

```powershell
fpl-history --database data/fpl.sqlite3 stage-one-diagnostics `
  --run-ids 355 356 357 358 359 `
  --baseline season_points_per_fixture `
  --output data/models/stage-one-diagnostics-v1.json

fpl-history --database data/fpl.sqlite3 `
  prospective-capture-status 2026-27
```
