"""Load a normalised historical-data bundle from CSV files.

This adapter intentionally expects stable project-owned column names. Source-specific
adapters can transform public datasets into this format without leaking their quirks
into the database layer.
"""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from typing import Any

from fpl_engine.domain import Position

from .records import (
    FixtureRecord,
    GameweekRecord,
    HistoricalBundle,
    PlayerFixtureStatsRecord,
    PlayerGameweekSnapshotRecord,
    PlayerRecord,
    PlayerSeasonRecord,
    SeasonRecord,
    TeamRecord,
)


def load_csv_bundle(directory: str | Path, season: SeasonRecord) -> HistoricalBundle:
    root = Path(directory)
    return HistoricalBundle(
        season=season,
        teams=tuple(_load_teams(root / "teams.csv")),
        players=tuple(_load_players(root / "players.csv")),
        player_seasons=tuple(_load_player_seasons(root / "player_seasons.csv")),
        gameweeks=tuple(_load_gameweeks(root / "gameweeks.csv")),
        fixtures=tuple(_load_fixtures(root / "fixtures.csv")),
        fixture_stats=tuple(
            _load_fixture_stats(root / "player_fixture_stats.csv")
        ),
        gameweek_snapshots=tuple(
            _load_gameweek_snapshots(root / "player_gameweek_snapshots.csv")
        ),
    )


def _rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _load_teams(path: Path) -> list[TeamRecord]:
    return [
        TeamRecord(row["source_team_id"], row["name"], row["short_name"])
        for row in _rows(path)
    ]


def _load_players(path: Path) -> list[PlayerRecord]:
    return [
        PlayerRecord(
            source_player_id=row["source_player_id"],
            web_name=row["web_name"],
            first_name=row.get("first_name", ""),
            second_name=row.get("second_name", ""),
            date_of_birth=_optional(row.get("date_of_birth")),
        )
        for row in _rows(path)
    ]


def _load_player_seasons(path: Path) -> list[PlayerSeasonRecord]:
    return [
        PlayerSeasonRecord(
            source_player_id=row["source_player_id"],
            source_team_id=row["source_team_id"],
            position=Position(row["position"]),
            start_price_tenths=_optional_int(row.get("start_price_tenths")),
            end_price_tenths=_optional_int(row.get("end_price_tenths")),
        )
        for row in _rows(path)
    ]


def _load_gameweeks(path: Path) -> list[GameweekRecord]:
    return [
        GameweekRecord(
            number=int(row["number"]),
            deadline_time=_optional(row.get("deadline_time")),
            is_finished=_bool(row.get("is_finished")),
        )
        for row in _rows(path)
    ]


def _load_fixtures(path: Path) -> list[FixtureRecord]:
    return [
        FixtureRecord(
            source_fixture_id=row["source_fixture_id"],
            home_team_source_id=row["home_team_source_id"],
            away_team_source_id=row["away_team_source_id"],
            gameweek_number=_optional_int(row.get("gameweek_number")),
            kickoff_time=_optional(row.get("kickoff_time")),
            home_score=_optional_int(row.get("home_score")),
            away_score=_optional_int(row.get("away_score")),
            finished=_bool(row.get("finished")),
        )
        for row in _rows(path)
    ]


def _load_fixture_stats(path: Path) -> list[PlayerFixtureStatsRecord]:
    records: list[PlayerFixtureStatsRecord] = []
    for row in _rows(path):
        records.append(
            PlayerFixtureStatsRecord(
                source_player_id=row["source_player_id"],
                source_fixture_id=row["source_fixture_id"],
                minutes=_int(row, "minutes"),
                starts=_bool(row.get("starts")),
                goals=_int(row, "goals"),
                assists=_int(row, "assists"),
                clean_sheet=_bool(row.get("clean_sheet")),
                goals_conceded=_int(row, "goals_conceded"),
                own_goals=_int(row, "own_goals"),
                penalties_saved=_int(row, "penalties_saved"),
                penalties_missed=_int(row, "penalties_missed"),
                yellow_cards=_int(row, "yellow_cards"),
                red_cards=_int(row, "red_cards"),
                saves=_int(row, "saves"),
                bonus=_int(row, "bonus"),
                bps=_int(row, "bps"),
                defensive_contributions=_int(row, "defensive_contributions"),
                expected_goals=_optional_float(row.get("expected_goals")),
                expected_assists=_optional_float(row.get("expected_assists")),
                expected_goal_involvements=_optional_float(
                    row.get("expected_goal_involvements")
                ),
                expected_goals_conceded=_optional_float(
                    row.get("expected_goals_conceded")
                ),
                total_points=_int(row, "total_points"),
            )
        )
    return records


def _load_gameweek_snapshots(path: Path) -> list[PlayerGameweekSnapshotRecord]:
    return [
        PlayerGameweekSnapshotRecord(
            source_player_id=row["source_player_id"],
            gameweek_number=int(row["gameweek_number"]),
            price_tenths=int(row["price_tenths"]),
            captured_at=datetime.fromisoformat(row["captured_at"]),
            selected_by_percent=_optional_float(row.get("selected_by_percent")),
            transfers_in=_optional_int(row.get("transfers_in")),
            transfers_out=_optional_int(row.get("transfers_out")),
            status=_optional(row.get("status")),
            chance_of_playing_next_round=_optional_int(
                row.get("chance_of_playing_next_round")
            ),
            news=_optional(row.get("news")),
        )
        for row in _rows(path)
    ]


def _optional(value: str | None) -> str | None:
    return value if value not in (None, "") else None


def _optional_int(value: str | None) -> int | None:
    return int(value) if value not in (None, "") else None


def _optional_float(value: str | None) -> float | None:
    return float(value) if value not in (None, "") else None


def _int(row: dict[str, Any], key: str) -> int:
    value = row.get(key)
    return int(value) if value not in (None, "") else 0


def _bool(value: str | None) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y"}
