from __future__ import annotations

from pathlib import Path

import pytest

from fpl_engine.history.csv_bundle import CsvBundleError, load_csv_bundle
from fpl_engine.history.records import SeasonRecord

SEASON = SeasonRecord("2025-26", "2025/26")


def _write_minimum_bundle(root: Path) -> None:
    (root / "teams.csv").write_text(
        "source_team_id,name,short_name\n1,North Town,NTH\n2,South City,STH\n",
        encoding="utf-8",
    )
    (root / "players.csv").write_text(
        "source_player_id,web_name,first_name,second_name\n101,Ada,Ada,Striker\n",
        encoding="utf-8",
    )
    (root / "player_seasons.csv").write_text(
        "source_player_id,source_team_id,position\n101,1,FWD\n",
        encoding="utf-8",
    )
    (root / "gameweeks.csv").write_text(
        "number,deadline_time,is_finished\n1,2025-08-15T18:30:00+00:00,true\n",
        encoding="utf-8",
    )
    (root / "fixtures.csv").write_text(
        "source_fixture_id,home_team_source_id,away_team_source_id,gameweek_number,finished\n"
        "5001,1,2,1,false\n",
        encoding="utf-8",
    )


def test_csv_loader_reports_missing_headers(tmp_path) -> None:
    _write_minimum_bundle(tmp_path)
    (tmp_path / "teams.csv").write_text(
        "source_team_id,name\n1,North Town\n", encoding="utf-8"
    )

    with pytest.raises(CsvBundleError, match="teams.csv: missing required header.*short_name"):
        load_csv_bundle(tmp_path, SEASON)


def test_csv_loader_rejects_invalid_numeric_values_instead_of_zeroing(tmp_path) -> None:
    _write_minimum_bundle(tmp_path)
    (tmp_path / "player_fixture_stats.csv").write_text(
        "source_player_id,source_fixture_id,minutes,total_points\n101,5001,not-an-int,3\n",
        encoding="utf-8",
    )

    with pytest.raises(CsvBundleError, match="player_fixture_stats.csv.*minutes"):
        load_csv_bundle(tmp_path, SEASON)


def test_csv_loader_rejects_invalid_booleans_and_duplicate_ids(tmp_path) -> None:
    _write_minimum_bundle(tmp_path)
    (tmp_path / "gameweeks.csv").write_text(
        "number\n1\n1\n", encoding="utf-8"
    )
    with pytest.raises(CsvBundleError, match="duplicate number"):
        load_csv_bundle(tmp_path, SEASON)

    _write_minimum_bundle(tmp_path)
    (tmp_path / "gameweeks.csv").write_text(
        "number,is_finished\n1,perhaps\n", encoding="utf-8"
    )
    with pytest.raises(CsvBundleError, match="invalid boolean"):
        load_csv_bundle(tmp_path, SEASON)


def test_csv_loader_validates_references_and_historical_observation_timing(tmp_path) -> None:
    _write_minimum_bundle(tmp_path)
    (tmp_path / "player_seasons.csv").write_text(
        "source_player_id,source_team_id,position\n101,missing,FWD\n",
        encoding="utf-8",
    )
    with pytest.raises(CsvBundleError, match="missing from teams.csv"):
        load_csv_bundle(tmp_path, SEASON)

    _write_minimum_bundle(tmp_path)
    (tmp_path / "player_gameweek_snapshots.csv").write_text(
        "source_player_id,gameweek_number,price_tenths,selected_count,"
        "observation_kind,timing_quality\n"
        "101,1,75,100,historical_reconstruction,unknown\n",
        encoding="utf-8",
    )
    bundle = load_csv_bundle(tmp_path, SEASON)
    observation = bundle.gameweek_snapshots[0]
    assert observation.captured_at is None
    assert observation.selected_count == 100
    assert observation.selected_by_percent is None


def test_csv_loader_reports_invalid_positions_and_timestamps(tmp_path) -> None:
    _write_minimum_bundle(tmp_path)
    (tmp_path / "player_seasons.csv").write_text(
        "source_player_id,source_team_id,position\n101,1,ALIEN\n",
        encoding="utf-8",
    )
    with pytest.raises(CsvBundleError, match="invalid value 'ALIEN'"):
        load_csv_bundle(tmp_path, SEASON)

    _write_minimum_bundle(tmp_path)
    (tmp_path / "player_gameweek_snapshots.csv").write_text(
        "source_player_id,gameweek_number,price_tenths,captured_at\n"
        "101,1,75,not-a-timestamp\n",
        encoding="utf-8",
    )
    with pytest.raises(CsvBundleError, match="invalid timestamp"):
        load_csv_bundle(tmp_path, SEASON)
