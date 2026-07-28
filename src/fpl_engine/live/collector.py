"""Transform and persist a current official FPL snapshot."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from fpl_engine.domain import Position
from fpl_engine.history.database import HistoricalDatabase
from fpl_engine.history.records import (
    FixtureRecord,
    GameweekRecord,
    HistoricalBundle,
    IngestionSource,
    PlayerGameweekSnapshotRecord,
    PlayerRecord,
    PlayerSeasonRecord,
    SeasonRecord,
    TeamRecord,
)

from .client import ApiPayload, FplApiClient
from .report import write_verification_report

POSITION_BY_ELEMENT_TYPE = {
    1: Position.GK,
    2: Position.DEF,
    3: Position.MID,
    4: Position.FWD,
}


class SnapshotClient(Protocol):
    def bootstrap_static(self) -> ApiPayload: ...

    def fixtures(self) -> ApiPayload: ...


@dataclass(frozen=True)
class CollectionResult:
    ingestion_run_id: int
    archive_directory: Path
    report_directory: Path
    report_index: Path
    latest_report_index: Path
    season_code: str
    gameweek_number: int
    teams: int
    players: int
    fixtures: int


class LiveSnapshotCollector:
    """Collects bootstrap and fixture data as one reproducible snapshot."""

    def __init__(
        self,
        database: HistoricalDatabase,
        *,
        archive_root: str | Path = "data/raw/fpl",
        report_root: str | Path = "data/reports/fpl",
        client: SnapshotClient | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.database = database
        self.archive_root = Path(archive_root)
        self.report_root = Path(report_root)
        self.client = client or FplApiClient()
        self.clock = clock or (lambda: datetime.now(UTC))

    def collect(
        self, *, season_code: str, season_name: str | None = None
    ) -> CollectionResult:
        captured_at = self.clock()
        bootstrap = self.client.bootstrap_static()
        fixtures = self.client.fixtures()
        self._validate_payloads(bootstrap.data, fixtures.data)
        gameweek_number = self._snapshot_gameweek(bootstrap.data["events"])

        digest = hashlib.sha256(bootstrap.body + b"\n" + fixtures.body).hexdigest()
        archive_directory = self._archive(
            captured_at=captured_at,
            season_code=season_code,
            bootstrap=bootstrap,
            fixtures=fixtures,
            digest=digest,
        )
        bundle = self._build_bundle(
            season_code=season_code,
            season_name=season_name or season_code,
            gameweek_number=gameweek_number,
            captured_at=captured_at,
            bootstrap=bootstrap.data,
            fixtures=fixtures.data,
        )
        source = IngestionSource(
            name="official-fpl-api",
            retrieved_at=captured_at,
            url=bootstrap.url,
            content_sha256=digest,
        )
        run_id = self.database.ingest_bundle(source, bundle)
        report = write_verification_report(
            self.database,
            report_root=self.report_root,
            season_code=season_code,
            gameweek_number=gameweek_number,
            captured_at=captured_at,
            ingestion_run_id=run_id,
            archive_directory=archive_directory,
        )
        return CollectionResult(
            ingestion_run_id=run_id,
            archive_directory=archive_directory,
            report_directory=report.directory,
            report_index=report.index_path,
            latest_report_index=report.latest_index_path,
            season_code=season_code,
            gameweek_number=gameweek_number,
            teams=len(bundle.teams),
            players=len(bundle.players),
            fixtures=len(bundle.fixtures),
        )

    @staticmethod
    def _validate_payloads(bootstrap: Any, fixtures: Any) -> None:
        required = {"events", "teams", "elements"}
        if not isinstance(bootstrap, dict) or not required.issubset(bootstrap):
            raise ValueError("bootstrap-static payload is missing required collections")
        for name in required:
            if not isinstance(bootstrap[name], list) or not all(
                isinstance(item, dict) for item in bootstrap[name]
            ):
                raise ValueError(f"bootstrap-static collection {name!r} is malformed")
        if not isinstance(fixtures, list) or not all(
            isinstance(item, dict) for item in fixtures
        ):
            raise ValueError("fixtures payload must be a list")
        _require_keys(bootstrap["events"], {"id"}, "event")
        _require_keys(bootstrap["teams"], {"id", "name", "short_name"}, "team")
        _require_keys(
            bootstrap["elements"],
            {"id", "web_name", "team", "element_type", "now_cost"},
            "player",
        )
        _require_keys(fixtures, {"id", "team_h", "team_a"}, "fixture")

    @staticmethod
    def _snapshot_gameweek(events: list[dict[str, Any]]) -> int:
        for key in ("is_next", "is_current"):
            matching = [int(event["id"]) for event in events if event.get(key)]
            if matching:
                return matching[0]
        if events:
            return max(int(event["id"]) for event in events)
        raise ValueError("No Gameweeks were returned by the FPL API")

    @staticmethod
    def _build_bundle(
        *,
        season_code: str,
        season_name: str,
        gameweek_number: int,
        captured_at: datetime,
        bootstrap: dict[str, Any],
        fixtures: list[dict[str, Any]],
    ) -> HistoricalBundle:
        events = bootstrap["events"]
        deadlines = [event.get("deadline_time") for event in events if event.get("deadline_time")]
        season = SeasonRecord(
            code=season_code,
            name=season_name,
            starts_on=min(deadlines)[:10] if deadlines else None,
            ends_on=max(deadlines)[:10] if deadlines else None,
        )
        teams = tuple(
            TeamRecord(
                source_team_id=str(team["id"]),
                name=str(team["name"]),
                short_name=str(team["short_name"]),
            )
            for team in bootstrap["teams"]
        )
        players = tuple(
            PlayerRecord(
                source_player_id=str(player["id"]),
                first_name=str(player.get("first_name", "")),
                second_name=str(player.get("second_name", "")),
                web_name=str(player["web_name"]),
            )
            for player in bootstrap["elements"]
        )
        player_seasons = tuple(
            PlayerSeasonRecord(
                source_player_id=str(player["id"]),
                source_team_id=str(player["team"]),
                position=POSITION_BY_ELEMENT_TYPE[int(player["element_type"])],
            )
            for player in bootstrap["elements"]
        )
        gameweeks = tuple(
            GameweekRecord(
                number=int(event["id"]),
                deadline_time=event.get("deadline_time"),
                is_finished=bool(event.get("finished", False)),
            )
            for event in events
        )
        fixture_records = tuple(
            FixtureRecord(
                source_fixture_id=str(fixture["id"]),
                home_team_source_id=str(fixture["team_h"]),
                away_team_source_id=str(fixture["team_a"]),
                gameweek_number=fixture.get("event"),
                kickoff_time=fixture.get("kickoff_time"),
                home_score=fixture.get("team_h_score"),
                away_score=fixture.get("team_a_score"),
                finished=bool(fixture.get("finished", False)),
            )
            for fixture in fixtures
        )
        snapshots = tuple(
            PlayerGameweekSnapshotRecord(
                source_player_id=str(player["id"]),
                gameweek_number=gameweek_number,
                price_tenths=int(player["now_cost"]),
                captured_at=captured_at,
                source_team_id=str(player["team"]),
                selected_by_percent=_optional_float(player.get("selected_by_percent")),
                transfers_in=_optional_int(player.get("transfers_in_event")),
                transfers_out=_optional_int(player.get("transfers_out_event")),
                status=player.get("status"),
                chance_of_playing_next_round=_optional_int(
                    player.get("chance_of_playing_next_round")
                ),
                news=player.get("news") or None,
            )
            for player in bootstrap["elements"]
        )
        return HistoricalBundle(
            season=season,
            teams=teams,
            players=players,
            player_seasons=player_seasons,
            gameweeks=gameweeks,
            fixtures=fixture_records,
            gameweek_snapshots=snapshots,
        )

    def _archive(
        self,
        *,
        captured_at: datetime,
        season_code: str,
        bootstrap: ApiPayload,
        fixtures: ApiPayload,
        digest: str,
    ) -> Path:
        stamp = captured_at.astimezone(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        directory = self.archive_root / season_code / stamp
        directory.mkdir(parents=True, exist_ok=False)
        _atomic_write(directory / "bootstrap-static.json", bootstrap.body)
        _atomic_write(directory / "fixtures.json", fixtures.body)
        manifest = {
            "captured_at": captured_at.isoformat(),
            "content_sha256": digest,
            "endpoints": {
                "bootstrap-static.json": bootstrap.url,
                "fixtures.json": fixtures.url,
            },
        }
        _atomic_write(
            directory / "manifest.json",
            json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8"),
        )
        return directory


def _atomic_write(path: Path, content: bytes) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(content)
    temporary.replace(path)


def _optional_int(value: Any) -> int | None:
    return None if value is None or value == "" else int(value)


def _optional_float(value: Any) -> float | None:
    return None if value is None or value == "" else float(value)


def _require_keys(
    records: list[dict[str, Any]], required: set[str], entity: str
) -> None:
    for index, record in enumerate(records):
        missing = sorted(required - record.keys())
        if missing:
            raise ValueError(
                f"{entity} record {index} is missing required fields: {', '.join(missing)}"
            )
