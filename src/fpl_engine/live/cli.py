"""Command-line entry point for collecting an official FPL snapshot."""

from __future__ import annotations

import argparse
from pathlib import Path

from fpl_engine.history.database import HistoricalDatabase

from .collector import LiveSnapshotCollector


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect the current official FPL snapshot")
    parser.add_argument("--database", type=Path, default=Path("data/fpl.sqlite3"))
    parser.add_argument("--archive-root", type=Path, default=Path("data/raw/fpl"))
    parser.add_argument("--season-code", required=True, help="For example 2026-27")
    parser.add_argument("--season-name", help="Human-readable name; defaults to season code")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.database.parent.mkdir(parents=True, exist_ok=True)
    with HistoricalDatabase(args.database) as database:
        database.initialise()
        result = LiveSnapshotCollector(
            database,
            archive_root=args.archive_root,
        ).collect(
            season_code=args.season_code,
            season_name=args.season_name,
        )
    print(
        f"Collected {result.players} players, {result.teams} teams and "
        f"{result.fixtures} fixtures for GW{result.gameweek_number}."
    )
    print(f"Ingestion run: {result.ingestion_run_id}")
    print(f"Raw archive: {result.archive_directory}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
