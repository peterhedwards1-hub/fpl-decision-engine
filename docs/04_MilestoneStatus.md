# Milestone implementation status

**Roadmap:** `02_ImplementationRoadmap.md`  
**Status date:** 2026-07-30
**Database schema:** 15

This is the implementation-status companion to the canonical roadmap. It does not redefine
milestone scope or acceptance criteria.

| Milestone | Status | Implemented evidence | Remaining qualification |
|---|---|---|---|
| 0 — Rules and scoring | Complete | Season-configured legality, scoring, selling prices, transfers, chips, autosubs, captain fallback and tests | Revalidate configuration if official rules change |
| 1 — Historical foundation | Complete | SQLite core, provenance, identity reconciliation, five pinned-revision Vaastav seasons from 2021/22 through 2025/26, and explicit exclusion of 2024/25 assistant-manager chip rows | Add other sources only when they materially improve modelling |
| 2 — Live collection | Complete | Deadline-aware official collection, gzip immutable archives, replay, fixture history, season statistics, health report, fail-closed pre-deadline capture, prospective completeness audit and thrice-weekly season schedule | Monitor scheduled runs and adjust cadence around exceptional deadlines |
| 3 — Manager state and basic front end | Complete | Append-only validated manager snapshots, financial/chip state, manual Streamlit editor, reload, pitch/bench view and freshness panel | Authenticated import remains optional |
| 4 — Projection baseline | Evaluated correctness default | Trial-13 parameters carried into corrected v4; five-season GW+1–GW+8 evaluation, simple baselines, two-stage appearance/conditional-minutes estimates, persisted appearance/60-minute probabilities, corrected scoring, recent evidence, team strength, availability, overrides and explorer | Recalibrate on development data without repeatedly reusing inspected validation seasons; add dedicated preseason/set-piece inputs |
| 5 — Best starting XI | Complete | Exact CBC optimisation, deterministic tie handling, near-selected list and exhaustive-enumeration test | None for milestone scope |
| 6 — Full squad and weekly lineup | Complete | Exact 15-player squad selection against each projected Gameweek's legal XI and captain value, ordered bench and exact current-week autosub/captain-fallback expectation | Add distribution-aware future autosub value when calibrated |
| 7 — Opening squad | Complete baseline | Multi-Gameweek legal-XI/captain objective, alternatives, rerun triggers and browser report; uncertainty and residual value disclosed without arbitrary objective weights | Calibrate decision-risk and terminal-value terms before pricing them |
| 8 — Transfer recommender | Complete extended scope | Ranked roll and exact legal routes through the configured transfer cap, hits, selling prices, bank, future free transfers, empirical state-dependent option value, continuous replay, exact autosubs and browser comparison | Estimate the option-value distribution from enough prospective actions |
| 9 — Two-pass workflow | Substantially complete | Strict news-v2 provenance, human review, automatic paired pre/post-news projection runs, final-run evidence enforcement, immutable decisions, actual-action capture and paired uplift scoring | Automate public post-deadline action reconciliation, news research execution and scheduled scoring |
| 10 — Full system | Forward qualification | Five-season store, corrected v4 incumbent, calibrated playing-time hurdle, coherent team-share xG, all four component oracles, joint Monte Carlo, constrained ensemble, immutable gates, transfer continuity, automatic chip timing and prospective audit | Operate live capture continuously and accumulate enough 2026/27 outcomes/actions to pass or reject the predeclared candidates |

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
`.[dev,optimize,modeling]` installation, Ruff, all 82 tests (including migration,
exact-solver and tuning tests), schema-version-11 initialisation, dependency validation,
workflow-YAML parsing and a headless Streamlit launch. The remaining repository-level
qualification is a successful run of the updated GitHub Actions workflow.

Correctness hardening on 2026-07-30 passes Ruff and all 87 tests, including
schema-version-13 migration, projection provenance/probability persistence, caller-budget
validation, configurable chip cooldowns and multi-Gameweek optimisation coverage.

The Stage 0/1 evaluation release passes Ruff and all 94 tests. It advances the schema to
version 14 so new backtests retain point-component expectations, and adds configured
appearance scoring, nullable defensive-contribution eligibility, season-aware moving-block
uncertainty, calibration, residual slices, appearance/minutes oracle sensitivities, a
forward-only empirical defensive-contribution challenger and prospective-capture
completeness checks.

The Stage 2–5 implementation advances the schema to version 15. It adds chronological
hurdle training/live application, coherent share-xG allocation, team/player event
oracles, joint squad simulation with CRPS/coverage/PIT checks, constrained chronological
ensembles, continuous transfer replay, evidence-derived free-transfer value, automatic
chip timing and immutable forward promotion declarations. It passes Ruff and all 109
tests, including schema-14 migration and full synthetic promotion-gate coverage.
Historical results remain design evidence; the incumbent is unchanged.

The original tuning experiment is documented in `05_ProjectionTuningRecord.md`; it remains
a one-season experiment even though the database now contains five seasons. The implemented
rolling route and team-news evaluation are documented in `06_StrongestModelRoute.md`; the
development-only model-assumption gates are documented in
`07_FootballAssumptionAudit.md`.

## Known model limitations

- The incumbent still lacks dedicated share-xG, set-piece-role and preseason inputs; the
  coherent xG implementation remains a challenger after failing the design RMSE gate.
- Joint uncertainty simulation exists, but is not promoted until prospective CRPS,
  coverage and PIT evidence accumulates.
- The full-squad solver proves its multi-Gameweek legal-XI/captain objective; its reported
  current-week score values autosubs and captain fallback exactly.
- Transfer search proves one best route for each move count through the configured cap. It
  does not yet enumerate every near-optimal combination or schedule future transfers.
- Chip timing now computes future opportunity cost over the supplied horizon; truly
  season-long timing remains limited by the forecast horizon.
- Reconstructed historical snapshots have unknown capture times. Backtests therefore ignore
  their availability fields; exact availability replay requires archived pre-deadline data.
