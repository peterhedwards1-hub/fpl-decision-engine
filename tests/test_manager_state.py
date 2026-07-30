from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from fpl_engine.config import load_season_rules
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
from fpl_engine.manager import (
    ManagerSnapshot,
    ManagerSquadEntry,
    ManagerStateError,
    ManagerStateRepository,
)

RULES = load_season_rules(Path("config/seasons/2026-27.json"))
CAPTURED_AT = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)


def _bundle() -> HistoricalBundle:
    assignments = (
        (1, Position.GK, 1),
        (2, Position.GK, 2),
        (3, Position.DEF, 1),
        (4, Position.DEF, 2),
        (5, Position.DEF, 3),
        (6, Position.DEF, 4),
        (7, Position.DEF, 5),
        (8, Position.MID, 1),
        (9, Position.MID, 2),
        (10, Position.MID, 3),
        (11, Position.MID, 4),
        (12, Position.MID, 5),
        (13, Position.FWD, 3),
        (14, Position.FWD, 4),
        (15, Position.FWD, 5),
    )
    return HistoricalBundle(
        season=SeasonRecord(code="2026-27", name="2026/27"),
        teams=tuple(
            TeamRecord(str(team), f"Team {team}", f"T{team}")
            for team in range(1, 6)
        ),
        players=tuple(
            PlayerRecord(str(player), "", "", f"Player {player}")
            for player, _, _ in assignments
        ),
        player_seasons=tuple(
            PlayerSeasonRecord(str(player), str(team), position)
            for player, position, team in assignments
        ),
        gameweeks=(
            GameweekRecord(
                1,
                deadline_time="2026-08-14T17:30:00Z",
                is_finished=False,
            ),
        ),
        fixtures=(
            FixtureRecord(
                "fixture-1",
                "3",
                "4",
                1,
                kickoff_time="2026-08-15T14:00:00Z",
            ),
        ),
        gameweek_snapshots=tuple(
            PlayerGameweekSnapshotRecord(
                source_player_id=str(player),
                gameweek_number=1,
                price_tenths=50,
                captured_at=CAPTURED_AT,
                source_team_id=str(team),
                observation_kind="live_pre_deadline",
                timing_quality="exact",
                source_observation_key=f"snapshot-{player}",
            )
            for player, _, team in assignments
        ),
    )


def _snapshot(run_id: int) -> ManagerSnapshot:
    starters = {1, 3, 4, 5, 8, 9, 10, 11, 13, 14, 15}
    bench_order = {2: 1, 6: 2, 7: 3, 12: 4}
    return ManagerSnapshot(
        season_code="2026-27",
        gameweek_number=1,
        captured_at=CAPTURED_AT,
        bank_tenths=15,
        free_transfers=2,
        remaining_chips={
            "wildcard": 2,
            "free_hit": 2,
            "bench_boost": 2,
            "triple_captain": 2,
        },
        captain_source_player_id="13",
        vice_captain_source_player_id="8",
        data_ingestion_run_id=run_id,
        entries=tuple(
            ManagerSquadEntry(
                source_player_id=str(player),
                purchase_price_tenths=50,
                selling_price_tenths=50,
                is_starter=player in starters,
                bench_order=bench_order.get(player),
            )
            for player in range(1, 16)
        ),
    )


def test_manager_snapshot_is_validated_saved_and_reloaded(tmp_path) -> None:
    source = IngestionSource(
        name="official-fpl-api",
        retrieved_at=CAPTURED_AT,
        identifier_namespace="official-fpl",
    )
    with HistoricalDatabase(tmp_path / "fpl.sqlite3") as database:
        database.initialise()
        run_id = database.ingest_bundle(source, _bundle())
        repository = ManagerStateRepository(database, RULES)

        snapshot_id = repository.save(_snapshot(run_id))
        stored = repository.latest("2026-27", 1)

        assert snapshot_id == 1
        assert stored is not None
        assert stored.snapshot_id == 1
        assert stored.deadline_time == "2026-08-14T17:30:00Z"
        assert stored.snapshot.bank_tenths == 15
        assert stored.snapshot.free_transfers == 2
        assert stored.snapshot.captain_source_player_id == "13"
        assert len(stored.snapshot.entries) == 15
        bench_ids = [
            entry.source_player_id
            for entry in stored.snapshot.entries
            if not entry.is_starter
        ]
        assert bench_ids == [
            "2",
            "6",
            "7",
            "12",
        ]
        assert len(repository.available_players("2026-27", 1)) == 15


def test_manager_snapshot_rejects_incorrect_selling_price(tmp_path) -> None:
    source = IngestionSource(
        name="official-fpl-api",
        retrieved_at=CAPTURED_AT,
        identifier_namespace="official-fpl",
    )
    with HistoricalDatabase(tmp_path / "fpl.sqlite3") as database:
        database.initialise()
        run_id = database.ingest_bundle(source, _bundle())
        repository = ManagerStateRepository(database, RULES)
        snapshot = _snapshot(run_id)
        entries = (
            replace(snapshot.entries[0], selling_price_tenths=51),
            *snapshot.entries[1:],
        )

        with pytest.raises(ManagerStateError, match="selling price should be 5.0"):
            repository.save(replace(snapshot, entries=entries))

        assert database.connection.execute(
            "SELECT COUNT(*) FROM manager_snapshots"
        ).fetchone()[0] == 0
