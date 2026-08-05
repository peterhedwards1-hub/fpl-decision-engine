"""Transform and persist a current official FPL snapshot."""

from __future__ import annotations

import gzip
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
    PlayerSeasonStatsObservationRecord,
    SeasonRecord,
    TeamRecord,
)

from .client import ApiPayload, FplApiClient
from .mirror import OFFICIAL_PROVENANCE, SnapshotProvenance
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
    deadline_time: str
    observation_kind: str
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
        provenance: SnapshotProvenance = OFFICIAL_PROVENANCE,
    ) -> None:
        self.database = database
        self.archive_root = Path(archive_root)
        self.report_root = Path(report_root)
        self.client = client or FplApiClient()
        self.clock = clock or (lambda: datetime.now(UTC))
        # A capture read from a mirror is recorded under the mirror's name.
        # Nothing downstream may see a mirrored row wearing the official
        # source's provenance.
        self.provenance = provenance

    def collect(
        self,
        *,
        season_code: str,
        season_name: str | None = None,
        require_pre_deadline: bool = False,
    ) -> CollectionResult:
        captured_at = self.clock()
        bootstrap = self.client.bootstrap_static()
        fixtures = self.client.fixtures()
        self._validate_payloads(bootstrap.data, fixtures.data)
        gameweek_number, deadline_time, observation_kind = self._snapshot_target(
            bootstrap.data["events"], captured_at
        )
        if require_pre_deadline and observation_kind != "live_pre_deadline":
            raise ValueError(
                "Capture is not before the next deadline; refusing to record "
                "it as prospective pre-deadline evidence"
            )

        digest = hashlib.sha256(bootstrap.body + b"\n" + fixtures.body).hexdigest()
        archive_directory = self._archive(
            captured_at=captured_at,
            season_code=season_code,
            bootstrap=bootstrap,
            fixtures=fixtures,
            digest=digest,
            gameweek_number=gameweek_number,
            deadline_time=deadline_time,
            observation_kind=observation_kind,
        )
        bundle = self._build_bundle(
            season_code=season_code,
            season_name=season_name or season_code,
            gameweek_number=gameweek_number,
            captured_at=captured_at,
            content_sha256=digest,
            observation_kind=observation_kind,
            bootstrap=bootstrap.data,
            fixtures=fixtures.data,
            source_name=self.provenance.source_name,
        )
        source = IngestionSource(
            name=self.provenance.source_name,
            retrieved_at=captured_at,
            url=bootstrap.url,
            content_sha256=digest,
            identifier_namespace=self.provenance.identifier_namespace,
            adapter_version=self.provenance.adapter_version,
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
            observation_mode=(
                "latest_pre_deadline"
                if observation_kind == "live_pre_deadline"
                else "latest_post_gameweek"
            ),
        )
        return CollectionResult(
            ingestion_run_id=run_id,
            archive_directory=archive_directory,
            report_directory=report.directory,
            report_index=report.index_path,
            latest_report_index=report.latest_index_path,
            season_code=season_code,
            gameweek_number=gameweek_number,
            deadline_time=deadline_time,
            observation_kind=observation_kind,
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
        _require_keys(bootstrap["events"], {"id", "deadline_time"}, "event")
        _require_keys(bootstrap["teams"], {"id", "name", "short_name"}, "team")
        _require_keys(
            bootstrap["elements"],
            {"id", "web_name", "team", "element_type", "now_cost"},
            "player",
        )
        _require_keys(fixtures, {"id", "team_h", "team_a"}, "fixture")

    @staticmethod
    def _snapshot_target(
        events: list[dict[str, Any]], captured_at: datetime
    ) -> tuple[int, str, str]:
        if captured_at.tzinfo is None:
            raise ValueError("Snapshot capture time must be timezone-aware")
        if events:
            parsed = sorted(
                (
                    datetime.fromisoformat(str(event["deadline_time"]).replace("Z", "+00:00")),
                    int(event["id"]),
                    str(event["deadline_time"]),
                )
                for event in events
            )
            upcoming = [
                item
                for item in parsed
                if item[0].astimezone(UTC) > captured_at.astimezone(UTC)
            ]
            if upcoming:
                _, number, deadline = upcoming[0]
                return number, deadline, "live_pre_deadline"
            _, number, deadline = parsed[-1]
            return number, deadline, "post_gameweek"
        raise ValueError("No Gameweeks were returned by the FPL API")

    @staticmethod
    def _build_bundle(
        *,
        season_code: str,
        season_name: str,
        gameweek_number: int,
        captured_at: datetime,
        content_sha256: str,
        observation_kind: str,
        bootstrap: dict[str, Any],
        fixtures: list[dict[str, Any]],
        source_name: str = "official-fpl-api",
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
                official_fpl_code=(
                    None
                    if player.get("has_temporary_code") is True
                    else _optional_identifier(player.get("code"))
                ),
                opta_code=_optional_identifier(player.get("opta_code")),
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
                observation_kind=observation_kind,
                timing_quality="exact",
                selected_by_percent=_optional_float(player.get("selected_by_percent")),
                transfers_in=_optional_int(player.get("transfers_in_event")),
                transfers_out=_optional_int(player.get("transfers_out_event")),
                status=player.get("status"),
                chance_of_playing_next_round=_optional_int(
                    player.get("chance_of_playing_next_round")
                ),
                news=player.get("news") or None,
                source_observation_key=_live_capture_key(
                    source_name,
                    season_code,
                    gameweek_number,
                    captured_at,
                    content_sha256,
                ),
            )
            for player in bootstrap["elements"]
        )
        season_stats = tuple(
            PlayerSeasonStatsObservationRecord(
                source_player_id=str(player["id"]),
                observed_at=captured_at,
                source_observation_key=_live_capture_key(
                    f"{source_name}-season-stats",
                    season_code,
                    gameweek_number,
                    captured_at,
                    content_sha256,
                ),
                minutes=_int_or_zero(player.get("minutes")),
                starts=_int_or_zero(player.get("starts")),
                goals=_int_or_zero(player.get("goals_scored")),
                assists=_int_or_zero(player.get("assists")),
                clean_sheets=_int_or_zero(player.get("clean_sheets")),
                goals_conceded=_int_or_zero(player.get("goals_conceded")),
                own_goals=_int_or_zero(player.get("own_goals")),
                penalties_saved=_int_or_zero(player.get("penalties_saved")),
                penalties_missed=_int_or_zero(player.get("penalties_missed")),
                yellow_cards=_int_or_zero(player.get("yellow_cards")),
                red_cards=_int_or_zero(player.get("red_cards")),
                saves=_int_or_zero(player.get("saves")),
                bonus=_int_or_zero(player.get("bonus")),
                bps=_int_or_zero(player.get("bps")),
                defensive_contributions=_int_or_zero(
                    player.get(
                        "defensive_contribution",
                        player.get("defensive_contributions"),
                    )
                ),
                expected_goals=_optional_float(player.get("expected_goals")),
                expected_assists=_optional_float(player.get("expected_assists")),
                expected_goal_involvements=_optional_float(
                    player.get("expected_goal_involvements")
                ),
                expected_goals_conceded=_optional_float(
                    player.get("expected_goals_conceded")
                ),
                total_points=_int_or_zero(player.get("total_points")),
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
            season_stats_observations=season_stats,
        )

    def _archive(
        self,
        *,
        captured_at: datetime,
        season_code: str,
        bootstrap: ApiPayload,
        fixtures: ApiPayload,
        digest: str,
        gameweek_number: int,
        deadline_time: str,
        observation_kind: str,
    ) -> Path:
        stamp = captured_at.astimezone(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        directory = self.archive_root / season_code / f"{stamp}-{digest[:12]}"
        directory.mkdir(parents=True, exist_ok=True)
        bootstrap_name = "bootstrap-static.json.gz"
        fixtures_name = "fixtures.json.gz"
        _write_immutable(
            directory / bootstrap_name,
            gzip.compress(bootstrap.body, mtime=0),
        )
        _write_immutable(
            directory / fixtures_name,
            gzip.compress(fixtures.body, mtime=0),
        )
        manifest = {
            "captured_at": captured_at.isoformat(),
            "content_sha256": digest,
            "compression": "gzip",
            "season_code": season_code,
            "gameweek_number": gameweek_number,
            "deadline_time": deadline_time,
            "observation_kind": observation_kind,
            "endpoints": {
                bootstrap_name: bootstrap.url,
                fixtures_name: fixtures.url,
            },
        }
        _write_immutable(
            directory / "manifest.json",
            json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8"),
        )
        return directory

    def replay_archives(
        self,
        *,
        season_code: str,
        season_name: str | None = None,
    ) -> tuple[int, ...]:
        """Rebuild normalised state from every immutable archive for a season."""

        season_root = self.archive_root / season_code
        if not season_root.exists():
            return ()
        run_ids = []
        for manifest_path in sorted(season_root.glob("*/manifest.json")):
            run_ids.append(
                self._replay_archive(
                    manifest_path.parent,
                    season_code=season_code,
                    season_name=season_name or season_code,
                )
            )
        return tuple(run_ids)

    def _replay_archive(
        self,
        directory: Path,
        *,
        season_code: str,
        season_name: str,
    ) -> int:
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        if manifest.get("season_code", season_code) != season_code:
            raise ValueError(f"Archive {directory} belongs to another season")
        captured_at = datetime.fromisoformat(str(manifest["captured_at"]))
        if captured_at.tzinfo is None:
            raise ValueError(f"Archive {directory} has a naive capture timestamp")
        endpoints = manifest.get("endpoints")
        if not isinstance(endpoints, dict):
            raise ValueError(f"Archive {directory} has no endpoint manifest")
        bootstrap_name = _endpoint_filename(endpoints, "bootstrap-static")
        fixtures_name = _endpoint_filename(endpoints, "fixtures")
        bootstrap_body = _read_archived_bytes(directory / bootstrap_name)
        fixtures_body = _read_archived_bytes(directory / fixtures_name)
        digest = hashlib.sha256(
            bootstrap_body + b"\n" + fixtures_body
        ).hexdigest()
        if digest != manifest["content_sha256"]:
            raise ValueError(f"Archive checksum mismatch in {directory}")
        existing = self.database.connection.execute(
            """
            SELECT id FROM ingestion_runs
            WHERE source_name = ?
              AND retrieved_at = ?
              AND content_sha256 = ?
              AND status = 'completed'
            """,
            (
                self.provenance.source_name,
                captured_at.astimezone(UTC).isoformat(),
                digest,
            ),
        ).fetchone()
        if existing is not None:
            return int(existing["id"])

        bootstrap_data = json.loads(bootstrap_body)
        fixtures_data = json.loads(fixtures_body)
        self._validate_payloads(bootstrap_data, fixtures_data)
        fallback = self._snapshot_target(bootstrap_data["events"], captured_at)
        gameweek_number = int(manifest.get("gameweek_number", fallback[0]))
        observation_kind = str(manifest.get("observation_kind", fallback[2]))
        bundle = self._build_bundle(
            season_code=season_code,
            season_name=season_name,
            gameweek_number=gameweek_number,
            captured_at=captured_at,
            content_sha256=digest,
            observation_kind=observation_kind,
            bootstrap=bootstrap_data,
            fixtures=fixtures_data,
            source_name=self.provenance.source_name,
        )
        source = IngestionSource(
            name=self.provenance.source_name,
            retrieved_at=captured_at,
            url=str(endpoints[bootstrap_name]),
            content_sha256=digest,
            identifier_namespace=self.provenance.identifier_namespace,
            adapter_version=self.provenance.adapter_version,
        )
        return self.database.ingest_bundle(source, bundle)


def _atomic_write(path: Path, content: bytes) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(content)
    temporary.replace(path)


def _write_immutable(path: Path, content: bytes) -> None:
    if path.exists():
        if path.read_bytes() != content:
            raise ValueError(f"Immutable archive path already has different content: {path}")
        return
    _atomic_write(path, content)


def _read_archived_bytes(path: Path) -> bytes:
    content = path.read_bytes()
    return gzip.decompress(content) if path.suffix == ".gz" else content


def _endpoint_filename(endpoints: dict[str, Any], stem: str) -> str:
    matches = [name for name in endpoints if stem in name]
    if len(matches) != 1:
        raise ValueError(f"Archive manifest must contain one {stem} endpoint")
    return matches[0]


def _optional_int(value: Any) -> int | None:
    return None if value is None or value == "" else int(value)


def _int_or_zero(value: Any) -> int:
    return 0 if value is None or value == "" else int(value)


def _optional_float(value: Any) -> float | None:
    return None if value is None or value == "" else float(value)


def _optional_identifier(value: Any) -> str | None:
    return None if value is None or value == "" else str(value)


def _live_capture_key(
    source_name: str,
    season_code: str,
    gameweek_number: int,
    captured_at: datetime,
    content_sha256: str,
) -> str:
    """Identify an archived capture, not merely its repeated content."""

    canonical = "|".join(
        (
            source_name,
            season_code,
            str(gameweek_number),
            captured_at.astimezone(UTC).isoformat(),
            content_sha256,
        )
    )
    return "live-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _require_keys(
    records: list[dict[str, Any]], required: set[str], entity: str
) -> None:
    for index, record in enumerate(records):
        missing = sorted(required - record.keys())
        if missing:
            raise ValueError(
                f"{entity} record {index} is missing required fields: {', '.join(missing)}"
            )
