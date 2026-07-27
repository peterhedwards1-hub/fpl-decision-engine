# FPL Decision Engine

A personal, reproducible Fantasy Premier League decision-support system.

## Current status

Milestone 0 establishes the rules and domain foundation used by later data, projection,
optimisation and front-end work.

Implemented:

- versioned 2026/27 season configuration;
- typed player, squad and Gameweek-stat domain objects;
- complete squad and starting-XI validation;
- budget, club-limit, formation, captain and vice-captain checks;
- deterministic player-points calculation;
- defensive-contribution scoring support;
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

## Core usage

```python
from pathlib import Path

from fpl_engine import load_season_rules

rules = load_season_rules(Path("config/seasons/2026-27.json"))
print(rules.squad.budget_tenths)  # 1000 = £100.0m
```

Prices and budgets are stored as integer tenths to avoid floating-point money errors.

## Repository structure

```text
config/seasons/       Versioned season rules
src/fpl_engine/       Core domain and rules code
tests/                Automated tests
docs/                 Product and implementation documentation
```

## Next milestone

Milestone 1 will define and populate the historical database while preserving source,
season and snapshot provenance.
