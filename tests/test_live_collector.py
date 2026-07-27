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


def _bootstrap(*, price: int = 75) -> dict:
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
                "team": 1,
                "element_type": 4,
                "now_cost": price,
                "selected_by_percent": "12.3",
                "transfers_in_event": 150,
                "transfers_out_event": 20,
                "status": "a",
                "chance_of_playing_next_round": 100,
                "news": "",
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
    assert (result.archive_directory / "bootstrap-static.json").read_bytes() == client.bootstrap.body
    assert (result.archive_directory / "fixtures.json").read_bytes() == client.fixture_payload.body


def test_repeat_collection_updates_same_gameweek_without_duplicates(tmp_path) -> None:
    times = iter(
        [
            datetime(2026, 7, 27, 12, 0, tzinfo=UTC),
            datetime(2026, 7, 27, 13, 0, tzinfo=UTC),
        ]
    )
    with HistoricalDatabase(tmp_path / "fpl.sqlite3") as database:
        database.initialise()
        first = LiveSnapshotCollector(
            database,
            archive_root=tmp_path / "raw",
            client=FakeClient(_bootstrap(price=75), _fixtures()),
            clock=lambda: next(times),
        )
        first.collect(season_code="2026-27")
        second = LiveSnapshotCollector(
            database,
            archive_root=tmp_path / "raw",
            client=FakeClient(_bootstrap(price=76), _fixtures()),
            clock=lambda: next(times),
        )
        second.collect(season_code="2026-27")

        summary = database.season_summary("2026-27")
        assert summary["players"] == 1
        assert summary["fixtures"] == 1
        assert summary["gameweek_snapshots"] == 1
        totals = database.player_gameweek_totals("2026-27", "101", 1)
        assert totals is not None
        assert totals["price_tenths"] == 76


def test_rejects_malformed_bootstrap_before_database_ingestion(tmp_path) -> None:
    with HistoricalDatabase(tmp_path / "fpl.sqlite3") as database:
        database.initialise()
        collector = LiveSnapshotCollector(
            database,
            archive_root=tmp_path / "raw",
            client=FakeClient({"events": []}, []),
            clock=lambda: datetime(2026, 7, 27, 12, 0, tzinfo=UTC),
        )
        try:
            collector.collect(season_code="2026-27")
        except ValueError as error:
            assert "missing required collections" in str(error)
        else:
            raise AssertionError("Malformed payload should fail")

        count = database.connection.execute("SELECT COUNT(*) FROM ingestion_runs").fetchone()[0]
        assert count == 0
