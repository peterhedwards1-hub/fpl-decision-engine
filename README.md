# FPL Decision Engine

A personal, reproducible Fantasy Premier League decision-support system.

## Current status

Milestones 0 and 1 establish the rules, domain and historical-data foundations used by
later collection, projection, optimisation and front-end work.

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
- automated tests and GitHub Actions CI.

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

## Rules usage

```python
from pathlib import Path

from fpl_engine import load_season_rules

rules = load_season_rules(Path("config/seasons/2026-27.json"))
print(rules.squad.budget_tenths)  # 1000 = £100.0m
```

Prices and budgets are stored as integer tenths to avoid floating-point money errors.

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

The optional CSV files are:

- `teams.csv`;
- `players.csv`;
- `player_seasons.csv`;
- `gameweeks.csv`;
- `fixtures.csv`;
- `player_fixture_stats.csv`;
- `player_gameweek_snapshots.csv`.

Each file uses the field names defined by the matching dataclass in
`src/fpl_engine/history/records.py`. Missing optional files are treated as empty.
Source-specific adapters should convert external datasets into this stable format rather
than placing source quirks inside the database layer.

## Repository structure

```text
config/seasons/           Versioned season rules
src/fpl_engine/           Core domain and rules code
src/fpl_engine/history/   Historical schema, records, import and query code
tests/                    Automated tests
docs/                     Product and implementation documentation
```

## Next milestone

Milestone 2 will collect current-season official data into immutable, timestamped raw
snapshots and normalise it into the historical database.
