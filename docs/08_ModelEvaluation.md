# Corrected-v4 multi-horizon evaluation

**Evaluation date:** 2026-07-30  
**Incumbent:** `rates-rules-corrected-v4`  
**Forecast origins:** GW2–GW31  
**Forecast horizon:** eight complete Gameweeks  
**Artifact:** `data/models/corrected-v4-horizon-evaluation.json`

## Decision

Retain corrected v4 as the production correctness default. Do not promote the
xG/xA challenger yet.

This is not a claim that corrected v4 is the final or most accurate possible model.
Simple season-average forecasts still beat it on raw MAE in several seasons. The model
usually performs better on captain and top-player regret, which is more directly relevant
to FPL decisions, but that advantage is not universal.

## Incumbent results

Every origin has a full eight-Gameweek outcome window. Development, validation and audit
seasons remain separated according to their previous use.

| Season | Role | Run | Overall MAE | GW+1 MAE | GW+8 MAE |
|---|---|---:|---:|---:|---:|
| 2021/22 | Development | 355 | 1.3631 | 1.2906 | 1.4133 |
| 2022/23 | Development | 356 | 1.2797 | 1.2459 | 1.3148 |
| 2023/24 | Development | 357 | 1.1796 | 1.1440 | 1.2075 |
| 2024/25 | Previously locked validation | 358 | 1.2336 | 1.2039 | 1.2505 |
| 2025/26 | Already-inspected audit | 359 | 1.2194 | 1.1946 | 1.2345 |

Pooled across the five seasons:

| Horizon | Samples | Points MAE | Points bias |
|---|---:|---:|---:|
| GW+1 | 103,057 | 1.2117 | -0.0078 |
| GW+2 | 102,546 | 1.2238 | -0.0119 |
| GW+3 | 102,281 | 1.2380 | -0.0134 |
| GW+4 | 101,878 | 1.2500 | -0.0159 |
| GW+5 | 101,903 | 1.2554 | -0.0183 |
| GW+6 | 101,998 | 1.2655 | -0.0172 |
| GW+7 | 102,514 | 1.2742 | -0.0203 |
| GW+8 | 102,718 | 1.2776 | -0.0227 |

Longer forecasts degrade steadily and become slightly more optimistic. Model-health and
decision reports must therefore keep horizon steps separate.

## Simple baselines

The main comparison below uses the expanding season-average points-per-fixture baseline.
It only uses evidence before each forecast origin.

| Season | Model MAE | Baseline MAE | Model captain regret | Baseline captain regret | Model top-15 regret | Baseline top-15 regret |
|---|---:|---:|---:|---:|---:|---:|
| 2021/22 | 1.3631 | 1.3560 | 12.5708 | 12.0958 | 111.4792 | 113.2667 |
| 2022/23 | 1.2797 | 1.2342 | 9.8943 | 9.7489 | 102.4537 | 104.3877 |
| 2023/24 | 1.1796 | 1.1011 | 12.3875 | 12.7583 | 116.2792 | 116.0250 |
| 2024/25 | 1.2336 | 1.1301 | 8.5458 | 8.9542 | 108.7292 | 109.4208 |
| 2025/26 | 1.2194 | 1.1278 | 13.1750 | 11.4000 | 122.9583 | 118.9000 |

The model cannot be justified by MAE alone. Its best evidence is improved decision regret
on the previously locked 2024/25 season, while the 2025/26 audit warns that this advantage
is not stable enough to declare the problem solved.

The evaluation command also reports recent-four points per fixture, season points per 90
using model minutes, and a position-average baseline.

## Legal squad regret

`evaluate-squad-regret` now replays each origin as an exact £100m persistent-squad,
legal-XI and captain problem.

- On the existing 2023/24 one-week development run 354, mean oracle regret was 101.0 for
  the model, 103.4865 for season average and 105.1622 for recent-four form.
- On the 2022/23 eight-week runs, corrected v4 scored 365.5517 mean regret and the xG/xA
  challenger scored 371.3793.

The absolute oracle-regret values are large because hindsight selects a fresh perfect
squad at every origin. The useful quantity is the comparison between methods.

**Those figures predate the autosub correction and are not comparable to current
output.** They were produced when realised points summed the forecast XI with captain
fallback and never substituted a blanking starter, while the hindsight side read the
solver objective directly. Both sides now replay the selected squad's own bench order
through exact autosubs, so realised scores rise, the two sides share one scoring
convention, and the numbers above must be regenerated before they are cited again.

Regret still grants a free wildcard at every origin. `replay-transfer-continuity` is the
persistent-squad counterpart: one squad, bank and free-transfer count carried forward,
hits charged, every week scored with autosubs. Read the two together — regret compares
selection methods, continuity estimates what a manager could actually have scored.

## xG/xA challenger

Expected-goal and expected-assist coverage is complete from 2022/23 onward and absent in
2021/22. The challenger uses actual-event fallback where expected-event fields are
missing.

| Season | Overall MAE change | Top-100 MAE change | Top-100 bias change | Captain-regret change | Top-15-regret change | Gate |
|---|---:|---:|---:|---:|---:|---|
| 2021/22 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | Pass/fallback |
| 2022/23 | -0.0087 | -0.0478 | +0.1398 | +0.6343 | +0.9075 | Fail |
| 2023/24 | -0.0090 | -0.0526 | +0.1231 | -0.3542 | -0.3084 | Fail |

The challenger improves MAE and top-100 MAE but fails the predeclared per-season bias and
decision gates. The locked validation season was therefore not queried for this
challenger.

The next expected-event design should estimate a player's share of team xG/xA and apply
that share to opponent-adjusted team expectations. Simply substituting xG/xA into the
existing player-rate model is insufficient.

## Availability gate

All historical player snapshots are `historical_reconstruction` with unknown timing.
They cannot establish what injury information was available before a deadline, so an
availability-recovery challenger cannot be backtested honestly on these seasons.

Promotion requires timestamped live 2026/27 snapshots containing:

1. pre-deadline status and chance-of-playing values;
2. subsequent appearance and minutes outcomes;
3. injury/news expiry or return estimates;
4. enough multi-week cases to compare frozen, immediate-recovery and calibrated-decay
   assumptions.

## Reproduction

Compile the persisted comparison:

```powershell
fpl-history --database data/fpl.sqlite3 compile-model-evaluation `
  --incumbent-runs 355 356 357 358 359 `
  --challenger-runs 360 361 362 `
  --output data/models/corrected-v4-horizon-evaluation.json
```

Compare any individual run with simple baselines:

```powershell
fpl-history --database data/fpl.sqlite3 compare-backtest-baselines 358
```

Run the expensive legal replay only after cheaper forecast and decision gates pass:

```powershell
fpl-history --database data/fpl.sqlite3 evaluate-squad-regret 358 `
  --methods model season_points_per_fixture recent_4_points_per_fixture
```

Then replay the same run as one persistent squad for a season-shaped score:

```powershell
fpl-history --database data/fpl.sqlite3 replay-transfer-continuity 358 `
  --max-transfers-per-week 1
```

## Next modelling work

1. Split learned modelling into appearance, conditional minutes and scoring components.
2. Build player-share-of-team-xG and opponent-adjusted team-xG challengers.
3. Price chip usage into the continuity replay, which currently plays none.
4. Collect live availability and paired team-news evidence throughout 2026/27.
5. Reserve genuinely new forward data for promotion decisions.
