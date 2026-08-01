# FPL Decision Engine

A personal, reproducible Fantasy Premier League decision-support system.

## Current status

Milestones 0–8 are implemented as a connected baseline. Milestone 9 supports the complete
manual provisional/final cycle, with automated post-deadline reconciliation still to add.
Milestone 10 is in progress.

The canonical delivery sequence and acceptance criteria are defined in
[the implementation roadmap](docs/02_ImplementationRoadmap.md). Cross-cutting database,
snapshot-storage and manager-state decisions are recorded in
[the architecture decisions](docs/03_ArchitectureDecisions.md).
Exact completion evidence and remaining qualifications are tracked in
[the milestone status](docs/04_MilestoneStatus.md).

Implemented:

- versioned 2025/26 and 2026/27 season configuration;
- typed player, squad and Gameweek-stat domain objects;
- complete squad and starting-XI validation;
- deterministic player-points calculation;
- a versioned SQLite historical database schema;
- lossless fixture-level performance storage, including double Gameweeks;
- separate Gameweek snapshots for price, ownership and availability;
- source provenance and ingestion audit records;
- idempotent, transactional historical imports;
- a project-owned normalised CSV import format;
- a pinned-revision Vaastav historical adapter with explicit data-quality counts;
- official FPL bootstrap and fixture snapshot collection;
- immutable timestamped raw JSON archives with checksums;
- browser and Excel verification reports generated from SQLite;
- a manual GitHub workflow for collecting data without local setup;
- validated append-only manager-state snapshots and a browser team editor;
- transparent eight-Gameweek projections with reasoned overrides;
- the held-out trial-13 parameters with corrected scoring rules promoted as
  `rates-rules-corrected-v4` (with v3 retained for reproducibility);
- exact XI, full-squad, opening-squad and transfer optimisation;
- bench, autosub, captaincy and chip-specific decision support;
- provisional/final weekly audit records and model-health scoring;
- leakage-controlled walk-forward projection backtesting with persisted scorecards;
- five imported seasons, locked rolling multi-season tuning and paired news-uplift scoring;
- a leakage-checked histogram gradient-boosting points challenger and auditable artifact;
- chronologically calibrated four-part playing-time hurdle challengers;
- a coherent share-of-team-xG scoring challenger with latent-component oracles;
- shared-outcome Monte Carlo squad simulation and constrained OOF ensembles;
- continuous transfer replay, empirical free-transfer option value and automatic chip timing;
- immutable forward-candidate registration and executable two-tier promotion gates;
- automated tests and GitHub Actions CI.

Schema version 15 includes the hardened identity and observation foundation, fixture-state
history, manager state, projections, weekly decision records and historical backtests. The
latest schema also records versioned team-news provenance, review governance and paired
pre/post-news projection evaluations, plus persisted appearance and 60-minute
probabilities for live optimisation and historical decision replay and component
expectations for oracle diagnostics. It also stores immutable model-candidate declarations,
canonical configuration hashes and gate policies. The
database supports in-place version-2 migration, stable player
identity links, explicit identifier namespaces and delivery-source provenance, timestamped
multi-observation Gameweek history, selected-manager counts, and strict CSV contract
validation. See [the historical import foundation design](docs/historical-import-foundation.md).

## Easiest data check: use GitHub

This route does not require Python on your computer.

1. Open the repository on GitHub.
2. Select **Actions**.
3. Select **Collect and verify FPL data**.
4. Select **Run workflow** and leave the season values as `2026-27` and `2026/27`.
5. When the run finishes, download the `fpl-verification-2026-27` artifact.
6. Unzip it and open:

```text
data/reports/fpl/2026-27/latest/index.html
```

The download also contains the SQLite database, exact source JSON and Excel-compatible CSV
files. The artifact is retained for 14 days.

## One-click Windows collection

With Python 3.12 or newer installed, double-click:

```text
collect-and-review.bat
```

The script does not create a virtual environment or install project dependencies. It runs the
standard-library collector directly, stores the results under `data`, and opens the latest
verification report in your browser.

## Development setup

Requires Python 3.12 or newer.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev,optimize,modeling]'
pytest
ruff check .
```

On Windows PowerShell, activate the environment with:

```powershell
.venv\Scripts\Activate.ps1
```

Install and launch the browser interface:

```powershell
python -m pip install -e ".[ui]"
fpl-app
```

The app provides team entry, a saved pitch view, projections, optimal XI, opening squad,
transfer comparison, structured team-news import/review, paired pre/post-news projections
and model-health views.

## Rules usage

```python
from pathlib import Path

from fpl_engine import load_season_rules

rules = load_season_rules(Path("config/seasons/2026-27.json"))
print(rules.squad.budget_tenths)  # 1000 = £100.0m
```

Prices and budgets are stored as integer tenths to avoid floating-point money errors.

## Live snapshot collection

Collect the current official FPL player, team, Gameweek and fixture data:

```bash
fpl-collect \
  --database data/fpl.sqlite3 \
  --archive-root data/raw/fpl \
  --report-root data/reports/fpl \
  --season-code 2026-27 \
  --season-name "2026/27" \
  --require-pre-deadline \
  --open-report
```

Each run:

1. fetches `bootstrap-static` and `fixtures` from the official FPL service;
2. validates the minimum response shape;
3. archives the exact response bytes under a UTC timestamp;
4. writes a manifest containing endpoint URLs and a combined SHA-256 checksum;
5. normalises teams, players, Gameweeks, fixtures, price, ownership and availability;
6. atomically appends timestamped Gameweek observations in SQLite, while reprocessing the
   same archived capture idempotently refreshes its observation;
7. reads the normalised rows back from SQLite and creates a verification report.

Live verification reports explicitly select the latest pre-deadline observation, even when
post-Gameweek observations are already present. Generic historical queries can still use
`latest_available` when that is the intended view.
`--require-pre-deadline` is intended for scheduled prospective collection: it fails before
archive or ingestion if the request no longer precedes the next deadline.

The latest report is written to:

```text
data/reports/fpl/2026-27/latest/index.html
```

Each timestamped report contains:

- a searchable browser table of players and fixtures;
- the most expensive and highest-owned players;
- player price, ownership, availability and news;
- `players.csv` and `fixtures.csv` for Excel;
- the source URL, retrieval time and SHA-256 checksum;
- checks for complete snapshots, unique IDs, valid foreign keys and ingestion row counts.

The collector deliberately uses only public, unauthenticated endpoints. It does not access a
manager's private team and does not make changes to an FPL account.

## Historical database

Create a local database:

```bash
fpl-history --database data/fpl_history.sqlite3 init
```

Import a normalised CSV directory:

```bash
fpl-history --database data/fpl_history.sqlite3 import-csv data/import/2025-26 \
  --season-code 2025-26 \
  --season-name "2025/26" \
  --source-name historical-source \
  --source-url https://example.com/source
```

Inspect row counts:

```bash
fpl-history --database data/fpl_history.sqlite3 summary 2025-26
```

Run a one-Gameweek-ahead walk-forward projection backtest:

```bash
fpl-history --database data/fpl_history.sqlite3 backtest-projections 2025-26 \
  --origin-start 2 \
  --origin-end 38 \
  --horizon 1 \
  --evidence-policy performance_only
```

The command recreates each forecast using only results from earlier seasons or earlier
Gameweeks. Timestamped pre-deadline runs replay the fixture slate known at that origin;
reconstructed historical runs explicitly disclose that they use the final fixture slate.
Predictions without explicit player-fixture outcome rows are excluded rather than scored as zero. The run
persists generated, scored and missing-outcome counts, the maximum source ingestion run,
a data fingerprint, and point/minute MAE, bias and RMSE overall, by position and by
forecast horizon. It also reports DNP/played, single/double-fixture and top-N breakdowns,
plus expected versus regulation player-minutes per match. `performance_only` is the honest mode for reconstructed
historical datasets: it ignores availability fields whose capture time is unknown.
`pre_deadline_only` is stricter and requires exact `live_pre_deadline` observations
captured before their recorded deadlines.

Model constants are command options, so variants can be given distinct
`--model-version` labels and compared on an untouched validation period. For example,
`--player-prior-minutes` controls shrinkage of scoring rates and
the appearance/recent-evidence options control the two-stage expected-minutes model.
The model estimates appearance probability separately from minutes conditional on
appearing, then reconciles every team to 990 expected player-minutes per fixture.
A forward-only `--defensive-contribution-model empirical_2025_minutes_band`
challenger replaces the underdispersed Poisson tail with 2025/26 position/minutes-band
hit rates. The backtester rejects that challenger on 2025/26 and earlier because those
outcomes supplied its calibration.
A share-based xG design is available with
`--scoring-event-source team_share_expected`. A trained Stage 2 artifact can be applied
with `--minutes-model learned_hurdle --playing-time-artifact <path>`.
A completed report can
be printed again with:

```bash
fpl-history --database data/fpl_history.sqlite3 backtest-report <run-id>
```

Compare the same persisted forecast sample with leakage-controlled simple baselines:

```bash
fpl-history --database data/fpl_history.sqlite3 \
  compare-backtest-baselines <run-id>
```

Compile Stage 1 diagnostics across completed seasonal runs:

```bash
fpl-history --database data/fpl.sqlite3 stage-one-diagnostics \
  --run-ids <run-id-1> <run-id-2> \
  --baseline season_points_per_fixture \
  --output data/models/stage-one-diagnostics.json
```

This reports season-aware paired moving-block bootstrap intervals, appearance and
60-minute probability calibration, points-decile calibration, residual slices and
appearance, minutes, team-goal and player-event oracle sensitivities.

Train the selected Stage 2 design:

```bash
fpl-history --database data/fpl.sqlite3 train-playing-time-hurdle \
  --training-seasons 2021-22 2022-23 2023-24 \
  --validation-season 2024-25 \
  --family logistic \
  --artifact data/models/playing-time-hurdle-logistic-v1.joblib
```

Register a candidate before outcomes and later apply the matched forward gate:

```bash
fpl-history --database data/fpl.sqlite3 register-forward-candidate \
  playing-time-hurdle-logistic-v1 \
  --season-code 2026-27 \
  --model-version rates-playing-time-hurdle-logistic-v1 \
  --model-config config/model_candidates/playing-time-hurdle-logistic-v1.json \
  --gate-policy config/model_candidates/forward-promotion-policy-v1.json
```

Historical output is design evidence, not a replacement holdout. Schema 15 persists
point components and candidate declarations; older backtests must be regenerated for
component oracle sensitivity.

Generate a declared candidate before the deadline:

```bash
fpl-history --database data/fpl.sqlite3 project-forward-candidate \
  playing-time-hurdle-logistic-v1 --start-gameweek 1 --horizon 8
```

Once outcomes exist, score a candidate against its declared control. This produces the
matched pair the gate requires — same season, origins and horizon, both
`pre_deadline_only`, the challenger running the declaration verbatim:

```bash
fpl-history --database data/fpl.sqlite3 backtest-forward-candidate \
  preseason-priors-v1 \
  --incumbent-config config/model_candidates/preseason-priors-v1-incumbent.json \
  --origin-start 1 --origin-end 8 --horizon 1

fpl-history --database data/fpl.sqlite3 build-decision-evidence \
  --incumbent-run <id> --challenger-run <id> \
  --owned-captain-regret-change <value> --transfer-regret-change <value> \
  --output data/preseason-priors-v1-decision-evidence.json
```

`build-decision-evidence` derives all three decision gates from the run pair —
legal-squad, owned-captain and same-state transfer regret. Nothing is supplied by an
operator, so the gate cannot be passed on an asserted number. To run any full configuration
outside the candidate flow, pass `backtest-projections --model-config <path>`;
individual flags still override single fields on top of it.

Before each deadline, capture the forecasts the forward gate will need. One call
persists the incumbent and every declared candidate from the same pre-deadline snapshot:

```bash
fpl-history --database data/fpl.sqlite3 capture-gameweek-forecasts 2026-27 \
  --gameweek 1 --horizon 8
```

It fails closed: after the deadline there is no honest pre-deadline forecast to take, so
nothing is written and nothing may be back-filled. It also refuses when no exact
pre-deadline snapshot exists yet, and when a candidate shares the incumbent's model
version — their runs would be indistinguishable later.

The incumbent is captured alongside the candidates deliberately. A challenger run with
nothing to compare against is not evidence, and the gate needs matched pairs. The output
records the projection run IDs, configuration hashes, the ingestion run that produced the
snapshot, and the generation timestamp. Outcomes are joined afterwards; this record is the
part that cannot be reconstructed once the deadline passes.

Audit prospective evidence completeness after each deadline:

```bash
fpl-history --database data/fpl.sqlite3 \
  prospective-capture-status 2026-27
```

The status report checks exact pre-deadline snapshots, completed-fixture captures with
cumulative player outcomes, manager state, paired pre/post-news projections, frozen
decisions, actual actions and evaluations. It additionally requires an **incumbent**
pre-deadline projection, not only the declared candidates; counts a projection only when
its rows carry expected minutes and appearance probabilities, since a run of nulls is not
evidence; and counts a frozen decision only when it records the selected squad, weekly XI,
captain, vice-captain and bench order, since `recommendation_json` is schema-free and a
bare existence check would pass an empty one. The durable GitHub workflow is scheduled on
Monday, Thursday and Friday during season months, while remaining manually dispatchable.

The expensive legal decision gate replays a £100m persistent squad, legal weekly XIs and
captaincy:

```bash
fpl-history --database data/fpl_history.sqlite3 \
  evaluate-squad-regret <run-id> --methods model season_points_per_fixture
```

Realised and hindsight points both replay the selected squad's own bench order through
exact autosubs, so a blanking starter is substituted rather than charged as a zero.

Captaincy is scored separately, against the best armband available *within the same
squad*:

```bash
fpl-history --database data/fpl_history.sqlite3 \
  evaluate-captain-regret <run-id>
```

The comparator is deliberately narrow. Comparing against the highest scorer in the game
would measure player ranking, not captaincy — a manager can only captain someone they own
and who actually played. So the hindsight choice is the best of that Gameweek's own
scoring lineup after autosubs, and the model's side applies the real fallback: the vice
captains when the captain records no minutes, and nobody does when neither plays.

That measure grants a free wildcard at every origin, so it ranks selection methods rather
than estimating a reachable score. For the latter, replay the same run as one persistent
squad — carrying bank and free transfers forward and charging hits:

```bash
fpl-history --database data/fpl_history.sqlite3 \
  replay-transfer-continuity <run-id> --max-transfers-per-week 1
```

It reports per-Gameweek transfers, hits, autosub counts, bank, squad selling value and net
points, plus a `season_points` total and the final purchase-price ledger. Each week also
records the state its decision was taken from — owned squad, purchase prices, selling
prices, bank, free transfers, chip availability and the transfer limit.

`evaluate-transfer-regret` scores that replay as decisions rather than points:

```bash
fpl-history --database data/fpl_history.sqlite3 \
  evaluate-transfer-regret <run-id> --max-transfers-per-week 1
```

It reports two measures, kept apart deliberately:

- **`same_state_mean_regret`** is the gate metric. From the state that really existed each
  Gameweek, how much did the chosen action cost against the best action available from
  that *identical* state — same squad, prices, bank, free transfers and chip availability,
  with outcomes known only to the hindsight branch. It does not compound.
- **`continuous_policy_points`** is the season-representative one: what the carried squad
  actually scored. More meaningful to a manager, but it accumulates every earlier decision
  and forecast error.

The model chooses over its full forecast horizon while hindsight optimises the scored
Gameweek alone, so a horizon-aware model shows positive regret by construction; only the
change between matched runs at the same horizon is comparable. Neither branch plays a
chip, so availability is carried but never spent.

Sale values come from that ledger, not the market. A player is carried at the price they
were bought for, and selling applies the season's configured profit-sharing rule
(`selling_prices` in the season config — 2026/27 returns one tenth per two tenths of
profit, and takes losses in full). Players kept through a Wildcard retain their original
purchase price; a Free Hit squad is temporary and leaves the ledger untouched. Without
this the replay would spend money the real game never returns.

Chips follow a **declared policy that plays nothing by default**, so a replay stays
chip-free unless you deliberately switch one on — a weakly validated chip adviser left
running would obscure whether the underlying projections improved. Both branches are
scored under the same chip, so the transfer comparison stays a transfer comparison.

Only Bench Boost and Triple Captain are replayable. Their value is local: on a fixed
squad, the gain is what the bench or the extra captain multiple actually scored that
week. Wildcard and Free Hit change which squad exists, so their value depends on future
state and opportunity cost and the same argument does not reach them.

Two policies are available. The **threshold** policy plays the first week whose forecast
gain clears a bar — myopic by construction, and it will spend a chip the week before a
double Gameweek. The **look-ahead** policy values the chip in every projected Gameweek up
to the set's expiry and plays only when this week beats every later one still available,
so a double Gameweek pulls the chip toward it. `--lookahead-margin` biases toward waiting
when two weeks are close, and the look-ahead stops at the half-season boundary because a
first-half chip cannot be played in the second.

Each replayed week records how far the look-ahead could see and whether that reached the
expiry, so a short projection horizon is visible rather than silently assumed sufficient.

`evaluate-chip-regret` scores chip *timing*:

```bash
fpl-history --database data/fpl_history.sqlite3 evaluate-chip-regret <run-id> \
  --lookahead --lookahead-margin 1.0
```

Every replayed week records what each chip *would* have gained on the squad actually
owned, so the week chosen is scored against every alternative. A chip never played is
charged the full best available gain, because not playing it is itself a timing decision.
The best week is the best within the replayed window, so a short window understates the
alternative.

The candidate universe is fixed at the opening Gameweek.

The latest completed scorecard also appears in the app's Data Health view. Backtest runs
do not create ordinary production projection runs.

Install the `modeling` extra and tune only on the development period, with the selected
configuration evaluated separately on GW26–38:

```bash
fpl-history --database data/fpl.sqlite3 tune-projections 2025-26 \
  --development-start 2 \
  --development-end 25 \
  --validation-start 26 \
  --validation-end 38 \
  --trials 30
```

Optuna stores the study in `data/fpl_tuning.sqlite3`, while every trial's forecasts,
configuration, source-ingestion revision and data fingerprint remain auditable in the
main historical database. The objective prioritises top-100 points MAE and penalises
top-100 bias, overall error, minutes error and violations of the regulation match-minute
budget. The final output evaluates both the untuned v2 baseline and the selected
configuration on the same validation window and reports signed changes, where negative
error changes are improvements. Reusing the same study name resumes the study and adds
the requested number of trials.

The Stage 3a preseason priors — carry-forward regression, the promoted-club multipliers
and the price-aware cold-start bounds — were declared rather than estimated. Fit them on
development seasons only:

```bash
fpl-history --database data/fpl.sqlite3 fit-preseason-priors \
  --target-seasons 2022-23 2023-24 \
  --origin-start 1 --origin-end 8 --design-size 48 \
  --confirmation-horizons 8 \
  --config-output config/model_candidates/preseason-priors-v2.json
```

Only the six Stage 3a fields move; every other field is held at the base configuration, so
this is not a re-tune of the incumbent. The command refuses design-exhausted seasons
(2024/25, 2025/26), refuses forward seasons, and refuses a target season with no earlier
season in the database, since carry-forward would have nothing to read.

Three properties make the result readable rather than merely numeric:

- The search design is a **fixed Halton sequence generated before any outcome is seen**. An
  adaptive sampler proposes later points in response to results from every season, which
  would leave a withheld season having helped choose the points it is then scored on.
- The objective is **RMSE plus half the absolute bias**, not MAE. Decision metrics are
  reported beside it rather than driving selection.
- A parameter whose leading estimate sits within 5% of a search bound is reported as
  **censored**, not identified: there the bound chose the value.

Walk one parameter with the others held fixed to see whether an optimum is interior or
bound-seeking:

```bash
fpl-history --database data/fpl.sqlite3 profile-preseason-prior \
  promoted_team_attack_multiplier \
  --target-seasons 2022-23 2023-24 --low 0.55 --high 1.35 --steps 7
```

Then see whether the change alters a decision at all, and how stable that decision is:

```bash
fpl-history --database data/fpl.sqlite3 compare-opening-squads 2026-27 \
  --first-config config/model_candidates/preseason-priors-v1.json \
  --second-config config/model_candidates/preseason-priors-v2.json \
  --gameweek 1 --horizon 8 \
  --sensitivity-parameters carry_forward_regression_matches cold_start_maximum_factor
```

Each configuration values **both** squads on its own scale, because expected points from
two configurations are not comparable across scales; only the self-minus-other differences
are. The sensitivity block perturbs the named parameters and reports which players hold
their place under every variant.

A fitting attempt on 2022/23 and 2023/24 is recorded in
[the modelling plan](docs/09_ModellingAndEvaluationPlan.md) §6.1a. It improved both seasons
in-sample and at the unfitted horizon 8, but failed its out-of-season check on one of two
folds, so the declared v1 values stand.

The completed 2025/26 study, its selected trial, held-out results, data scope and
limitations are recorded in
[the projection tuning record](docs/05_ProjectionTuningRecord.md). It used three imported
seasons as potential historical evidence but evaluated model selection only on 2025/26;
it was not a five-season test.

The database now contains the complete 2021/22–2025/26 five-season window. The locked
rolling evaluation command, corrected-v4 default and team-news integration are documented
in [the strongest-model route](docs/06_StrongestModelRoute.md). The completed
five-season, eight-Gameweek evaluation and xG/xA challenger decision are recorded in
[the corrected-v4 model evaluation](docs/08_ModelEvaluation.md). The season-aware
bootstrap, calibration, residual-slice and prospective-capture results are recorded in
[the Stage 1 diagnostic results](docs/10_StageOneDiagnostics.md).

Football assumptions can be tested without reopening the locked holdout:

```bash
fpl-history --database data/fpl.sqlite3 audit-projection-assumptions \
  --development-seasons 2021-22 2022-23 2023-24 \
  --origin-start 2 --origin-end 38 --horizon 1
```

The audit compares the frozen robust-v4 reference with position-aware minutes, recent
scoring evidence, corrected threshold/penalty scoring and their combination. It also
compares median-target and mean-target learned losses on expanding chronological folds.
Its assumptions, metrics, gates and limitations are documented in
[the football-assumption audit](docs/07_FootballAssumptionAudit.md).

The required core CSV files are:

- `teams.csv`;
- `players.csv`;
- `player_seasons.csv`;
- `gameweeks.csv`;
- `fixtures.csv`;

The optional CSV files are:

- `player_fixture_stats.csv`;
- `player_gameweek_snapshots.csv`.

The snapshot file may include `source_team_id`, `selected_count`, `observation_kind`,
`timing_quality`, `observed_on` and `source_observation_key`. An `exact` observation
requires a timezone-aware `captured_at`; `date_only` uses `observed_on` and never
pretends that midnight is an exact capture time; `unknown` leaves both fields blank.

Each file uses the field names defined by the matching dataclass in
`src/fpl_engine/history/records.py`. Missing optional files are treated as empty.
Source-specific adapters should convert external datasets into this stable format rather
than placing source quirks inside the database layer.

## Repository structure

```text
config/seasons/           Versioned season rules
src/fpl_engine/           Core domain and rules code
src/fpl_engine/history/   Historical schema, records, import and query code
src/fpl_engine/live/      Official collection, transformation and reports
tests/                    Automated tests
docs/                     Product and implementation documentation
```

## Current milestone

Milestone 10 is in progress. The immediate focus is calibration from real 2026/27 snapshots,
automatic post-deadline reconciliation, richer uncertainty and expected-minutes modelling,
and season-long chip/transfer planning.
