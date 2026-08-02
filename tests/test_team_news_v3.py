from __future__ import annotations

import json

import pytest
from test_manager_state import CAPTURED_AT, RULES, _bundle, _snapshot

from fpl_engine.history.database import HistoricalDatabase
from fpl_engine.history.records import IngestionSource
from fpl_engine.news import (
    ingest_structured_news,
    parse_structured_news_v3,
    structured_news_v3_schema,
)
from fpl_engine.projections import RatesProjectionModel
from fpl_engine.team_news_v3 import (
    compute_package_hash,
    generate_team_news_research_package,
)
from fpl_engine.workflow import WeeklyWorkflowRepository, WorkflowError


def _seed(tmp_path):
    database = HistoricalDatabase(tmp_path / "fpl.sqlite3")
    database.initialise()
    ingestion = database.ingest_bundle(
        IngestionSource(
            name="official-fpl-api",
            retrieved_at=CAPTURED_AT,
            identifier_namespace="official-fpl",
        ),
        _bundle(),
    )
    from fpl_engine.manager import ManagerStateRepository

    ManagerStateRepository(database, RULES).save(_snapshot(ingestion))
    projection = RatesProjectionModel(database, RULES).project(
        season_code="2026-27", start_gameweek=1, horizon_gameweeks=1, generated_at=CAPTURED_AT
    )
    return database, projection.projection_run_id


def test_package_contains_all_squad_roles_and_is_deterministic(tmp_path) -> None:
    database, projection_id = _seed(tmp_path)
    try:
        kwargs = dict(
            season_code="2026-27",
            gameweek_number=1,
            projection_run_id=projection_id,
            research_mode="preseason",
            research_window_start="2026-07-01T00:00:00+00:00",
            research_timestamp=CAPTURED_AT,
            alternatives_limit=10,
        )
        first = generate_team_news_research_package(database, **kwargs)
        second = generate_team_news_research_package(database, **kwargs)
        assert first == second
        assert len(first["selected_squad"]) == 15
        assert {item["source_player_id"] for item in first["selected_squad"]} == {
            str(index) for index in range(1, 16)
        }
        by_id = {item["source_player_id"]: item for item in first["selected_squad"]}
        assert by_id["13"]["is_captain"] is True
        assert by_id["8"]["is_vice_captain"] is True
        assert by_id["13"]["is_starting_xi"] is True
        assert by_id["2"]["bench_position"] == 1
        assert by_id["2"]["is_starting_xi"] is False
        assert first["input_package_hash"] == compute_package_hash(first)
        assert first["target_deadline"] is not None
    finally:
        database.close()


def _complete_v3_result(package: dict, *, evidence: list[dict] | None = None) -> dict:
    result = structured_news_v3_schema()
    result["input_package_id"] = package["input_package_id"]
    result["input_package_hash"] = package["input_package_hash"]
    result["season_code"] = package["season_code"]
    result["gameweek"] = package["gameweek"]
    result["target_deadline"] = package["target_deadline"]
    result["research_mode"] = package["research_mode"]
    result["research_window_start"] = package["research_window_start"]
    result["generated_at"] = package["research_timestamp"]
    result["research_run_id"] = "research-v3-test"
    result["coverage"] = [
        {
            "source_player_id": item["source_player_id"],
            "priority": item["priority"],
            "status": "checked_no_material_evidence",
            "areas_checked": ["injury", "training"],
            "latest_source_checked_at": package["research_timestamp"],
            "notes": None,
        }
        for item in package["selected_squad"] + package["alternatives"]
    ]
    result["evidence"] = [] if evidence is None else evidence
    return result


def test_complete_coverage_allows_empty_evidence_but_missing_coverage_does_not(tmp_path) -> None:
    database, projection_id = _seed(tmp_path)
    try:
        package = generate_team_news_research_package(
            database,
            season_code="2026-27",
            gameweek_number=1,
            projection_run_id=projection_id,
            research_mode="preseason",
            research_window_start="2026-07-01T00:00:00+00:00",
            research_timestamp=CAPTURED_AT,
        )
        repository = WeeklyWorkflowRepository(database)
        payload = _complete_v3_result(package)
        assert parse_structured_news_v3(json.dumps(payload)).evidence == ()
        assert (
            ingest_structured_news(
                repository, season_code="2026-27", gameweek_number=1, payload=json.dumps(payload)
            )
            == ()
        )
        payload = _complete_v3_result(package)
        payload["research_run_id"] = "research-v3-missing"
        payload["coverage"] = payload["coverage"][1:]
        with pytest.raises(ValueError, match="coverage is missing"):
            ingest_structured_news(
                repository, season_code="2026-27", gameweek_number=1, payload=json.dumps(payload)
            )
    finally:
        database.close()


def test_v3_rejects_unknown_package_wrong_gameweek_and_after_deadline(tmp_path) -> None:
    database, projection_id = _seed(tmp_path)
    try:
        package = generate_team_news_research_package(
            database,
            season_code="2026-27",
            gameweek_number=1,
            projection_run_id=projection_id,
            research_mode="final",
            research_window_start="2026-08-01T00:00:00+00:00",
            research_timestamp=CAPTURED_AT,
        )
        repository = WeeklyWorkflowRepository(database)
        wrong = _complete_v3_result(package)
        wrong["input_package_hash"] = "bad"
        with pytest.raises(ValueError, match="hash"):
            ingest_structured_news(
                repository, season_code="2026-27", gameweek_number=1, payload=json.dumps(wrong)
            )
        wrong = _complete_v3_result(package)
        wrong["gameweek"] = 2
        with pytest.raises(ValueError, match="Gameweek"):
            ingest_structured_news(
                repository, season_code="2026-27", gameweek_number=1, payload=json.dumps(wrong)
            )
        wrong = _complete_v3_result(package)
        wrong["generated_at"] = "2026-08-15T00:00:00+00:00"
        with pytest.raises(ValueError, match="after the target deadline"):
            ingest_structured_news(
                repository, season_code="2026-27", gameweek_number=1, payload=json.dumps(wrong)
            )
    finally:
        database.close()


def test_v3_conflicts_and_unsupported_adjustments_remain_reviewable(tmp_path) -> None:
    database, projection_id = _seed(tmp_path)
    try:
        package = generate_team_news_research_package(
            database,
            season_code="2026-27",
            gameweek_number=1,
            projection_run_id=projection_id,
            research_mode="provisional",
            research_window_start="2026-08-01T00:00:00+00:00",
            research_timestamp=CAPTURED_AT,
        )
        result = _complete_v3_result(package)
        item = structured_news_v3_schema()["evidence"][0]
        item["source_player_id"] = "13"
        item["priority"] = "critical"
        item["selected_player_status"] = "selected"
        item["source_url"] = "https://example.invalid/conflict"
        item["published_at"] = CAPTURED_AT.isoformat()
        item["conflicting_evidence"] = [
            {
                "source_name": "Reliable reporter",
                "source_url": "https://example.invalid/conflict-2",
                "published_at": CAPTURED_AT.isoformat(),
                "source_tier": "strong_reporting",
                "fact_summary": "A conflicting report.",
            }
        ]
        item["unresolved_uncertainty"] = "Starting status remains unclear."
        item["confidence_after_conflict"] = "medium"
        item["model_area"] = "tactical_role"
        item["suggested_adjustment"] = {"kind": "tactical_role", "value": "wide"}
        item["adjustment_basis"] = "The report describes a changed role."
        item["adjustment_support"] = "unsupported"
        result["evidence"] = [item]
        ids = ingest_structured_news(
            WeeklyWorkflowRepository(database),
            season_code="2026-27",
            gameweek_number=1,
            payload=json.dumps(result),
        )
        assert len(ids) == 1
        row = database.connection.execute(
            "SELECT conflicting_evidence_json, adjustment_support, "
            "expected_minutes_adjustment FROM news_evidence WHERE id = ?",
            (ids[0],),
        ).fetchone()
        assert json.loads(row["conflicting_evidence_json"])[0]["source_name"] == "Reliable reporter"
        assert row["adjustment_support"] == "unsupported"
        assert row["expected_minutes_adjustment"] is None
        with pytest.raises(WorkflowError, match="not directly supported"):
            WeeklyWorkflowRepository(database).review_evidence(
                ids[0],
                status="accepted",
                rationale="Role is recorded but not model-supported.",
                expected_minutes_adjustment=5,
            )
    finally:
        database.close()
