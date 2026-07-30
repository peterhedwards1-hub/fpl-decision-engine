from datetime import UTC, datetime

import pytest

from fpl_engine.history import (
    HistoricalDatabase,
    IngestionSource,
    VaastavAdapter,
    VaastavImportError,
)


class FakeVaastavSource:
    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files

    def fetch(self, source_ref: str, season_code: str, path: str) -> bytes:
        assert source_ref == "abc123"
        assert season_code == "2025-26"
        try:
            return self.files[path]
        except KeyError as error:
            raise AssertionError(f"Unexpected source file {path}") from error


def _source_files() -> dict[str, bytes]:
    return {
        "teams.csv": (
            b"id,name,short_name\n"
            b"1,North Town,NTH\n"
            b"2,South City,STH\n"
        ),
        "players_raw.csv": (
            b"id,code,opta_code,has_temporary_code,first_name,second_name,"
            b"web_name,birth_date,team,element_type,now_cost\n"
            b"101,1001,p1001,False,Ada,Striker,Ada,1998-01-02,1,4,76\n"
            b"202,2002,p2002,False,Grace,Keeper,Grace,1995-03-04,2,1,45\n"
            b"303,3003,,True,Alex,Manager,Alex,,1,5,5\n"
        ),
        "fixtures.csv": (
            b"id,event,kickoff_time,team_h,team_a,team_h_score,team_a_score,"
            b"finished\n"
            b"501,1,2025-08-16T14:00:00Z,1,2,2,0,True\n"
        ),
        "gws/gw1.csv": (
            b"element,fixture,round,position,value,selected,transfers_in,"
            b"transfers_out,was_home,minutes,starts,goals_scored,assists,"
            b"clean_sheets,goals_conceded,own_goals,penalties_saved,"
            b"penalties_missed,yellow_cards,red_cards,saves,bonus,bps,"
            b"defensive_contribution,expected_goals,expected_assists,"
            b"expected_goal_involvements,expected_goals_conceded,total_points\n"
            b"101,501,1,FWD,75,120000,1000,20,True,90,1,2,0,1,0,0,0,0,"
            b"0,0,0,3,40,5,1.20,0.10,1.30,0.20,13\n"
            b"202,501,1,GK,45,80000,50,10,False,90,1,0,0,0,2,0,0,0,"
            b"0,0,4,1,25,0,0.00,0.00,0.00,1.80,3\n"
            b"303,501,1,AM,5,1000,0,0,True,0,0,0,0,0,0,0,0,0,"
            b"0,0,0,0,0,0,0.00,0.00,0.00,0.00,0\n"
        ),
    }


def test_vaastav_adapter_builds_lossless_historical_bundle(tmp_path) -> None:
    result = VaastavAdapter(FakeVaastavSource(_source_files())).load_season(
        source_ref="abc123",
        season_code="2025-26",
    )

    assert result.source_files == (
        "fixtures.csv",
        "gws/gw1.csv",
        "players_raw.csv",
        "teams.csv",
    )
    assert len(result.content_sha256) == 64
    assert result.quality.fixture_stats == 2
    assert result.quality.skipped_rescheduled_rows == 0
    assert result.quality.skipped_non_player_rows == 1
    assert result.quality.players_without_gameweek_rows == 0
    assert result.bundle.season.name == "2025/26"
    assert result.bundle.fixture_stats[0].defensive_contributions == 5
    assert result.bundle.fixture_stats[0].expected_goals == 1.2
    assert result.bundle.gameweek_snapshots[0].timing_quality == "unknown"
    assert result.bundle.gameweek_snapshots[0].captured_at is None
    assert result.bundle.gameweek_snapshots[0].selected_count == 120000

    with HistoricalDatabase(tmp_path / "history.sqlite3") as database:
        database.initialise()
        run_id = database.ingest_bundle(
            IngestionSource(
                name="vaastav-fpl-dataset",
                retrieved_at=datetime(2026, 7, 29, tzinfo=UTC),
                identifier_namespace="official-fpl",
                content_sha256=result.content_sha256,
                source_revision="abc123",
                adapter_version="vaastav-v1",
            ),
            result.bundle,
        )

        assert run_id == 1
        assert database.season_summary("2025-26") == {
            "teams": 2,
            "players": 2,
            "gameweeks": 1,
            "fixtures": 1,
            "fixture_stats": 2,
            "season_stats_observations": 0,
            "gameweek_snapshots": 2,
        }
        totals = database.player_gameweek_totals("2025-26", "101", 1)
        assert totals is not None
        assert totals["minutes"] == 90
        assert totals["total_points"] == 13
        assert totals["selected_count"] == 120000


def test_vaastav_adapter_requires_immutable_source_revision() -> None:
    adapter = VaastavAdapter(FakeVaastavSource(_source_files()))

    with pytest.raises(VaastavImportError, match="immutable commit SHA"):
        adapter.load_season(source_ref="master", season_code="2025-26")


def test_vaastav_adapter_rejects_unknown_player_references() -> None:
    files = _source_files()
    files["gws/gw1.csv"] = files["gws/gw1.csv"].replace(b"101,501", b"999,501", 1)

    with pytest.raises(VaastavImportError, match="unknown player 999"):
        VaastavAdapter(FakeVaastavSource(files)).load_season(
            source_ref="abc123",
            season_code="2025-26",
        )
