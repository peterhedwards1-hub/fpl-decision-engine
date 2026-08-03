# Coherent allocation and uncertain minutes challengers

The incumbent rates model remains the default. The component matrix below is
opt-in only; each file is a complete copy of `preseason-priors-v1` with only
the named component changed. It is deliberately not a comparison against
generic `ProjectionModelConfig` defaults.

| Candidate | Team strength | Minutes | Player scoring |
| --- | --- | --- | --- |
| `preseason-priors-v1` | preseason raw-goals control | incumbent two-stage | independent rates |
| `opponent-adjusted-team-strength-only-v1` | opponent-adjusted | incumbent two-stage | independent rates (diagnostic only: known double-count limitation) |
| `coherent-player-allocation-v1` | preseason raw-goals control | incumbent two-stage | `coherent_team_allocation` |
| `uncertain-minutes-v1` | preseason raw-goals control | `participation_v1` | independent rates |
| `opponent-adjusted-coherent-allocation-v1` | opponent-adjusted | incumbent two-stage | `coherent_team_allocation` |
| `coherent-points-minutes-v1` | preseason raw-goals control | `participation_v1` | `coherent_team_allocation` |
| `opponent-adjusted-coherent-participation-v1` | opponent-adjusted | `participation_v1` | `coherent_team_allocation` |

Every candidate file contains every `ProjectionModelConfig` field and is tested
through the same declaration round-trip used by the promotion gate. For
opponent-adjusted candidates, its settings and contextual-adjustment manifest
are also pinned. Shared venue, multiplier-bound, assist-rate and form-half-life
values have one source of truth: the projection config. A contradictory copy in
the team-strength settings is rejected rather than silently overwritten. The
canonical declaration is hashed after loading, so changing an active assumption
changes the persisted identity. `DEFAULT_MODEL_CONFIG` is unchanged.

## Coherent event allocation

For each fixture the model first calculates one team expected-goals value from the same team strength, opponent defence and venue factors used by the team projection. It then forms non-negative player weights from historical xG/xA, position priors, expected participation and shrinkage. The normalized weights allocate the team value. No second team-strength multiplier is applied to the allocated player events. Consequently, the sum of player goals equals the team fixture expectation whenever at least one player has positive participation; an all-unavailable team is reported as unallocated rather than silently assigned to a fringe player.

Penalty goals are a declared uncertain fraction (`0.08` by default) of team goals.
Reviewed, effective-dated penalty-role overrides allocate that component between
the declared primary and secondary takers; an unavailable taker receives none.
Without such evidence, the component uses an explicitly unresolved broad prior,
not the ordinary goal-share leader. Assists are allocated from team assisted
goals, equal to team goals multiplied by
`(1 - coherent_assist_unassisted_goal_fraction)`. Rebounds, own goals and goals
without an FPL assist remain represented by that unassisted fraction rather than
being independently generated.

Transfers use the old event evidence as a role prior and apply additional
shrinkage only when the target-season club differs from the most recent
previous-season club. A settled player is not penalised merely for an older
career move. The new club controls the available team output; it does not
multiply the old-club total a second time.

## Participation and reconciliation

`participation_v1` stores start probability, substitute-appearance probability conditional on not starting, conditional minutes for each route, no-appearance probability, unconditional minutes and 60-minute probability. Unknown roles are explicit and shrink toward conservative position priors. Official FPL status is kept separate from the tactical start probability; an `a` status means “not officially flagged”, not “certain starter”. A small horizon decay returns later Gameweeks gradually toward the role prior when no forward evidence is available.

The old 990-minute allocation remains available for the incumbent. The challenger uses a bounded, role-preserving correction after player estimates, with configurable relative and absolute limits. Goalkeepers and outfielders are handled in separate groups. A player with zero availability cannot be promoted by reconciliation, and any residual team deficit is retained as an unresolved diagnostic rather than fabricated precision.

Every persisted projection row includes the participation components, role evidence, unknown-role flag, reconciliation adjustment, team expected goals, assisted-goal expectation, goal/assist/penalty shares and the canonical config hash in `assumptions_json`.

## 2026/27 diagnostic run (superseded comparison)

The following run used database `data/fpl.sqlite3`, ingestion run `7`, generated at `2026-08-02T18:39:20.591976+00:00`, with the pre-deadline snapshot and an eight-Gameweek horizon. It predates the complete-control declarations above, so it is retained as a structural diagnostic only. It is not a valid clean ablation or qualification result and must be regenerated before comparison or forward registration.

| Model | Horizon points (own scale) | Captain / vice | Notable change |
| --- | ---: | --- | --- |
| incumbent preseason priors | 452.271 | Haaland / B. Fernandes | Reported squad reproduced |
| coherent-player-allocation-v1 | 393.330 | B. Fernandes / Haaland | United triple is not selected; player goals reconcile to team goals |
| uncertain-minutes-v1 | 403.750 | Haaland / Cunha | Palmer/Rayan/Truffert/Evanilson/Dowell no longer receive static 90/40.8-minute paths |
| coherent-points-minutes-v1 | 356.802 | Haaland / Cunha | Both weaknesses are active |

These totals are not comparable across model families. Cross-valuations from the same run were:

```
holder                         incumbent  allocation  minutes  combined
incumbent squad                  452.271    387.990   386.741   345.623
allocation squad                 448.033    393.330   373.961   342.637
minutes squad                    418.890    347.894   403.750   348.892
combined squad                   419.708    359.104   396.241   356.802
```

The incumbent remains available for rollback. Challenger outputs are diagnostics, not evidence that a lower or higher raw total is more accurate. They should be registered for forward capture only after leakage-safe historical evaluation; no inspected-season tuning or automatic promotion is performed here.

## Reproduction commands

```powershell
.\\.venv\\Scripts\\python.exe -m pytest tests/test_projections.py tests/test_model_challengers.py -q
.\\.venv\\Scripts\\ruff.exe check src/fpl_engine/projections.py tests/test_model_challengers.py
fpl-history --database data/fpl.sqlite3 register-forward-candidate coherent-player-allocation-v1 `
  --season 2026-27 --model-version rates-coherent-player-allocation-v1 `
  --model-config config/model_candidates/coherent-player-allocation-v1.json `
  --gate-policy config/model_candidates/forward-promotion-policy-v1.json
```

Repeat registration with the other two JSON files and model-version labels before `capture-gameweek-forecasts`. Registration is intentionally a separate operator action; adding these files does not silently replace the incumbent or alter `DEFAULT_MODEL_CONFIG`.

## Remaining limitations

The historical schema does not always distinguish injury, being dropped and an unused substitute. Those cases remain uncertain and are exposed for the team-news workflow. Penalty roles, tactical positions, future line-ups and injury return dates still require sourced, effective-dated overrides. A full leakage-safe historical playing-time and decision evaluation is a qualification step for forward registration, not something inferred from this one 2026/27 opening squad.
