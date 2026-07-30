import json
import sqlite3
from datetime import UTC, datetime

import pytest

from fpl_engine.history.database import HistoricalDatabase
from fpl_engine.promotion import (
    DecisionGateEvidence,
    PromotionGatePolicy,
    evaluate_forward_candidate,
    register_forward_candidate,
)
from fpl_engine.prospective import build_prospective_capture_status


def test_forward_candidate_registration_is_hashed_and_immutable(tmp_path) -> None:
    with HistoricalDatabase(tmp_path / "fpl.sqlite3") as database:
        database.initialise()
        database.connection.execute(
            """
            INSERT INTO seasons (code, name)
            VALUES ('2026-27', '2026/27')
            """
        )
        database.connection.commit()

        registration = register_forward_candidate(
            database,
            candidate_key="team-share-v1",
            season_code="2026-27",
            model_version="team-share-v1",
            model_config={"scoring_event_source": "team_share_expected"},
            registered_at=datetime(2026, 7, 30, tzinfo=UTC),
        )

        assert len(registration["model_config_sha256"]) == 64
        stored = database.connection.execute(
            """
            SELECT model_config_json, status
            FROM model_candidate_registrations
            """
        ).fetchone()
        assert json.loads(stored["model_config_json"]) == {
            "scoring_event_source": "team_share_expected"
        }
        assert stored["status"] == "declared"
        with pytest.raises(
            sqlite3.IntegrityError,
            match="declarations are immutable",
        ):
            database.connection.execute(
                """
                UPDATE model_candidate_registrations
                SET model_version = 'changed'
                """
            )


def test_historical_candidate_registration_is_rejected(tmp_path) -> None:
    with HistoricalDatabase(tmp_path / "fpl.sqlite3") as database:
        database.initialise()
        database.connection.execute(
            "INSERT INTO seasons (code, name) VALUES ('2025-26', '2025/26')"
        )
        database.connection.commit()

        with pytest.raises(ValueError, match="forward seasons"):
            register_forward_candidate(
                database,
                candidate_key="leaking",
                season_code="2025-26",
                model_version="bad",
                model_config={},
            )


def test_prospective_status_requires_each_registered_candidate_run(
    tmp_path,
) -> None:
    with HistoricalDatabase(tmp_path / "fpl.sqlite3") as database:
        database.initialise()
        database.connection.execute(
            """
            INSERT INTO seasons (id, code, name)
            VALUES (1, '2026-27', '2026/27')
            """
        )
        database.connection.execute(
            """
            INSERT INTO ingestion_runs (
                id, source_name, retrieved_at, status
            ) VALUES (
                1, 'test', '2026-07-30T00:00:00+00:00', 'completed'
            )
            """
        )
        database.connection.execute(
            """
            INSERT INTO gameweeks (
                season_id, number, deadline_time, provenance_run_id
            ) VALUES (
                1, 1, '2026-08-21T17:30:00+00:00', 1
            )
            """
        )
        database.connection.commit()
        register_forward_candidate(
            database,
            candidate_key="candidate",
            season_code="2026-27",
            model_version="candidate-v1",
            model_config={"value": 1},
            registered_at=datetime(2026, 7, 30, tzinfo=UTC),
        )

        report = build_prospective_capture_status(
            database,
            "2026-27",
            as_of=datetime(2026, 8, 22, tzinfo=UTC),
        )

    assert "candidate_projection:candidate" in (
        report["gameweeks"][0]["missing_required"]
    )
    assert (
        report["gameweeks"][0]["candidate_projections"]["candidate"][
            "valid_pre_deadline_runs"
        ]
        == 0
    )


def test_matched_forward_rows_can_qualify_declared_candidate(
    tmp_path,
) -> None:
    policy = PromotionGatePolicy(
        minimum_samples=4,
        minimum_position_samples=1,
        bootstrap_samples=100,
    )
    with HistoricalDatabase(tmp_path / "fpl.sqlite3") as database:
        database.initialise()
        database.connection.executescript(
            """
            INSERT INTO ingestion_runs (
                id, source_name, retrieved_at, status
            ) VALUES (
                1, 'test', '2026-07-30T00:00:00+00:00', 'completed'
            );
            INSERT INTO seasons (id, code, name)
            VALUES (1, '2026-27', '2026/27');
            INSERT INTO teams (
                id, season_id, identifier_namespace, source_team_id,
                name, short_name, provenance_run_id
            ) VALUES
                (1, 1, 'official-fpl', '1', 'One', 'ONE', 1),
                (2, 1, 'official-fpl', '2', 'Two', 'TWO', 1);
            INSERT INTO gameweeks (
                id, season_id, number, deadline_time, provenance_run_id
            ) VALUES (
                1, 1, 1, '2026-08-21T17:30:00+00:00', 1
            );
            """
        )
        for index, position in enumerate(
            ("GK", "DEF", "MID", "FWD"),
            start=1,
        ):
            database.connection.execute(
                """
                INSERT INTO players (id, web_name, provenance_run_id)
                VALUES (?, ?, 1)
                """,
                (index, position),
            )
            database.connection.execute(
                """
                INSERT INTO player_seasons (
                    id, season_id, player_id, identifier_namespace,
                    source_player_id, team_id, position, provenance_run_id
                ) VALUES (?, 1, ?, 'official-fpl', ?, 1, ?, 1)
                """,
                (index, index, str(index), position),
            )
        run_values = (
            (
                1,
                "incumbent",
                '{"value":0}',
            ),
            (
                2,
                "candidate-v1",
                '{"value":1}',
            ),
        )
        for run_id, version, config in run_values:
            database.connection.execute(
                """
                INSERT INTO projection_backtest_runs (
                    id, season_id, model_version, created_at,
                    origin_gameweek_start, origin_gameweek_end,
                    horizon_gameweeks, evidence_policy,
                    model_config_json, limitations_json, status,
                    prediction_count
                ) VALUES (
                    ?, 1, ?, '2026-08-22T00:00:00+00:00',
                    1, 1, 1, 'pre_deadline_only',
                    ?, '[]', 'completed', 4
                )
                """,
                (run_id, version, config),
            )
            for player_id in range(1, 5):
                database.connection.execute(
                    """
                    INSERT INTO projection_backtest_predictions (
                        backtest_run_id, origin_gameweek,
                        target_gameweek, horizon_step,
                        player_season_id, fixture_count,
                        expected_minutes, appearance_probability,
                        sixty_probability, actual_minutes,
                        expected_points, actual_points, uncertainty
                    ) VALUES (
                        ?, 1, 1, 1, ?, 1, 75, 0.9, 0.8,
                        90, 2, 2, 1
                    )
                    """,
                    (run_id, player_id),
                )
        database.connection.commit()
        register_forward_candidate(
            database,
            candidate_key="candidate",
            season_code="2026-27",
            model_version="candidate-v1",
            model_config={"value": 1},
            gate_policy=policy,
            registered_at=datetime(2026, 7, 30, tzinfo=UTC),
        )

        report = evaluate_forward_candidate(
            database,
            candidate_key="candidate",
            incumbent_run_ids=(1,),
            challenger_run_ids=(2,),
            decision_evidence=DecisionGateEvidence(
                legal_squad_regret_change=0,
                owned_captain_regret_change=0,
                transfer_regret_change=0,
                source_report="test",
            ),
        )

        assert report["passed"] is True
        assert report["status"] == "qualified"
        assert database.connection.execute(
            """
            SELECT status FROM model_candidate_registrations
            WHERE candidate_key = 'candidate'
            """
        ).fetchone()[0] == "qualified"


def test_probability_gate_fails_cleanly_without_single_fixture_rows(
    tmp_path,
) -> None:
    policy = PromotionGatePolicy(
        minimum_samples=4,
        minimum_position_samples=1,
        bootstrap_samples=100,
    )
    with HistoricalDatabase(tmp_path / "fpl.sqlite3") as database:
        database.initialise()
        database.connection.executescript(
            """
            INSERT INTO ingestion_runs (
                id, source_name, retrieved_at, status
            ) VALUES (
                1, 'test', '2026-07-30T00:00:00+00:00', 'completed'
            );
            INSERT INTO seasons (id, code, name)
            VALUES (1, '2026-27', '2026/27');
            INSERT INTO teams (
                id, season_id, identifier_namespace, source_team_id,
                name, short_name, provenance_run_id
            ) VALUES
                (1, 1, 'official-fpl', '1', 'One', 'ONE', 1),
                (2, 1, 'official-fpl', '2', 'Two', 'TWO', 1);
            INSERT INTO gameweeks (
                id, season_id, number, deadline_time, provenance_run_id
            ) VALUES (
                1, 1, 1, '2026-08-21T17:30:00+00:00', 1
            );
            """
        )
        for index, position in enumerate(("GK", "DEF", "MID", "FWD"), start=1):
            database.connection.execute(
                """
                INSERT INTO players (id, web_name, provenance_run_id)
                VALUES (?, ?, 1)
                """,
                (index, position),
            )
            database.connection.execute(
                """
                INSERT INTO player_seasons (
                    id, season_id, player_id, identifier_namespace,
                    source_player_id, team_id, position, provenance_run_id
                ) VALUES (?, 1, ?, 'official-fpl', ?, 1, ?, 1)
                """,
                (index, index, str(index), position),
            )
        for run_id, version, config in (
            (1, "incumbent", '{"value":0}'),
            (2, "candidate-v1", '{"value":1}'),
        ):
            database.connection.execute(
                """
                INSERT INTO projection_backtest_runs (
                    id, season_id, model_version, created_at,
                    origin_gameweek_start, origin_gameweek_end,
                    horizon_gameweeks, evidence_policy,
                    model_config_json, limitations_json, status,
                    prediction_count
                ) VALUES (
                    ?, 1, ?, '2026-08-22T00:00:00+00:00',
                    1, 1, 1, 'pre_deadline_only',
                    ?, '[]', 'completed', 4
                )
                """,
                (run_id, version, config),
            )
            for player_id in range(1, 5):
                database.connection.execute(
                    """
                    INSERT INTO projection_backtest_predictions (
                        backtest_run_id, origin_gameweek,
                        target_gameweek, horizon_step,
                        player_season_id, fixture_count,
                        expected_minutes, appearance_probability,
                        sixty_probability, actual_minutes,
                        expected_points, actual_points, uncertainty
                    ) VALUES (
                        ?, 1, 1, 1, ?, 2, 75, 0.9, 0.8,
                        90, 2, 2, 1
                    )
                    """,
                    (run_id, player_id),
                )
        database.connection.commit()
        register_forward_candidate(
            database,
            candidate_key="candidate",
            season_code="2026-27",
            model_version="candidate-v1",
            model_config={"value": 1},
            gate_policy=policy,
            registered_at=datetime(2026, 7, 30, tzinfo=UTC),
        )

        report = evaluate_forward_candidate(
            database,
            candidate_key="candidate",
            incumbent_run_ids=(1,),
            challenger_run_ids=(2,),
            decision_evidence=DecisionGateEvidence(
                legal_squad_regret_change=0,
                owned_captain_regret_change=0,
                transfer_regret_change=0,
                source_report="test",
            ),
        )

    assert report["passed"] is False
    assert report["gates"]["probability_evidence"] is False
    assert report["probability"]["samples"] == 0
