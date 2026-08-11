from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

from fpl_engine.history.database import HistoricalDatabase
from fpl_engine.production import (
    recommend_planning_horizon,
    select_production_projection_run,
)
from fpl_engine.projections import MODEL_VERSION


def _insert_run(
    database: HistoricalDatabase,
    *,
    generated_at: str,
    horizon: int,
    model_version: str,
) -> int:
    cursor = database.connection.execute(
        """
        INSERT INTO projection_runs (
            season_id, generated_at, start_gameweek, horizon_gameweeks,
            model_version, observation_mode, assumptions_json
        ) VALUES (1, ?, 1, ?, ?, 'latest_available', '{}')
        RETURNING id
        """,
        (generated_at, horizon, model_version),
    )
    return int(cursor.fetchone()[0])


def test_production_selection_rejects_newer_short_and_challenger_runs(tmp_path) -> None:
    with HistoricalDatabase(tmp_path / "fpl.sqlite3") as database:
        database.initialise()
        database.connection.execute(
            "INSERT INTO seasons (id, code, name) VALUES (1, '2026-27', '2026/27')"
        )
        production_id = _insert_run(
            database,
            generated_at="2026-08-01T12:00:00+00:00",
            horizon=8,
            model_version=MODEL_VERSION,
        )
        _insert_run(
            database,
            generated_at="2026-08-02T12:00:00+00:00",
            horizon=8,
            model_version="rates-unqualified-challenger-v1",
        )
        _insert_run(
            database,
            generated_at="2026-08-03T12:00:00+00:00",
            horizon=1,
            model_version=f"{MODEL_VERSION}-post-news-v2",
        )
        _insert_run(
            database,
            generated_at="2026-08-04T12:00:00+00:00",
            horizon=8,
            model_version=f"{MODEL_VERSION}-unqualified-experiment",
        )

        selected = select_production_projection_run(
            database,
            season_code="2026-27",
            start_gameweek=1,
            minimum_horizon_gameweeks=8,
        )

        assert selected is not None
        assert selected.run_id == production_id
        assert selected.horizon_gameweeks == 8


def test_production_selection_prefers_the_validated_preseason_model(tmp_path) -> None:
    # Before the season starts the validated preseason carry-forward run is the
    # strongest forecast and must be chosen over an older standard-model run, so
    # squad and transfer advice share one model. An unqualified challenger that
    # merely borrows the "preseason" word must still be rejected.
    with HistoricalDatabase(tmp_path / "fpl.sqlite3") as database:
        database.initialise()
        database.connection.execute(
            "INSERT INTO seasons (id, code, name) VALUES (1, '2026-27', '2026/27')"
        )
        _insert_run(
            database,
            generated_at="2026-08-02T12:00:00+00:00",
            horizon=8,
            model_version=MODEL_VERSION,
        )
        preseason_id = _insert_run(
            database,
            generated_at="2026-08-08T12:00:00+00:00",
            horizon=8,
            model_version=f"{MODEL_VERSION}-preseason-carry-forward-promoted-fixed",
        )
        _insert_run(
            database,
            generated_at="2026-08-09T12:00:00+00:00",
            horizon=8,
            model_version="rates-unqualified-challenger-preseason",
        )

        selected = select_production_projection_run(
            database,
            season_code="2026-27",
            start_gameweek=1,
            minimum_horizon_gameweeks=8,
        )

        assert selected is not None
        assert selected.run_id == preseason_id


def test_production_selection_accepts_newer_long_horizon_news_run(tmp_path) -> None:
    with HistoricalDatabase(tmp_path / "fpl.sqlite3") as database:
        database.initialise()
        database.connection.execute(
            "INSERT INTO seasons (id, code, name) VALUES (1, '2026-27', '2026/27')"
        )
        _insert_run(
            database,
            generated_at="2026-08-01T12:00:00+00:00",
            horizon=8,
            model_version=MODEL_VERSION,
        )
        news_id = _insert_run(
            database,
            generated_at="2026-08-02T12:00:00+00:00",
            horizon=8,
            model_version=f"{MODEL_VERSION}-post-news-v2",
        )

        selected = select_production_projection_run(
            database,
            season_code="2026-27",
            start_gameweek=1,
            minimum_horizon_gameweeks=5,
        )

        assert selected is not None
        assert selected.run_id == news_id


def test_production_selection_returns_none_without_a_qualified_run(tmp_path) -> None:
    with HistoricalDatabase(tmp_path / "fpl.sqlite3") as database:
        database.initialise()
        database.connection.execute(
            "INSERT INTO seasons (id, code, name) VALUES (1, '2026-27', '2026/27')"
        )
        _insert_run(
            database,
            generated_at="2026-08-03T12:00:00+00:00",
            horizon=1,
            model_version=MODEL_VERSION,
        )

        assert (
            select_production_projection_run(
                database,
                season_code="2026-27",
                start_gameweek=1,
                minimum_horizon_gameweeks=8,
            )
            is None
        )


def test_planning_horizon_extends_for_known_exception_and_chip_expiry() -> None:
    connection = Mock()
    connection.execute.return_value.fetchall.return_value = (
        {"gameweek_number": 6, "fixture_count": 1},
        {"gameweek_number": 7, "fixture_count": 2},
    )
    database = SimpleNamespace(connection=connection)

    recommendation = recommend_planning_horizon(
        database,
        season_code="2026-27",
        start_gameweek=1,
        base_horizon_gameweeks=5,
        extension_window_gameweeks=3,
        chip_expiry_gameweek=8,
    )

    assert recommendation.required_horizon_gameweeks == 8
    assert recommendation.exceptional_gameweeks == (7, 8)
    assert any("blank or double" in reason for reason in recommendation.reasons)
    assert any("chip set expires" in reason for reason in recommendation.reasons)
    connection.execute.assert_called_once()


def test_planning_horizon_never_extends_past_the_season() -> None:
    database = SimpleNamespace(connection=Mock())

    recommendation = recommend_planning_horizon(
        database,
        season_code="2026-27",
        start_gameweek=37,
    )

    assert recommendation.base_horizon_gameweeks == 2
    assert recommendation.required_horizon_gameweeks == 2
    database.connection.execute.assert_not_called()
