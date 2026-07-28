from __future__ import annotations

import csv
from datetime import UTC, datetime

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
from fpl_engine.live.report import write_verification_report


def test_verification_report_contains_database_values_and_excel_exports(tmp_path) -> None:
    captured_at = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
    bundle = HistoricalBundle(
        season=SeasonRecord(code="2026-27", name="2026/27"),
        teams=(
            TeamRecord(source_team_id="1", name="North Town", short_name="NTH"),
            TeamRecord(source_team_id="2", name="South City", short_name="STH"),
        ),
        players=(PlayerRecord(source_player_id="101", web_name="Ada"),),
        player_seasons=(
            PlayerSeasonRecord(
                source_player_id="101",
                source_team_id="1",
                position=Position.FWD,
            ),
        ),
        gameweeks=(
            GameweekRecord(
                number=1,
                deadline_time="2026-08-14T17:30:00Z",
            ),
        ),
        fixtures=(
            FixtureRecord(
                source_fixture_id="501",
                home_team_source_id="1",
                away_team_source_id="2",
                gameweek_number=1,
            ),
        ),
        gameweek_snapshots=(
            PlayerGameweekSnapshotRecord(
                source_player_id="101",
                gameweek_number=1,
                price_tenths=75,
                selected_by_percent=12.3,
                status="a",
                captured_at=captured_at,
            ),
        ),
    )
    source = IngestionSource(
        name="official-fpl-api",
        retrieved_at=captured_at,
        url="https://fantasy.premierleague.com/api/bootstrap-static/",
        content_sha256="abc123",
    )

    with HistoricalDatabase(tmp_path / "fpl.sqlite3") as database:
        database.initialise()
        run_id = database.ingest_bundle(source, bundle)
        report = write_verification_report(
            database,
            report_root=tmp_path / "reports",
            season_code="2026-27",
            gameweek_number=1,
            captured_at=captured_at,
            ingestion_run_id=run_id,
            archive_directory=tmp_path / "raw",
        )

    report_html = report.index_path.read_text(encoding="utf-8")
    assert "All checks passed" in report_html
    assert "Ada" in report_html
    assert "£7.5m" in report_html
    assert "North Town" in report_html

    with report.players_csv_path.open(encoding="utf-8-sig", newline="") as csv_file:
        player_rows = list(csv.DictReader(csv_file))
    assert player_rows[0]["web_name"] == "Ada"
    assert player_rows[0]["price_millions"] == "7.5"
    assert report.fixtures_csv_path.exists()
    assert report.latest_index_path.exists()
