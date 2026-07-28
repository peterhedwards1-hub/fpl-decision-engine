"""Load and validate the project-owned normalised historical CSV format."""

from __future__ import annotations

import csv
from collections.abc import Callable
from datetime import datetime
from math import isfinite
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

REQUIRED_FILES = (
    "teams.csv",
    "players.csv",
    "player_seasons.csv",
    "gameweeks.csv",
    "fixtures.csv",
)

OBSERVATION_KINDS = {
    "live_pre_deadline",
    "post_gameweek",
    "historical_reconstruction",
}
TIMING_QUALITIES = {"exact", "date_only", "unknown"}


class CsvBundleError(ValueError):
    """Raised when a CSV bundle violates its declared contract."""


def load_csv_bundle(directory: str | Path, season: SeasonRecord) -> HistoricalBundle:
    root = Path(directory)
    if not root.is_dir():
        raise FileNotFoundError(f"CSV bundle directory does not exist: {root}")
    missing = [name for name in REQUIRED_FILES if not (root / name).is_file()]
    if missing:
        raise CsvBundleError(
            f"CSV bundle is missing required files: {', '.join(missing)}"
        )

    bundle = HistoricalBundle(
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
    for filename, records in (
        ("teams.csv", bundle.teams),
        ("players.csv", bundle.players),
        ("player_seasons.csv", bundle.player_seasons),
        ("gameweeks.csv", bundle.gameweeks),
        ("fixtures.csv", bundle.fixtures),
    ):
        if not records:
            raise CsvBundleError(f"{filename}: required dataset is empty")
    _validate_references(bundle)
    return bundle


def _rows(
    path: Path,
    required_headers: set[str],
) -> list[tuple[int, dict[str, str | None]]]:
    if not path.exists():
        return []
    try:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            headers = reader.fieldnames or []
            duplicates = {header for header in headers if headers.count(header) > 1}
            if duplicates:
                raise CsvBundleError(
                    f"{path.name}: duplicate header(s): {', '.join(sorted(duplicates))}"
                )
            missing = required_headers - set(headers)
            if missing:
                raise CsvBundleError(
                    f"{path.name}: missing required header(s): {', '.join(sorted(missing))}"
                )
            rows = []
            for line_number, row in enumerate(reader, start=2):
                if None in row:
                    raise CsvBundleError(
                        f"{path.name}: row {line_number} has more values than headers"
                    )
                rows.append((line_number, row))
            return rows
    except UnicodeDecodeError as error:
        raise CsvBundleError(f"{path.name}: file is not valid UTF-8") from error


def _load_teams(path: Path) -> list[TeamRecord]:
    rows = _rows(path, {"source_team_id", "name", "short_name"})
    _ensure_unique(rows, path, lambda row: row["source_team_id"], "source_team_id")
    return [
        TeamRecord(row["source_team_id"] or "", row["name"] or "", row["short_name"] or "")
        for _, row in rows
    ]


def _load_players(path: Path) -> list[PlayerRecord]:
    rows = _rows(path, {"source_player_id", "web_name"})
    _ensure_unique(rows, path, lambda row: row["source_player_id"], "source_player_id")
    return [
        PlayerRecord(
            source_player_id=row["source_player_id"] or "",
            web_name=row["web_name"] or "",
            first_name=row.get("first_name") or "",
            second_name=row.get("second_name") or "",
            date_of_birth=_optional_text(row.get("date_of_birth")),
            official_fpl_code=_optional_text(row.get("official_fpl_code")),
            opta_code=_optional_text(row.get("opta_code")),
        )
        for _, row in rows
    ]


def _load_player_seasons(path: Path) -> list[PlayerSeasonRecord]:
    rows = _rows(path, {"source_player_id", "source_team_id", "position"})
    _ensure_unique(rows, path, lambda row: row["source_player_id"], "source_player_id")
    records = []
    for line, row in rows:
        position = _position(row["position"], path, line)
        records.append(
            PlayerSeasonRecord(
                source_player_id=row["source_player_id"] or "",
                source_team_id=row["source_team_id"] or "",
                position=position,
                start_price_tenths=_optional_int(
                    row.get("start_price_tenths"), path, line, "start_price_tenths"
                ),
                end_price_tenths=_optional_int(
                    row.get("end_price_tenths"), path, line, "end_price_tenths"
                ),
                identifier_namespace=_optional_text(row.get("identifier_namespace")),
            )
        )
    return records


def _load_gameweeks(path: Path) -> list[GameweekRecord]:
    rows = _rows(path, {"number"})
    _ensure_unique(rows, path, lambda row: row["number"], "number")
    return [
        GameweekRecord(
            number=_required_int(row.get("number"), path, line, "number"),
            deadline_time=_optional_text(row.get("deadline_time")),
            is_finished=_bool(row.get("is_finished"), path, line, "is_finished"),
        )
        for line, row in rows
    ]


def _load_fixtures(path: Path) -> list[FixtureRecord]:
    rows = _rows(
        path,
        {"source_fixture_id", "home_team_source_id", "away_team_source_id"},
    )
    _ensure_unique(rows, path, lambda row: row["source_fixture_id"], "source_fixture_id")
    return [
        FixtureRecord(
            source_fixture_id=row["source_fixture_id"] or "",
            home_team_source_id=row["home_team_source_id"] or "",
            away_team_source_id=row["away_team_source_id"] or "",
            gameweek_number=_optional_int(
                row.get("gameweek_number"), path, line, "gameweek_number"
            ),
            kickoff_time=_optional_text(row.get("kickoff_time")),
            home_score=_optional_int(row.get("home_score"), path, line, "home_score"),
            away_score=_optional_int(row.get("away_score"), path, line, "away_score"),
            finished=_bool(row.get("finished"), path, line, "finished"),
        )
        for line, row in rows
    ]


def _load_fixture_stats(path: Path) -> list[PlayerFixtureStatsRecord]:
    rows = _rows(path, {"source_player_id", "source_fixture_id"})
    _ensure_unique(
        rows,
        path,
        lambda row: (row["source_player_id"], row["source_fixture_id"]),
        "source_player_id/source_fixture_id",
    )
    records = []
    for line, row in rows:
        records.append(
            PlayerFixtureStatsRecord(
                source_player_id=row["source_player_id"] or "",
                source_fixture_id=row["source_fixture_id"] or "",
                minutes=_int(row, "minutes", path, line),
                starts=_bool(row.get("starts"), path, line, "starts"),
                goals=_int(row, "goals", path, line),
                assists=_int(row, "assists", path, line),
                clean_sheet=_bool(row.get("clean_sheet"), path, line, "clean_sheet"),
                goals_conceded=_int(row, "goals_conceded", path, line),
                own_goals=_int(row, "own_goals", path, line),
                penalties_saved=_int(row, "penalties_saved", path, line),
                penalties_missed=_int(row, "penalties_missed", path, line),
                yellow_cards=_int(row, "yellow_cards", path, line),
                red_cards=_int(row, "red_cards", path, line),
                saves=_int(row, "saves", path, line),
                bonus=_int(row, "bonus", path, line),
                bps=_int(row, "bps", path, line),
                defensive_contributions=_int(
                    row, "defensive_contributions", path, line
                ),
                expected_goals=_optional_float(
                    row.get("expected_goals"), path, line, "expected_goals"
                ),
                expected_assists=_optional_float(
                    row.get("expected_assists"), path, line, "expected_assists"
                ),
                expected_goal_involvements=_optional_float(
                    row.get("expected_goal_involvements"),
                    path,
                    line,
                    "expected_goal_involvements",
                ),
                expected_goals_conceded=_optional_float(
                    row.get("expected_goals_conceded"),
                    path,
                    line,
                    "expected_goals_conceded",
                ),
                total_points=_int(row, "total_points", path, line),
            )
        )
    return records


def _load_gameweek_snapshots(path: Path) -> list[PlayerGameweekSnapshotRecord]:
    rows = _rows(path, {"source_player_id", "gameweek_number", "price_tenths"})
    _ensure_unique(
        rows,
        path,
        lambda row: (
            row["source_player_id"],
            row["gameweek_number"],
            row.get("observation_kind") or "live_pre_deadline",
            row.get("source_observation_key") or row.get("captured_at"),
        ),
        "player/gameweek/observation",
    )
    records = []
    for line, row in rows:
        observation_kind = row.get("observation_kind") or "live_pre_deadline"
        timing_quality = row.get("timing_quality") or (
            "exact" if row.get("captured_at") else "unknown"
        )
        if observation_kind not in OBSERVATION_KINDS:
            raise CsvBundleError(
                f"{path.name}: row {line}, field observation_kind has invalid value "
                f"{observation_kind!r}"
            )
        if timing_quality not in TIMING_QUALITIES:
            raise CsvBundleError(
                f"{path.name}: row {line}, field timing_quality has invalid value "
                f"{timing_quality!r}"
            )
        records.append(
            PlayerGameweekSnapshotRecord(
                source_player_id=row["source_player_id"] or "",
                gameweek_number=_required_int(
                    row.get("gameweek_number"), path, line, "gameweek_number"
                ),
                price_tenths=_required_int(
                    row.get("price_tenths"), path, line, "price_tenths"
                ),
                captured_at=_timestamp(row.get("captured_at"), path, line),
                selected_by_percent=_optional_float(
                    row.get("selected_by_percent"),
                    path,
                    line,
                    "selected_by_percent",
                ),
                transfers_in=_optional_int(
                    row.get("transfers_in"), path, line, "transfers_in"
                ),
                transfers_out=_optional_int(
                    row.get("transfers_out"), path, line, "transfers_out"
                ),
                status=_optional_text(row.get("status")),
                chance_of_playing_next_round=_optional_int(
                    row.get("chance_of_playing_next_round"),
                    path,
                    line,
                    "chance_of_playing_next_round",
                ),
                news=_optional_text(row.get("news")),
                source_team_id=_optional_text(row.get("source_team_id")),
                selected_count=_optional_int(
                    row.get("selected_count"), path, line, "selected_count"
                ),
                observation_kind=observation_kind,
                timing_quality=timing_quality,
                source_observation_key=_optional_text(
                    row.get("source_observation_key")
                ),
            )
        )
    return records


def _validate_references(bundle: HistoricalBundle) -> None:
    team_ids = {record.source_team_id for record in bundle.teams}
    player_ids = {record.source_player_id for record in bundle.players}
    gameweek_numbers = {record.number for record in bundle.gameweeks}
    fixture_ids = {record.source_fixture_id for record in bundle.fixtures}

    for index, record in enumerate(bundle.player_seasons, start=2):
        if record.source_player_id not in player_ids:
            raise CsvBundleError(
                f"player_seasons.csv: row {index}, source_player_id "
                f"{record.source_player_id!r} is missing from players.csv"
            )
        if record.source_team_id not in team_ids:
            raise CsvBundleError(
                f"player_seasons.csv: row {index}, source_team_id "
                f"{record.source_team_id!r} is missing from teams.csv"
            )
    for index, record in enumerate(bundle.fixtures, start=2):
        if record.home_team_source_id not in team_ids:
            raise CsvBundleError(
                f"fixtures.csv: row {index}, home_team_source_id "
                f"{record.home_team_source_id!r} is missing from teams.csv"
            )
        if record.away_team_source_id not in team_ids:
            raise CsvBundleError(
                f"fixtures.csv: row {index}, away_team_source_id "
                f"{record.away_team_source_id!r} is missing from teams.csv"
            )
        if (
            record.gameweek_number is not None
            and record.gameweek_number not in gameweek_numbers
        ):
            raise CsvBundleError(
                f"fixtures.csv: row {index}, gameweek_number "
                f"{record.gameweek_number!r} is missing from gameweeks.csv"
            )
    for index, record in enumerate(bundle.fixture_stats, start=2):
        if record.source_player_id not in player_ids:
            raise CsvBundleError(
                f"player_fixture_stats.csv: row {index}, source_player_id "
                f"{record.source_player_id!r} is missing from players.csv"
            )
        if record.source_fixture_id not in fixture_ids:
            raise CsvBundleError(
                f"player_fixture_stats.csv: row {index}, source_fixture_id "
                f"{record.source_fixture_id!r} is missing from fixtures.csv"
            )
    for index, record in enumerate(bundle.gameweek_snapshots, start=2):
        if record.source_player_id not in player_ids:
            raise CsvBundleError(
                f"player_gameweek_snapshots.csv: row {index}, source_player_id "
                f"{record.source_player_id!r} is missing from players.csv"
            )
        if record.gameweek_number not in gameweek_numbers:
            raise CsvBundleError(
                f"player_gameweek_snapshots.csv: row {index}, gameweek_number "
                f"{record.gameweek_number!r} is missing from gameweeks.csv"
            )
        if record.source_team_id is not None and record.source_team_id not in team_ids:
            raise CsvBundleError(
                f"player_gameweek_snapshots.csv: row {index}, source_team_id "
                f"{record.source_team_id!r} is missing from teams.csv"
            )


def _ensure_unique(
    rows: list[tuple[int, dict[str, str | None]]],
    path: Path,
    key: Callable[[dict[str, str | None]], Any],
    label: str,
) -> None:
    seen: dict[Any, int] = {}
    for line, row in rows:
        value = key(row)
        if value in seen:
            raise CsvBundleError(
                f"{path.name}: row {line}, duplicate {label} value {value!r}; "
                f"first seen on row {seen[value]}"
            )
        seen[value] = line


def _position(value: str | None, path: Path, line: int) -> Position:
    try:
        return Position(value or "")
    except ValueError as error:
        raise CsvBundleError(
            f"{path.name}: row {line}, field position has invalid value {value!r}"
        ) from error


def _int(
    row: dict[str, str | None], key: str, path: Path, line: int, default: int = 0
) -> int:
    value = row.get(key)
    if value in (None, ""):
        return default
    return _required_int(value, path, line, key)


def _required_int(value: str | None, path: Path, line: int, key: str) -> int:
    if value in (None, ""):
        raise CsvBundleError(f"{path.name}: row {line}, field {key} is required")
    try:
        return int(value)
    except ValueError as error:
        raise CsvBundleError(
            f"{path.name}: row {line}, field {key} has invalid integer {value!r}"
        ) from error


def _optional_int(
    value: str | None, path: Path, line: int, key: str
) -> int | None:
    if value in (None, ""):
        return None
    return _required_int(value, path, line, key)


def _optional_float(
    value: str | None, path: Path, line: int, key: str
) -> float | None:
    if value in (None, ""):
        return None
    try:
        parsed = float(value)
        if not isfinite(parsed):
            raise ValueError("non-finite float")
        return parsed
    except ValueError as error:
        raise CsvBundleError(
            f"{path.name}: row {line}, field {key} has invalid float {value!r}"
        ) from error


def _bool(value: str | None, path: Path, line: int, key: str) -> bool:
    if value in (None, ""):
        return False
    normalised = value.strip().lower()
    if normalised in {"1", "true", "yes", "y"}:
        return True
    if normalised in {"0", "false", "no", "n"}:
        return False
    raise CsvBundleError(
        f"{path.name}: row {line}, field {key} has invalid boolean {value!r}"
    )


def _timestamp(value: str | None, path: Path, line: int) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError as error:
        raise CsvBundleError(
            f"{path.name}: row {line}, field captured_at has invalid timestamp {value!r}"
        ) from error


def _optional_text(value: str | None) -> str | None:
    return value if value not in (None, "") else None
