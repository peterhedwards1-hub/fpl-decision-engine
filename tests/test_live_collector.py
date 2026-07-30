from __future__ import annotations

import gzip
import hashlib
import json
from datetime import UTC, datetime

import pytest

from fpl_engine.history.database import HistoricalDatabase
from fpl_engine.live.client import ApiPayload
from fpl_engine.live.collector import LiveSnapshotCollector
from fpl_engine.prospective import build_prospective_capture_status


class FakeClient:
    def __init__(self, bootstrap: dict, fixtures: list[dict]) -> None:
        self.bootstrap = _payload("bootstrap-static/", bootstrap)
        self.fixture_payload = _payload("fixtures/", fixtures)

    def bootstrap_static(self) -> ApiPayload:
        return self.bootstrap

    def fixtures(self) -> ApiPayload:
        return self.fixture_payload


def _payload(path: str, data: object) -> ApiPayload:
    body = json.dumps(data, sort_keys=True).encode()
    return ApiPayload(
        url=f"https://fantasy.premierleague.com/api/{path}",
        body=body,
        data=data,
    )


def _bootstrap(
    *, price: int = 75, team: int = 1, code: int | None = None,
    temporary: bool = False, opta_code: str | None = None
) -> dict:
    return {
        "events": [
            {
                "id": 1,
                "deadline_time": "2026-08-14T17:30:00Z",
                "finished": False,
                "is_current": False,
                "is_next": True,
            },
            {
                "id": 2,
                "deadline_time": "2026-08-21T17:30:00Z",
                "finished": False,
                "is_current": False,
                "is_next": False,
            },
        ],
        "teams": [
            {"id": 1, "name": "North Town", "short_name": "NTH"},
            {"id": 2, "name": "South City", "short_name": "STH"},
        ],
        "elements": [
            {
                "id": 101,
                "first_name": "Ada",
                "second_name": "Striker",
                "web_name": "Ada",
                "team": team,
                "element_type": 4,
                "now_cost": price,
                "selected_by_percent": "12.3",
                "transfers_in_event": 150,
                "transfers_out_event": 20,
                "status": "a",
                "chance_of_playing_next_round": 100,
                "news": "",
                "minutes": 810,
                "starts": 9,
                "goals_scored": 6,
                "assists": 3,
                "clean_sheets": 2,
                "bonus": 8,
                "bps": 211,
                "defensive_contribution": 5,
                "expected_goals": "5.4",
                "expected_assists": "2.1",
                "expected_goal_involvements": "7.5",
                "total_points": 61,
                **({"code": code} if code is not None else {}),
                **({"has_temporary_code": True} if temporary else {}),
                **({"opta_code": opta_code} if opta_code is not None else {}),
            }
        ],
    }


def _fixtures() -> list[dict]:
    return [
        {
            "id": 501,
            "event": 1,
            "team_h": 1,
            "team_a": 2,
            "kickoff_time": "2026-08-15T14:00:00Z",
            "team_h_score": None,
            "team_a_score": None,
            "finished": False,
        }
    ]


def test_collects_archives_and_ingests_snapshot(tmp_path) -> None:
    captured_at = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
    client = FakeClient(_bootstrap(), _fixtures())

    with HistoricalDatabase(tmp_path / "fpl.sqlite3") as database:
        database.initialise()
        result = LiveSnapshotCollector(
            database,
            archive_root=tmp_path / "raw",
            report_root=tmp_path / "reports",
            client=client,
            clock=lambda: captured_at,
        ).collect(season_code="2026-27", season_name="2026/27")

        assert result.gameweek_number == 1
        assert database.season_summary("2026-27") == {
            "teams": 2,
            "players": 1,
            "gameweeks": 2,
            "fixtures": 1,
            "fixture_stats": 0,
            "season_stats_observations": 1,
            "gameweek_snapshots": 1,
        }
        totals = database.player_gameweek_totals("2026-27", "101", 1)
        assert totals is not None
        assert totals["price_tenths"] == 75
        assert totals["selected_by_percent"] == 12.3
        season_stats = database.connection.execute(
            """
            SELECT goals, assists, defensive_contributions, expected_goals,
                   total_points
            FROM player_season_stats_observations
            """
        ).fetchone()
        assert dict(season_stats) == {
            "goals": 6,
            "assists": 3,
            "defensive_contributions": 5,
            "expected_goals": 5.4,
            "total_points": 61,
        }

    manifest = json.loads((result.archive_directory / "manifest.json").read_text())
    expected_digest = hashlib.sha256(
        client.bootstrap.body + b"\n" + client.fixture_payload.body
    ).hexdigest()
    assert manifest["content_sha256"] == expected_digest
    bootstrap_archive = result.archive_directory / "bootstrap-static.json.gz"
    assert gzip.decompress(bootstrap_archive.read_bytes()) == client.bootstrap.body
    fixtures_archive = result.archive_directory / "fixtures.json.gz"
    assert gzip.decompress(fixtures_archive.read_bytes()) == client.fixture_payload.body
    assert manifest["observation_kind"] == "live_pre_deadline"
    assert result.deadline_time == "2026-08-14T17:30:00Z"
    assert result.observation_kind == "live_pre_deadline"
    assert result.report_index.exists()
    assert result.latest_report_index.exists()


def test_repeat_collection_updates_same_gameweek_without_duplicates(tmp_path) -> None:
    times = iter(
        [
            datetime(2026, 7, 27, 12, 0, 0, tzinfo=UTC),
            datetime(2026, 7, 27, 12, 0, 0, 500000, tzinfo=UTC),
        ]
    )
    with HistoricalDatabase(tmp_path / "fpl.sqlite3") as database:
        database.initialise()
        first = LiveSnapshotCollector(
            database,
            archive_root=tmp_path / "raw",
            report_root=tmp_path / "reports",
            client=FakeClient(_bootstrap(price=75), _fixtures()),
            clock=lambda: next(times),
        )
        first.collect(season_code="2026-27")
        second = LiveSnapshotCollector(
            database,
            archive_root=tmp_path / "raw",
            report_root=tmp_path / "reports",
            client=FakeClient(_bootstrap(price=76, team=2), _fixtures()),
            clock=lambda: next(times),
        )
        second_result = second.collect(season_code="2026-27")

        summary = database.season_summary("2026-27")
        assert summary["players"] == 1
        assert summary["fixtures"] == 1
        assert summary["gameweek_snapshots"] == 2
        totals = database.player_gameweek_totals("2026-27", "101", 1)
        assert totals is not None
        assert totals["price_tenths"] == 76
        player_season = database.connection.execute(
            """
            SELECT teams.source_team_id, snapshots.team_id
            FROM player_seasons ps
            JOIN teams ON teams.id = ps.team_id
            JOIN player_gameweek_snapshots snapshots
              ON snapshots.player_season_id = ps.id
            WHERE ps.season_id = (SELECT id FROM seasons WHERE code = '2026-27')
            ORDER BY snapshots.captured_at DESC, snapshots.id DESC
            """
        ).fetchone()
        assert player_season["source_team_id"] == "1"
        current_team = database.connection.execute(
            "SELECT source_team_id FROM teams WHERE id = ?",
            (player_season["team_id"],),
        ).fetchone()
        assert current_team["source_team_id"] == "2"
        assert "South City" in second_result.latest_report_index.read_text(
            encoding="utf-8"
        )


def test_live_report_uses_pre_deadline_observation_when_post_gameweek_exists(tmp_path) -> None:
    captured_at = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
    with HistoricalDatabase(tmp_path / "fpl.sqlite3") as database:
        database.initialise()
        collector = LiveSnapshotCollector(
            database,
            archive_root=tmp_path / "raw",
            report_root=tmp_path / "reports",
            client=FakeClient(_bootstrap(price=75), _fixtures()),
            clock=lambda: captured_at,
        )
        collector.collect(season_code="2026-27")
        player_season_id = database.connection.execute(
            "SELECT id FROM player_seasons WHERE source_player_id = '101'"
        ).fetchone()[0]
        gameweek_id = database.connection.execute(
            "SELECT id FROM gameweeks WHERE number = 1"
        ).fetchone()[0]
        team_id = database.connection.execute(
            "SELECT id FROM teams WHERE source_team_id = '1'"
        ).fetchone()[0]
        database.connection.execute(
            """
            INSERT INTO player_gameweek_observations (
                player_season_id, gameweek_id, observation_kind, observed_at,
                timing_quality, team_id, price_tenths, source_observation_key,
                provenance_run_id
            ) VALUES (?, ?, 'post_gameweek', ?, 'exact', ?, 99, 'post-existing', 1)
            """,
            (
                player_season_id,
                gameweek_id,
                captured_at.isoformat(),
                team_id,
            ),
        )
        database.connection.commit()

        result = LiveSnapshotCollector(
            database,
            archive_root=tmp_path / "raw",
            report_root=tmp_path / "reports",
            client=FakeClient(_bootstrap(price=76), _fixtures()),
            clock=lambda: captured_at.replace(microsecond=1),
        ).collect(season_code="2026-27")

        report = result.latest_report_index.read_text(encoding="utf-8")
        assert "£7.6m" in report
        assert "£9.9m" not in report


def test_live_codes_ignore_temporary_values_but_keep_opta_and_later_permanent_code(
    tmp_path,
) -> None:
    captured_at = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
    with HistoricalDatabase(tmp_path / "fpl.sqlite3") as database:
        database.initialise()
        LiveSnapshotCollector(
            database,
            archive_root=tmp_path / "raw",
            report_root=tmp_path / "reports",
            client=FakeClient(
                _bootstrap(code=123, temporary=True, opta_code="opta-1"), _fixtures()
            ),
            clock=lambda: captured_at,
        ).collect(season_code="2026-27")
        assert database.connection.execute(
            "SELECT COUNT(*) FROM player_identifiers WHERE identifier_type = 'official_fpl_code'"
        ).fetchone()[0] == 0
        assert database.connection.execute(
            "SELECT identifier_value FROM player_identifiers WHERE identifier_type = 'opta_code'"
        ).fetchone()[0] == "opta-1"

        LiveSnapshotCollector(
            database,
            archive_root=tmp_path / "raw",
            report_root=tmp_path / "reports",
            client=FakeClient(_bootstrap(code=456), _fixtures()),
            clock=lambda: captured_at.replace(microsecond=1),
        ).collect(season_code="2026-27")
        assert database.connection.execute(
            "SELECT identifier_value FROM player_identifiers "
            "WHERE identifier_type = 'official_fpl_code'"
        ).fetchone()[0] == "456"


def test_same_live_content_at_different_capture_times_is_distinct(tmp_path) -> None:
    times = iter(
        [
            datetime(2026, 7, 27, 12, 0, tzinfo=UTC),
            datetime(2026, 7, 27, 12, 1, tzinfo=UTC),
        ]
    )
    with HistoricalDatabase(tmp_path / "fpl.sqlite3") as database:
        database.initialise()
        for _ in range(2):
            LiveSnapshotCollector(
                database,
                archive_root=tmp_path / "raw",
                report_root=tmp_path / "reports",
                client=FakeClient(_bootstrap(), _fixtures()),
                clock=lambda: next(times),
            ).collect(season_code="2026-27")
        assert database.connection.execute(
            "SELECT COUNT(*) FROM player_gameweek_observations"
        ).fetchone()[0] == 2


def test_rejects_malformed_bootstrap_before_database_ingestion(tmp_path) -> None:
    with HistoricalDatabase(tmp_path / "fpl.sqlite3") as database:
        database.initialise()
        collector = LiveSnapshotCollector(
            database,
            archive_root=tmp_path / "raw",
            report_root=tmp_path / "reports",
            client=FakeClient({"events": []}, []),
            clock=lambda: datetime(2026, 7, 27, 12, 0, tzinfo=UTC),
        )
        try:
            collector.collect(season_code="2026-27")
        except ValueError as error:
            assert "missing required collections" in str(error)
        else:
            raise AssertionError("Malformed payload should fail")

        query = "SELECT COUNT(*) FROM ingestion_runs"
        count = database.connection.execute(query).fetchone()[0]
        assert count == 0


def test_archives_rebuild_a_fresh_database_idempotently(tmp_path) -> None:
    captured_at = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
    archive_root = tmp_path / "raw"
    with HistoricalDatabase(tmp_path / "source.sqlite3") as database:
        database.initialise()
        LiveSnapshotCollector(
            database,
            archive_root=archive_root,
            report_root=tmp_path / "reports",
            client=FakeClient(_bootstrap(), _fixtures()),
            clock=lambda: captured_at,
        ).collect(season_code="2026-27")

    with HistoricalDatabase(tmp_path / "rebuilt.sqlite3") as rebuilt:
        rebuilt.initialise()
        collector = LiveSnapshotCollector(
            rebuilt,
            archive_root=archive_root,
            report_root=tmp_path / "rebuilt-reports",
            client=FakeClient(_bootstrap(), _fixtures()),
        )
        first_replay = collector.replay_archives(season_code="2026-27")
        second_replay = collector.replay_archives(season_code="2026-27")

        assert first_replay == (1,)
        assert second_replay == (1,)
        assert rebuilt.season_summary("2026-27") == {
            "teams": 2,
            "players": 1,
            "gameweeks": 2,
            "fixtures": 1,
            "fixture_stats": 0,
            "season_stats_observations": 1,
            "gameweek_snapshots": 1,
        }
        assert rebuilt.connection.execute(
            "SELECT COUNT(*) FROM ingestion_runs"
        ).fetchone()[0] == 1


def test_capture_after_final_deadline_is_not_labelled_pre_deadline(tmp_path) -> None:
    bootstrap = _bootstrap()
    for event in bootstrap["events"]:
        event["is_next"] = False
        event["is_current"] = False

    with HistoricalDatabase(tmp_path / "fpl.sqlite3") as database:
        database.initialise()
        result = LiveSnapshotCollector(
            database,
            archive_root=tmp_path / "raw",
            report_root=tmp_path / "reports",
            client=FakeClient(bootstrap, _fixtures()),
            clock=lambda: datetime(2026, 8, 22, 12, 0, tzinfo=UTC),
        ).collect(season_code="2026-27")

        observation = database.connection.execute(
            """
            SELECT observation_kind
            FROM player_gameweek_observations
            """
        ).fetchone()

    assert result.gameweek_number == 2
    assert result.observation_kind == "post_gameweek"
    assert observation["observation_kind"] == "post_gameweek"
    assert "All checks passed" in result.latest_report_index.read_text(
        encoding="utf-8"
    )


def test_required_pre_deadline_capture_fails_before_writing(tmp_path) -> None:
    bootstrap = _bootstrap()
    for event in bootstrap["events"]:
        event["is_next"] = False
        event["is_current"] = False

    with HistoricalDatabase(tmp_path / "fpl.sqlite3") as database:
        database.initialise()
        collector = LiveSnapshotCollector(
            database,
            archive_root=tmp_path / "raw",
            report_root=tmp_path / "reports",
            client=FakeClient(bootstrap, _fixtures()),
            clock=lambda: datetime(2026, 8, 22, 12, 0, tzinfo=UTC),
        )
        with pytest.raises(ValueError, match="not before the next deadline"):
            collector.collect(
                season_code="2026-27",
                require_pre_deadline=True,
            )
        assert database.connection.execute(
            "SELECT COUNT(*) FROM ingestion_runs"
        ).fetchone()[0] == 0
        assert not (tmp_path / "raw").exists()


def test_prospective_status_exposes_unrecoverable_workflow_gaps(tmp_path) -> None:
    captured_at = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
    with HistoricalDatabase(tmp_path / "fpl.sqlite3") as database:
        database.initialise()
        LiveSnapshotCollector(
            database,
            archive_root=tmp_path / "raw",
            report_root=tmp_path / "reports",
            client=FakeClient(_bootstrap(), _fixtures()),
            clock=lambda: captured_at,
        ).collect(season_code="2026-27")

        upcoming = build_prospective_capture_status(
            database,
            "2026-27",
            as_of=captured_at,
        )
        assert upcoming["gameweeks"][0]["status"] == "upcoming"

        after_deadline = build_prospective_capture_status(
            database,
            "2026-27",
            as_of=datetime(2026, 8, 15, 12, 0, tzinfo=UTC),
        )
        gameweek = after_deadline["gameweeks"][0]
        assert gameweek["status"] == "incomplete"
        assert gameweek["counts"]["pre_deadline_snapshot"] == 1
        assert "pre_deadline_snapshot" not in gameweek["missing_required"]
        assert "paired_news_projections" in gameweek["missing_required"]
        assert "actual_action" in gameweek["missing_required"]


def test_next_pre_deadline_capture_supplies_completed_gameweek_outcomes(
    tmp_path,
) -> None:
    first_at = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
    second_at = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    completed_bootstrap = _bootstrap()
    completed_bootstrap["events"][0]["finished"] = True
    completed_bootstrap["events"][0]["is_next"] = False
    completed_bootstrap["events"][1]["is_next"] = True
    completed_bootstrap["elements"][0]["minutes"] = 90
    completed_bootstrap["elements"][0]["total_points"] = 8
    completed_fixtures = _fixtures()
    completed_fixtures[0]["team_h_score"] = 2
    completed_fixtures[0]["team_a_score"] = 0
    completed_fixtures[0]["finished"] = True

    with HistoricalDatabase(tmp_path / "fpl.sqlite3") as database:
        database.initialise()
        LiveSnapshotCollector(
            database,
            archive_root=tmp_path / "raw",
            report_root=tmp_path / "reports",
            client=FakeClient(_bootstrap(), _fixtures()),
            clock=lambda: first_at,
        ).collect(season_code="2026-27")
        second = LiveSnapshotCollector(
            database,
            archive_root=tmp_path / "raw",
            report_root=tmp_path / "reports",
            client=FakeClient(completed_bootstrap, completed_fixtures),
            clock=lambda: second_at,
        ).collect(season_code="2026-27")
        status = build_prospective_capture_status(
            database,
            "2026-27",
            as_of=second_at,
        )

    assert second.gameweek_number == 2
    gameweek_one = status["gameweeks"][0]
    assert gameweek_one["is_finished"] is True
    assert gameweek_one["counts"]["recorded_outcomes"] == 1
    assert "recorded_outcomes" not in gameweek_one["missing_required"]
    assert "post_gameweek_snapshot" not in gameweek_one["missing_required"]
