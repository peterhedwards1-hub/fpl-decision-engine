# Milestone implementation status

**Roadmap:** `02_ImplementationRoadmap.md`  
**Status date:** 2026-07-29  
**Database schema:** 10

This is the implementation-status companion to the canonical roadmap. It does not redefine
milestone scope or acceptance criteria.

| Milestone | Status | Implemented evidence | Remaining qualification |
|---|---|---|---|
| 0 — Rules and scoring | Complete | Season-configured legality, scoring, selling prices, transfers, chips, autosubs, captain fallback and tests | Revalidate configuration if official rules change |
| 1 — Historical foundation | Complete | SQLite core, provenance, identity reconciliation, Vaastav adapter pinned to immutable revisions and real 2025/26 import validation | Add other sources only when they materially improve modelling |
| 2 — Live collection | Complete | Deadline-aware official collection, gzip immutable archives, replay, fixture history, season statistics, health report and durable `snapshot-data` workflow | Add a schedule when collection cadence is agreed |
| 3 — Manager state and basic front end | Complete | Append-only validated manager snapshots, financial/chip state, manual Streamlit editor, reload, pitch/bench view and freshness panel | Authenticated import remains optional |
| 4 — Projection baseline | Complete baseline | Eight-Gameweek rates model, two-stage appearance/conditional-minutes estimates, recent-evidence weighting, 990-minute team reconciliation, team strength, fixture/location effects, scoring components, availability, uncertainty, overrides, persistence and explorer | Add dedicated preseason/set-piece inputs and learned probability challengers |
| 5 — Best starting XI | Complete | Exact CBC optimisation, deterministic tie handling, near-selected list and exhaustive-enumeration test | None for milestone scope |
| 6 — Full squad and weekly lineup | Complete | Exact 15-player squad/lineup/captain selection, ordered bench and exact enumeration of all appearance/autosub outcomes | Improve the selection objective as distributions mature |
| 7 — Opening squad | Complete baseline | Eight-Gameweek robust objective, uncertainty and residual value, alternatives, rerun triggers and browser report | Calibrate robustness weights on realised 2026/27 data |
| 8 — Transfer recommender | Complete initial scope | Ranked roll, exact one- and two-transfer routes, hits, selling prices, bank, future free transfers, post-route lineup and browser comparison | Extend beyond two simultaneous transfers where useful |
| 9 — Two-pass workflow | Substantially complete | News review queue, provisional/final modes, reviewed overrides, immutable final runs, actual-action/deviation capture, evaluation and browser workflow | Automate public post-deadline action reconciliation and scheduled scoring |
| 10 — Full system | In progress | Strict structured-news JSON contract, human review, two-transfer search, dynamic 1–8 GW horizon, blanks/doubles, chip-specific optimisation, replay, model-health/version comparison, decision-focused backtest diagnostics and persisted Optuna development/validation tuning; the 2025/26 study selected trial 13 and improved held-out top-100 points MAE by 10.1% | Promote the selected candidate under a new version, add multi-season validation and learned boosted challengers, implement end-to-end decision-regret replay, deepen transfer/chip planning, add external news acquisition and justify private hosting |

## Current delivery gate

Milestones 0–8 are implemented as a connected baseline, pending integration qualification
on the updated main workflow. The qualification gate requires a reproducible install with
the explicit optimisation dependency, clean lint and tests, database migration coverage, a
database initialisation smoke run, and a basic UI launch check. Restore the stronger
“ready for use” wording only after that workflow passes. Milestone 9 can run the complete
manual weekly cycle without rewriting historical inputs, but its post-deadline
reconciliation is not yet automatic. Milestone 10 is intentionally not marked complete:
its remaining work depends on current-season observations, model calibration and deployment
choices rather than missing FPL rule definitions.

Local integration preflight on 2026-07-29 passed the exact
`.[dev,optimize,modeling]` installation, Ruff, all 74 tests (including migration,
exact-solver and tuning tests), schema-version-10 initialisation, dependency validation,
workflow-YAML parsing and a headless Streamlit launch. The remaining repository-level
qualification is a successful run of the updated GitHub Actions workflow.

The completed tuning experiment is documented in
`05_ProjectionTuningRecord.md`. Three seasons were present as potential rate evidence
(2022/23, 2023/24 and 2025/26), but development and held-out evaluation both used
2025/26. This is not yet a five-season evaluation.

## Known model limitations

- Preseason role, penalties and set pieces currently enter through reasoned expected-minutes
  or team-strength overrides rather than dedicated feeds.
- Uncertainty is a transparent scalar penalty, not yet a full outcome distribution.
- The full-squad solver proves its linear selection objective; its reported weekly score then
  values autosubs exactly across appearance outcomes.
- Transfer search proves one best route for each of zero, one and two moves. It does not yet
  enumerate every near-optimal combination or longer transfer plan.
- Chip recommendations share the legal rules and exact squad solver, but season-long chip
  timing is not yet jointly optimised.
- Reconstructed historical snapshots have unknown capture times. Backtests therefore ignore
  their availability fields; exact availability replay requires archived pre-deadline data.
