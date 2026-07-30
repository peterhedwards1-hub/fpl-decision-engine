from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from test_manager_state import CAPTURED_AT, _bundle, _snapshot

from fpl_engine.config import load_season_rules
from fpl_engine.history.database import HistoricalDatabase
from fpl_engine.history.records import IngestionSource
from fpl_engine.manager import ManagerStateRepository
from fpl_engine.model_health import build_model_health_report
from fpl_engine.news_projection import (
    create_news_projection_pair,
    evaluate_news_projection_pair,
)
from fpl_engine.projections import RatesProjectionModel
from fpl_engine.workflow import WeeklyWorkflowRepository, WorkflowError

RULES = load_season_rules(Path("config/seasons/2026-27.json"))


def test_two_pass_workflow_freezes_final_and_records_outcome(tmp_path) -> None:
    with HistoricalDatabase(tmp_path / "fpl.sqlite3") as database:
        database.initialise()
        ingestion_run_id = database.ingest_bundle(
            IngestionSource(
                name="official-fpl-api",
                retrieved_at=CAPTURED_AT,
                identifier_namespace="official-fpl",
            ),
            _bundle(),
        )
        manager_id = ManagerStateRepository(database, RULES).save(
            _snapshot(ingestion_run_id)
        )
        projection_id = RatesProjectionModel(database, RULES).project(
            season_code="2026-27",
            start_gameweek=1,
            horizon_gameweeks=1,
            generated_at=CAPTURED_AT,
        ).projection_run_id
        workflow = WeeklyWorkflowRepository(database)
        evidence_id = workflow.add_news_evidence(
            season_code="2026-27",
            gameweek_number=1,
            evidence_type="training",
            summary="Player returned to team training",
            confidence="medium",
            evidence_at=CAPTURED_AT,
            source_player_id="13",
        )
        provisional_id = workflow.create_decision_run(
            manager_snapshot_id=manager_id,
            projection_run_id=projection_id,
            mode="provisional",
            recommendation={"action": "roll", "expected_points": 52.5},
            decision_triggers=("Review training update",),
        )
        assert workflow.load(provisional_id).frozen_at is None

        with pytest.raises(WorkflowError, match="pending evidence"):
            workflow.create_decision_run(
                manager_snapshot_id=manager_id,
                projection_run_id=projection_id,
                mode="final",
                recommendation={"action": "roll", "expected_points": 53.0},
                decision_triggers=(),
                created_at=CAPTURED_AT,
            )

        workflow.review_evidence(
            evidence_id,
            status="accepted",
            rationale="Reliable training report",
            expected_minutes_adjustment=10,
        )
        pair = create_news_projection_pair(
            database,
            RULES,
            season_code="2026-27",
            gameweek_number=1,
            generated_at=CAPTURED_AT,
        )
        final_id = workflow.create_decision_run(
            manager_snapshot_id=manager_id,
            projection_run_id=pair.post_news_projection_run_id,
            mode="final",
            recommendation={"action": "roll", "expected_points": 53.0},
            decision_triggers=("Revisit if absent from squad",),
            overrides=(
                {
                    "source_player_id": "13",
                    "expected_minutes_adjustment": 10,
                    "rationale": "Reliable training report",
                },
            ),
            created_at=CAPTURED_AT,
        )
        final = workflow.load(final_id)
        assert final.frozen_at == CAPTURED_AT
        assert pair.pre_news_projection_run_id != (
            pair.post_news_projection_run_id
        )
        accepted = database.connection.execute(
            """
            SELECT original_value, accepted_value
            FROM news_evidence WHERE id = ?
            """,
            (evidence_id,),
        ).fetchone()
        assert accepted["accepted_value"] == pytest.approx(
            min(90, accepted["original_value"] + 10)
        )

        with pytest.raises(
            sqlite3.IntegrityError, match="final weekly decision runs are immutable"
        ):
            database.connection.execute(
                """
                UPDATE weekly_decision_runs SET recommendation_json = '{}'
                WHERE id = ?
                """,
                (final_id,),
            )
        database.connection.rollback()

        action_id = workflow.record_actual_action(
            final_id,
            action={
                "transfers": [],
                "chip": None,
                "captain": "13",
                "vice_captain": "8",
            },
            followed_recommendation=True,
            recorded_at=CAPTURED_AT,
        )
        evaluation_id = workflow.evaluate(
            final_id,
            realised_points=61,
            review_notes="Captain return exceeded forecast",
            evaluated_at=CAPTURED_AT,
        )
        assert action_id == 1
        assert evaluation_id == 1
        evaluation = database.connection.execute(
            "SELECT forecast_points, realised_points, score_error "
            "FROM weekly_evaluations"
        ).fetchone()
        assert dict(evaluation) == {
            "forecast_points": 53.0,
            "realised_points": 61.0,
            "score_error": 8.0,
        }
        database.connection.execute(
            "UPDATE fixtures SET finished = 1 WHERE gameweek_id = 1"
        )
        database.connection.commit()
        news_evaluation = evaluate_news_projection_pair(
            database,
            pair.pair_id,
            evaluated_at=CAPTURED_AT,
        )
        assert news_evaluation.sample_count > 0
        assert news_evaluation.pair_id == pair.pair_id
        health = build_model_health_report(database, "2026-27")
        assert health.weekly_decisions_scored == 1
        assert health.weekly_mean_absolute_error == 8.0
        assert health.weekly_bias == 8.0
        assert health.news_pairs_scored == 1
        assert health.news_points_mae_change is not None
