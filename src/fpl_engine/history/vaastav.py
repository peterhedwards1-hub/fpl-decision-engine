"""Adapter for the permitted Vaastav FPL historical CSV dataset."""

from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass
from datetime import date
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

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

POSITION_BY_ELEMENT_TYPE = {
    "1": Position.GK,
    "2": Position.DEF,
    "3": Position.MID,
    "4": Position.FWD,
}


class VaastavImportError(ValueError):
    """Raised when the upstream historical contract is missing or inconsistent."""


class VaastavSource(Protocol):
    def fetch(self, source_ref: str, season_code: str, path: str) -> bytes: ...


@dataclass(frozen=True)
class VaastavLoadResult:
    bundle: HistoricalBundle
    content_sha256: str
    source_files: tuple[str, ...]
    quality: VaastavQualityReport


@dataclass(frozen=True)
class VaastavQualityReport:
    teams: int
    players: int
    gameweeks: int
    fixtures: int
    fixture_stats: int
    gameweek_observations: int
    skipped_rescheduled_rows: int
    players_without_gameweek_rows: int


class VaastavClient:
    """Fetch raw source files at an immutable Git revision."""

    def __init__(
        self,
        *,
        base_url: str = (
            "https://raw.githubusercontent.com/"
            "vaastav/Fantasy-Premier-League"
        ),
        timeout_seconds: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def fetch(self, source_ref: str, season_code: str, path: str) -> bytes:
        url = f"{self.base_url}/{source_ref}/data/{season_code}/{path}"
        request = Request(
            url,
            headers={
                "Accept": "text/csv",
                "User-Agent": "fpl-decision-engine/0.3",
            },
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                return response.read()
        except (HTTPError, URLError, TimeoutError) as error:
            raise VaastavImportError(f"Could not fetch {url}: {error}") from error


class VaastavAdapter:
    """Translate source-specific Vaastav files into canonical typed records."""

    def __init__(self, source: VaastavSource | None = None) -> None:
        self.source = source or VaastavClient()

    def load_season(
        self,
        *,
        source_ref: str,
        season_code: str,
        season_name: str | None = None,
    ) -> VaastavLoadResult:
        if not source_ref or source_ref in {"main", "master", "HEAD"}:
            raise VaastavImportError(
                "source_ref must be an immutable commit SHA, not a moving branch"
            )

        fetched: dict[str, bytes] = {}

        def fetch(path: str) -> bytes:
            content = self.source.fetch(source_ref, season_code, path)
            fetched[path] = content
            return content

        team_rows = _csv_rows(fetch("teams.csv"), "teams.csv")
        player_rows = _csv_rows(fetch("players_raw.csv"), "players_raw.csv")
        fixture_rows = _csv_rows(fetch("fixtures.csv"), "fixtures.csv")
        gameweek_numbers = sorted(
            {
                _required_int(row, "event", "fixtures.csv")
                for row in fixture_rows
                if _text(row.get("event")) is not None
            }
        )
        if not gameweek_numbers:
            raise VaastavImportError("fixtures.csv contains no assigned Gameweeks")

        gameweek_rows: list[dict[str, str]] = []
        for gameweek_number in gameweek_numbers:
            path = f"gws/gw{gameweek_number}.csv"
            rows = _csv_rows(fetch(path), path)
            for row in rows:
                row_number = _required_int(row, "round", path)
                if row_number != gameweek_number:
                    raise VaastavImportError(
                        f"{path} contains a row for Gameweek {row_number}"
                    )
            gameweek_rows.extend(rows)

        bundle = _build_bundle(
            season_code=season_code,
            season_name=season_name or _season_name(season_code),
            team_rows=team_rows,
            player_rows=player_rows,
            fixture_rows=fixture_rows,
            gameweek_rows=gameweek_rows,
        )
        digest = hashlib.sha256()
        for path in sorted(fetched):
            digest.update(path.encode("utf-8"))
            digest.update(b"\0")
            digest.update(fetched[path])
        return VaastavLoadResult(
            bundle=bundle,
            content_sha256=digest.hexdigest(),
            source_files=tuple(sorted(fetched)),
            quality=VaastavQualityReport(
                teams=len(bundle.teams),
                players=len(bundle.players),
                gameweeks=len(bundle.gameweeks),
                fixtures=len(bundle.fixtures),
                fixture_stats=len(bundle.fixture_stats),
                gameweek_observations=len(bundle.gameweek_snapshots),
                skipped_rescheduled_rows=(
                    len(gameweek_rows) - len(bundle.fixture_stats)
                ),
                players_without_gameweek_rows=(
                    len(bundle.players)
                    - len(
                        {
                            observation.source_player_id
                            for observation in bundle.gameweek_snapshots
                        }
                    )
                ),
            ),
        )


def _build_bundle(
    *,
    season_code: str,
    season_name: str,
    team_rows: list[dict[str, str]],
    player_rows: list[dict[str, str]],
    fixture_rows: list[dict[str, str]],
    gameweek_rows: list[dict[str, str]],
) -> HistoricalBundle:
    teams = tuple(
        TeamRecord(
            source_team_id=str(_required_int(row, "id", "teams.csv")),
            name=_required_text(row, "name", "teams.csv"),
            short_name=_required_text(row, "short_name", "teams.csv"),
        )
        for row in team_rows
    )
    team_ids = {team.source_team_id for team in teams}
    if len(team_ids) != len(teams):
        raise VaastavImportError("teams.csv contains duplicate team IDs")

    players = tuple(_player_record(row) for row in player_rows)
    player_rows_by_id = {
        str(_required_int(row, "id", "players_raw.csv")): row
        for row in player_rows
    }
    if len(player_rows_by_id) != len(player_rows):
        raise VaastavImportError("players_raw.csv contains duplicate player IDs")

    fixtures = tuple(_fixture_record(row, team_ids) for row in fixture_rows)
    fixture_rows_by_id = {
        fixture.source_fixture_id: row
        for fixture, row in zip(fixtures, fixture_rows, strict=True)
    }
    if len(fixture_rows_by_id) != len(fixture_rows):
        raise VaastavImportError("fixtures.csv contains duplicate fixture IDs")

    sorted_gw_rows = sorted(
        gameweek_rows,
        key=lambda row: (
            _required_int(row, "round", "Gameweek CSV"),
            _required_int(row, "fixture", "Gameweek CSV"),
            _required_int(row, "element", "Gameweek CSV"),
        ),
    )
    earliest_player_rows: dict[str, dict[str, str]] = {}
    latest_player_rows: dict[str, dict[str, str]] = {}
    fixture_stats_by_key: dict[
        tuple[str, str], PlayerFixtureStatsRecord
    ] = {}
    snapshots_by_key: dict[tuple[str, int], PlayerGameweekSnapshotRecord] = {}

    for row in sorted_gw_rows:
        player_id = str(_required_int(row, "element", "Gameweek CSV"))
        fixture_id = str(_required_int(row, "fixture", "Gameweek CSV"))
        gameweek_number = _required_int(row, "round", "Gameweek CSV")
        if player_id not in player_rows_by_id:
            raise VaastavImportError(
                f"Gameweek data references unknown player {player_id}"
            )
        if fixture_id not in fixture_rows_by_id:
            raise VaastavImportError(
                f"Gameweek data references unknown fixture {fixture_id}"
            )
        fixture = fixture_rows_by_id[fixture_id]
        team_id = _fixture_team_for_player(row, fixture)
        earliest_player_rows.setdefault(player_id, row)
        latest_player_rows[player_id] = row

        fixture_gameweek_number = _required_int(
            fixture, "event", "fixtures.csv"
        )
        stats_record = PlayerFixtureStatsRecord(
            source_player_id=player_id,
            source_fixture_id=fixture_id,
            minutes=_int(row, "minutes", 0),
            starts=_bool(row, "starts", False),
            goals=_int(row, "goals_scored", 0),
            assists=_int(row, "assists", 0),
            clean_sheet=_bool(row, "clean_sheets", False),
            goals_conceded=_int(row, "goals_conceded", 0),
            own_goals=_int(row, "own_goals", 0),
            penalties_saved=_int(row, "penalties_saved", 0),
            penalties_missed=_int(row, "penalties_missed", 0),
            yellow_cards=_int(row, "yellow_cards", 0),
            red_cards=_int(row, "red_cards", 0),
            saves=_int(row, "saves", 0),
            bonus=_int(row, "bonus", 0),
            bps=_int(row, "bps", 0),
            defensive_contributions=_int(
                row, "defensive_contribution", 0
            ),
            expected_goals=_optional_float(row, "expected_goals"),
            expected_assists=_optional_float(row, "expected_assists"),
            expected_goal_involvements=_optional_float(
                row, "expected_goal_involvements"
            ),
            expected_goals_conceded=_optional_float(
                row, "expected_goals_conceded"
            ),
            total_points=_int(row, "total_points", 0),
        )
        if fixture_gameweek_number == gameweek_number:
            stats_key = (player_id, fixture_id)
            existing_stats = fixture_stats_by_key.get(stats_key)
            if existing_stats is not None and existing_stats != stats_record:
                raise VaastavImportError(
                    "Conflicting duplicate performance rows for "
                    f"player {player_id}, fixture {fixture_id}"
                )
            fixture_stats_by_key[stats_key] = stats_record

        snapshot = PlayerGameweekSnapshotRecord(
            source_player_id=player_id,
            gameweek_number=gameweek_number,
            price_tenths=_required_int(row, "value", "Gameweek CSV"),
            captured_at=None,
            source_team_id=team_id,
            selected_count=_optional_int(row, "selected"),
            transfers_in=_optional_int(row, "transfers_in"),
            transfers_out=_optional_int(row, "transfers_out"),
            observation_kind="historical_reconstruction",
            timing_quality="unknown",
            source_observation_key=(
                f"vaastav-{season_code}-gw{gameweek_number}-player{player_id}"
            ),
        )
        snapshot_key = (player_id, gameweek_number)
        existing_snapshot = snapshots_by_key.get(snapshot_key)
        if existing_snapshot is not None and _snapshot_values(
            existing_snapshot
        ) != _snapshot_values(snapshot):
            raise VaastavImportError(
                "Double-Gameweek rows disagree on snapshot values for "
                f"player {player_id}, Gameweek {gameweek_number}"
            )
        snapshots_by_key[snapshot_key] = snapshot

    player_seasons: list[PlayerSeasonRecord] = []
    for player_id, player_row in player_rows_by_id.items():
        first_row = earliest_player_rows.get(player_id)
        last_row = latest_player_rows.get(player_id)
        team_id = (
            _fixture_team_for_player(
                first_row,
                fixture_rows_by_id[
                    str(_required_int(first_row, "fixture", "Gameweek CSV"))
                ],
            )
            if first_row is not None
            else str(_required_int(player_row, "team", "players_raw.csv"))
        )
        if team_id not in team_ids:
            raise VaastavImportError(
                f"Player {player_id} references unknown team {team_id}"
            )
        position_value = (
            _required_text(first_row, "position", "Gameweek CSV")
            if first_row is not None
            else _required_text(player_row, "element_type", "players_raw.csv")
        )
        position = _position(position_value)
        start_price = (
            _required_int(first_row, "value", "Gameweek CSV")
            if first_row is not None
            else None
        )
        end_price = (
            _required_int(last_row, "value", "Gameweek CSV")
            if last_row is not None
            else _optional_int(player_row, "now_cost")
        )
        player_seasons.append(
            PlayerSeasonRecord(
                source_player_id=player_id,
                source_team_id=team_id,
                position=position,
                start_price_tenths=start_price,
                end_price_tenths=end_price,
            )
        )

    gameweek_numbers = sorted(
        {_required_int(row, "event", "fixtures.csv") for row in fixture_rows}
    )
    gameweeks = tuple(
        GameweekRecord(
            number=number,
            deadline_time=None,
            is_finished=all(
                _bool(row, "finished", False)
                for row in fixture_rows
                if _required_int(row, "event", "fixtures.csv") == number
            ),
        )
        for number in gameweek_numbers
    )
    kickoff_dates = [
        _required_text(row, "kickoff_time", "fixtures.csv")[:10]
        for row in fixture_rows
        if _text(row.get("kickoff_time")) is not None
    ]
    season = SeasonRecord(
        code=season_code,
        name=season_name,
        starts_on=min(kickoff_dates) if kickoff_dates else None,
        ends_on=max(kickoff_dates) if kickoff_dates else None,
    )
    return HistoricalBundle(
        season=season,
        teams=teams,
        players=players,
        player_seasons=tuple(player_seasons),
        gameweeks=gameweeks,
        fixtures=fixtures,
        fixture_stats=tuple(fixture_stats_by_key.values()),
        gameweek_snapshots=tuple(snapshots_by_key.values()),
    )


def _player_record(row: dict[str, str]) -> PlayerRecord:
    player_id = str(_required_int(row, "id", "players_raw.csv"))
    temporary_code = _bool(row, "has_temporary_code", False)
    official_code = None if temporary_code else _text(row.get("code"))
    return PlayerRecord(
        source_player_id=player_id,
        first_name=_text(row.get("first_name")) or "",
        second_name=_text(row.get("second_name")) or "",
        web_name=_required_text(row, "web_name", "players_raw.csv"),
        date_of_birth=_valid_date_or_none(row.get("birth_date")),
        official_fpl_code=official_code,
        opta_code=_text(row.get("opta_code")),
    )


def _fixture_record(
    row: dict[str, str], team_ids: set[str]
) -> FixtureRecord:
    fixture_id = str(_required_int(row, "id", "fixtures.csv"))
    home_team_id = str(_required_int(row, "team_h", "fixtures.csv"))
    away_team_id = str(_required_int(row, "team_a", "fixtures.csv"))
    if home_team_id not in team_ids or away_team_id not in team_ids:
        raise VaastavImportError(
            f"Fixture {fixture_id} references an unknown team"
        )
    return FixtureRecord(
        source_fixture_id=fixture_id,
        home_team_source_id=home_team_id,
        away_team_source_id=away_team_id,
        gameweek_number=_required_int(row, "event", "fixtures.csv"),
        kickoff_time=_text(row.get("kickoff_time")),
        home_score=_optional_int(row, "team_h_score"),
        away_score=_optional_int(row, "team_a_score"),
        finished=_bool(row, "finished", False),
    )


def _fixture_team_for_player(
    row: dict[str, str], fixture: dict[str, str]
) -> str:
    key = "team_h" if _bool(row, "was_home", False) else "team_a"
    return str(_required_int(fixture, key, "fixtures.csv"))


def _snapshot_values(
    record: PlayerGameweekSnapshotRecord,
) -> tuple[object, ...]:
    return (
        record.price_tenths,
        record.source_team_id,
        record.selected_count,
        record.transfers_in,
        record.transfers_out,
    )


def _csv_rows(content: bytes, path: str) -> list[dict[str, str]]:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise VaastavImportError(f"{path} is not valid UTF-8") from error
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise VaastavImportError(f"{path} has no CSV header")
    rows = list(reader)
    if not rows:
        raise VaastavImportError(f"{path} contains no data rows")
    return rows


def _season_name(season_code: str) -> str:
    parts = season_code.split("-")
    if len(parts) != 2 or len(parts[0]) != 4 or len(parts[1]) != 2:
        raise VaastavImportError(
            f"Season code {season_code!r} must look like 2025-26"
        )
    return f"{parts[0]}/{parts[1]}"


def _position(value: str) -> Position:
    normalized = value.strip().upper()
    if normalized in {position.value for position in Position}:
        return Position(normalized)
    try:
        return POSITION_BY_ELEMENT_TYPE[normalized]
    except KeyError as error:
        raise VaastavImportError(f"Unknown player position {value!r}") from error


def _valid_date_or_none(value: str | None) -> str | None:
    normalized = _text(value)
    if normalized is None:
        return None
    try:
        return date.fromisoformat(normalized).isoformat()
    except ValueError as error:
        raise VaastavImportError(f"Invalid birth date {normalized!r}") from error


def _required_text(
    row: dict[str, str] | None, key: str, path: str
) -> str:
    if row is None:
        raise VaastavImportError(f"{path} row is missing")
    value = _text(row.get(key))
    if value is None:
        raise VaastavImportError(f"{path} has a blank required {key!r} value")
    return value


def _required_int(row: dict[str, str], key: str, path: str) -> int:
    value = _text(row.get(key))
    if value is None:
        raise VaastavImportError(f"{path} has a blank required {key!r} value")
    try:
        return int(value)
    except ValueError as error:
        raise VaastavImportError(
            f"{path} has invalid integer {key}={value!r}"
        ) from error


def _int(row: dict[str, str], key: str, default: int) -> int:
    value = _text(row.get(key))
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as error:
        raise VaastavImportError(f"Invalid integer {key}={value!r}") from error


def _optional_int(row: dict[str, str], key: str) -> int | None:
    value = _text(row.get(key))
    if value is None:
        return None
    try:
        return int(value)
    except ValueError as error:
        raise VaastavImportError(f"Invalid integer {key}={value!r}") from error


def _optional_float(row: dict[str, str], key: str) -> float | None:
    value = _text(row.get(key))
    if value is None:
        return None
    try:
        return float(value)
    except ValueError as error:
        raise VaastavImportError(f"Invalid float {key}={value!r}") from error


def _bool(row: dict[str, str], key: str, default: bool) -> bool:
    value = _text(row.get(key))
    if value is None:
        return default
    normalized = value.lower()
    if normalized in {"true", "1"}:
        return True
    if normalized in {"false", "0"}:
        return False
    raise VaastavImportError(f"Invalid boolean {key}={value!r}")


def _text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return None if normalized in {"", "None", "null", "NULL"} else normalized
