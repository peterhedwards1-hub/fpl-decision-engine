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
- exact XI, full-squad, opening-squad and transfer optimisation;
- bench, autosub, captaincy and chip-specific decision support;
- provisional/final weekly audit records and model-health scoring;
- leakage-controlled walk-forward projection backtesting with persisted scorecards;
- automated tests and GitHub Actions CI.

Schema version 10 includes the hardened identity and observation foundation, fixture-state
history, manager state, projections, weekly decision records and historical backtests. The
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
transfer comparison, weekly evidence/review and model-health views.

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
A completed report can
be printed again with:

```bash
fpl-history --database data/fpl_history.sqlite3 backtest-report <run-id>
```

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

The completed 2025/26 study, its selected trial, held-out results, data scope and
limitations are recorded in
[the projection tuning record](docs/05_ProjectionTuningRecord.md). It used three imported
seasons as potential historical evidence but evaluated model selection only on 2025/26;
it was not a five-season test.

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
