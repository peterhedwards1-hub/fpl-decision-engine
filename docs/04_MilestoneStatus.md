# Milestone implementation status

**Roadmap:** `02_ImplementationRoadmap.md`  
**Status date:** 2026-07-29  
**Database schema:** 8

This is the implementation-status companion to the canonical roadmap. It does not redefine
milestone scope or acceptance criteria.

| Milestone | Status | Implemented evidence | Remaining qualification |
|---|---|---|---|
| 0 — Rules and scoring | Complete | Season-configured legality, scoring, selling prices, transfers, chips, autosubs, captain fallback and tests | Revalidate configuration if official rules change |
| 1 — Historical foundation | Complete | SQLite core, provenance, identity reconciliation, Vaastav adapter pinned to immutable revisions and real 2025/26 import validation | Add other sources only when they materially improve modelling |
| 2 — Live collection | Complete | Deadline-aware official collection, gzip immutable archives, replay, fixture history, season statistics, health report and durable `snapshot-data` workflow | Add a schedule when collection cadence is agreed |
| 3 — Manager state and basic front end | Complete | Append-only validated manager snapshots, financial/chip state, manual Streamlit editor, reload, pitch/bench view and freshness panel | Authenticated import remains optional |
| 4 — Projection baseline | Complete baseline | Eight-Gameweek rates model, shrinkage, team strength, fixture/location effects, scoring components, availability, uncertainty, overrides, persistence and explorer | Add dedicated preseason/set-piece inputs and richer probabilistic minutes |
| 5 — Best starting XI | Complete | Exact CBC optimisation, deterministic tie handling, near-selected list and exhaustive-enumeration test | None for milestone scope |
| 6 — Full squad and weekly lineup | Complete | Exact 15-player squad/lineup/captain selection, ordered bench and exact enumeration of all appearance/autosub outcomes | Improve the selection objective as distributions mature |
| 7 — Opening squad | Complete baseline | Eight-Gameweek robust objective, uncertainty and residual value, alternatives, rerun triggers and browser report | Calibrate robustness weights on realised 2026/27 data |
| 8 — Transfer recommender | Complete initial scope | Ranked roll, exact one- and two-transfer routes, hits, selling prices, bank, future free transfers, post-route lineup and browser comparison | Extend beyond two simultaneous transfers where useful |
| 9 — Two-pass workflow | Substantially complete | News review queue, provisional/final modes, reviewed overrides, immutable final runs, actual-action/deviation capture, evaluation and browser workflow | Automate public post-deadline action reconciliation and scheduled scoring |
| 10 — Full system | In progress | Strict structured-news JSON contract, human review, two-transfer search, dynamic 1–8 GW horizon, blanks/doubles, chip-specific optimisation, replay and model-health/version comparison | Richer expected-minutes and uncertainty distributions, deeper transfer/chip planning, external news acquisition, calibrated backtests and justified private hosting |

## Current delivery gate

Milestones 0–8 are ready for use as a connected baseline. Milestone 9 can run the complete
manual weekly cycle without rewriting historical inputs, but its post-deadline reconciliation
is not yet automatic. Milestone 10 is intentionally not marked complete: its remaining work
depends on current-season observations, model calibration and deployment choices rather than
missing FPL rule definitions.

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
