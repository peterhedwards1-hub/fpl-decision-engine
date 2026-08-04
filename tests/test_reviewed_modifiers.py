# ruff: noqa: I001
from datetime import datetime

import pytest
from test_projections import CAPTURED_AT, RULES, _bundle

from fpl_engine.history.database import HistoricalDatabase
from fpl_engine.history.records import IngestionSource
from fpl_engine.projections import RatesProjectionModel
from fpl_engine.reviewed_modifiers import (
    ModifierValidationError,
    ReviewedProjectionModifier,
    active_modifiers,
    create_reviewed_modifier,
)
from fpl_engine.workflow import WeeklyWorkflowRepository




def _database() -> HistoricalDatabase:
    database = HistoricalDatabase(":memory:")
    database.initialise()
    database.ingest_bundle(
        IngestionSource(
            name="official-fpl-api",
            retrieved_at=CAPTURED_AT,
            identifier_namespace="official-fpl",
        ),
        _bundle(),
    )
    return database


def test_modifier_domain_rejects_bad_probability_and_naive_time() -> None:
    with pytest.raises(ModifierValidationError, match="between 0 and 1"):
        ReviewedProjectionModifier(
            source_player_id="101",
            modifier_type="appearance_probability",
            operation="set",
            value=1.2,
            start_gameweek=1,
            end_gameweek=1,
            evidence_ids=(1,),
            rationale="test",
            reviewed_by="user",
            reviewed_at=CAPTURED_AT,
        )
    with pytest.raises(ModifierValidationError, match="timezone-aware"):
        ReviewedProjectionModifier(
            source_player_id="101",
            modifier_type="expected_minutes",
            operation="set",
            value=0,
            start_gameweek=1,
            end_gameweek=1,
            evidence_ids=(1,),
            rationale="test",
            reviewed_by="user",
            reviewed_at=datetime(2026, 8, 1),
        )


def test_accepted_modifier_applies_only_to_its_gameweek_range() -> None:
    database = _database()
    try:
        workflow = WeeklyWorkflowRepository(database)
        evidence_id = workflow.add_news_evidence(
            season_code="2026-27",
            gameweek_number=1,
            evidence_type="injury",
            summary="Unavailable for the first Gameweek",
            confidence="high",
            evidence_at=CAPTURED_AT,
            source_player_id="101",
            schema_version=3,
            source_name="Official club",
            source_tier="official",
            model_area="availability",
            adjustment_support="supported_numeric",
            research_mode="final",
            research_run_id="research-1",
            input_package_id="package-1",
        )
        workflow.review_evidence(
            evidence_id,
            status="accepted",
            rationale="Confirmed unavailable for GW1.",
            reviewed_at=CAPTURED_AT,
        )
        modifier = ReviewedProjectionModifier(
            source_player_id="101",
            modifier_type="availability",
            operation="set",
            value=0.0,
            start_gameweek=1,
            end_gameweek=1,
            evidence_ids=(evidence_id,),
            rationale="Confirmed unavailable for GW1.",
            reviewed_by="user",
            reviewed_at=CAPTURED_AT,
            research_run_id="research-1",
            input_package_id="package-1",
        )
        modifier_id = create_reviewed_modifier(
            database,
            season_code="2026-27",
            gameweek_number=1,
            modifier=modifier,
        )
        assert modifier_id == 1
        assert len(active_modifiers(
            database,
            season_code="2026-27",
            start_gameweek=1,
            horizon_gameweeks=2,
            now=CAPTURED_AT,
        )) == 1
        baseline = RatesProjectionModel(database, RULES).project(
            season_code="2026-27",
            start_gameweek=1,
            horizon_gameweeks=2,
            generated_at=CAPTURED_AT,
        )
        from fpl_engine.research_decision import generate_revised_projection

        revised = generate_revised_projection(
            database,
            RULES,
            baseline_projection_run_id=int(baseline.projection_run_id),
            decision_type="weekly_xi",
            generated_at=CAPTURED_AT,
        )
        rows = database.connection.execute(
            """
            SELECT gameweek_number, expected_minutes, appearance_probability
            FROM player_gameweek_projections
            WHERE projection_run_id = ?
            ORDER BY gameweek_number
            """,
            (revised.revised_projection_run_id,),
        ).fetchall()
        assert rows[0]["expected_minutes"] == 0.0
        assert rows[0]["appearance_probability"] == 0.0
        assert rows[1]["expected_minutes"] > 0.0
        links = database.connection.execute(
            "SELECT modifier_id FROM projection_run_modifier_links"
        ).fetchall()
        assert [row[0] for row in links] == [modifier_id]
    finally:
        database.close()


def test_informational_review_creates_no_modifier() -> None:
    database = _database()
    try:
        workflow = WeeklyWorkflowRepository(database)
        evidence_id = workflow.add_news_evidence(
            season_code="2026-27",
            gameweek_number=1,
            evidence_type="training",
            summary="Background only",
            confidence="medium",
            evidence_at=CAPTURED_AT,
            source_player_id="101",
            schema_version=3,
            source_name="Reporter",
            source_tier="strong_reporting",
            model_area="informational",
            adjustment_support="informational",
            research_mode="preseason",
        )
        workflow.review_evidence(evidence_id, status="accepted", rationale="")
        assert database.connection.execute(
            "SELECT COUNT(*) FROM reviewed_projection_modifiers"
        ).fetchone()[0] == 0
    finally:
        database.close()
