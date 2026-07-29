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

- versioned 2026/27 season configuration;
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
- automated tests and GitHub Actions CI.

Schema version 8 includes the hardened identity and observation foundation, fixture-state
history, manager state, projections and weekly decision records. The database supports
in-place version-2 migration, stable player
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
python -m pip install -e '.[dev]'
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
