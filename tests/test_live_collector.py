from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from fpl_engine.history.database import HistoricalDatabase
from fpl_engine.live.client import ApiPayload
from fpl_engine.live.collector import LiveSnapshotCollector


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
            "gameweek_snapshots": 1,
        }
        totals = database.player_gameweek_totals("2026-27", "101", 1)
        assert totals is not None
        assert totals["price_tenths"] == 75
        assert totals["selected_by_percent"] == 12.3

    manifest = json.loads((result.archive_directory / "manifest.json").read_text())
    expected_digest = hashlib.sha256(
        client.bootstrap.body + b"\n" + client.fixture_payload.body
    ).hexdigest()
    assert manifest["content_sha256"] == expected_digest
    bootstrap_archive = result.archive_directory / "bootstrap-static.json"
    assert bootstrap_archive.read_bytes() == client.bootstrap.body
    fixtures_archive = result.archive_directory / "fixtures.json"
    assert fixtures_archive.read_bytes() == client.fixture_payload.body
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
