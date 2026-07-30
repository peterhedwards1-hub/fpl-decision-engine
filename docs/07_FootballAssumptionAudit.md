# Football-assumption audit

**Status date:** 2026-07-30
**Audit version:** `football-assumptions-v1`
**Selection data:** development seasons only

## Purpose

Hyperparameter tuning cannot repair an incorrect football assumption. This audit keeps
the production `rates-two-stage-v3` incumbent unchanged and compares predeclared
challenger variants on the 2021/22–2023/24 development seasons. It does not query the
already-inspected 2024/25 validation season.

The transparent variants isolate:

1. `reference`: the robust-v4 trial-71 configuration;
2. `position_minutes`: separate 90-minute goalkeeper and 900-minute outfield budgets;
3. `recent_scoring`: triple weight for scoring events in the recent six-Gameweek window;
4. `corrected_scoring`: Poisson threshold probabilities for defensive contributions and
   explicit penalty-save/miss rates;
5. `combined`: all three changes.

The learned comparison fits the same histogram gradient booster with:

- `absolute_error`, which targets a conditional median;
- `squared_error`, which targets a conditional mean;
- `poisson`, which targets a non-negative conditional mean.

The folds expand chronologically: 2021/22 trains the 2022/23 evaluation, then 2021/22
and 2022/23 train the 2023/24 evaluation.

## Metrics and gates

Every transparent variant reports overall and top-100 MAE and bias, minutes MAE,
top-100 realised points, captain regret and unconstrained top-15 regret. Its
cross-season score uses the same 25% worst-season stability penalty as the robust
rolling study.

A transparent variant passes the development gate only if it does not worsen overall
MAE, top-100 MAE, top-100 absolute bias or captain regret against the reference.

A learned mean-target loss passes only if it improves overall and top-100 absolute bias
against `absolute_error` without worsening top-100 MAE or captain regret. Passing is
evidence for further development, not automatic production promotion.

## Run

From PowerShell:

```powershell
.\.venv\Scripts\python.exe -m fpl_engine.history.cli `
  --database data/fpl.sqlite3 `
  audit-projection-assumptions `
  --development-seasons 2021-22 2022-23 2023-24 `
  --origin-start 2 `
  --origin-end 38 `
  --horizon 1 `
  --output data/models/assumption-audit-v1.json `
  --artifact-directory data/models/assumption-audit
```

The command persists auditable backtest runs and learned fold artifacts. It writes the
concise comparison to `data/models/assumption-audit-v1.json`.

## Completed development result

The full 2021/22–2023/24 audit completed on 2026-07-30. No variant passed every
predeclared gate, so the production incumbent remains unchanged.

| Variant | Overall MAE | Top-100 MAE | Top-100 absolute bias | Captain regret | Gate |
|---|---:|---:|---:|---:|---|
| Reference | 1.1949 | 2.5899 | 0.1359 | 10.7412 | Reference |
| Position minutes | 1.1944 | 2.6056 | 0.1357 | 10.8986 | Fail |
| Recent scoring | 1.1909 | 2.6074 | 0.1601 | 10.8326 | Fail |
| Corrected scoring | 1.1790 | 2.5692 | 0.2118 | 10.7863 | Fail |
| Combined | 1.1756 | 2.5963 | 0.2192 | 10.7247 | Fail |

Position-aware allocation nearly removed aggregate goalkeeper minutes bias
(-1.2593 to -0.0015 minutes) and improved goalkeeper points MAE (0.8767 to 0.8527),
but worsened top-player and captain performance. It remains a sound structural input for
a dedicated minutes challenger rather than a standalone promotion.

The learned results confirmed the target mismatch:

| Loss | Target | Overall MAE | Absolute bias | Top-100 MAE | Top-100 bias | Gate |
|---|---|---:|---:|---:|---:|---|
| Absolute error | Median | 1.0427 | 0.8771 | 2.1574 | 1.2362 | Fail |
| Squared error | Mean | 1.1366 | 0.0241 | 2.6179 | 0.1933 | Fail |
| Poisson | Mean | 1.1357 | 0.0240 | 2.6438 | 0.1360 | Fail |

Mean-target losses fixed the severe underprediction but did not preserve top-player
ranking. The next learned design should therefore model appearance, conditional minutes
and scoring components separately rather than directly replacing total expected points.

## Boundaries

- Club-change resets are not included because reconstructed fixture rows do not retain a
  sufficiently reliable per-fixture club role.
- Defensive-contribution threshold scoring did not exist in the development seasons, so
  it requires forward 2026/27 calibration.
- Historical team news is unavailable and remains a separate paired live evaluation.
- Top-15 regret ignores prices, formations and club limits. Full optimiser replay remains
  a later promotion gate.
- xG/xA and opponent-adjusted team-strength challengers remain separate work because their
  historical coverage must be measured before they can replace realised-event rates.
