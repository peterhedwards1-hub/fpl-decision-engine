"""Post-Gameweek scoring and model-version comparison."""

from __future__ import annotations

import math
from dataclasses import dataclass

from .history.database import HistoricalDatabase


@dataclass(frozen=True)
class ModelVersionHealth:
    model_version: str
    samples: int
    mean_absolute_error: float
    bias: float
    root_mean_square_error: float


@dataclass(frozen=True)
class ModelHealthReport:
    season_code: str
    versions: tuple[ModelVersionHealth, ...]
    weekly_decisions_scored: int
    weekly_mean_absolute_error: float | None
    weekly_bias: float | None


def build_model_health_report(
    database: HistoricalDatabase, season_code: str
) -> ModelHealthReport:
    rows = database.connection.execute(
        """
        WITH actual AS (
            SELECT stats.player_season_id, gameweeks.number AS gameweek_number,
                   SUM(stats.total_points) AS actual_points
            FROM player_fixture_stats stats
            JOIN fixtures ON fixtures.id = stats.fixture_id
            JOIN gameweeks ON gameweeks.id = fixtures.gameweek_id
            JOIN seasons ON seasons.id = fixtures.season_id
            WHERE seasons.code = ?
            GROUP BY stats.player_season_id, gameweeks.number
        )
        SELECT runs.model_version, projections.expected_points,
               actual.actual_points
        FROM player_gameweek_projections projections
        JOIN projection_runs runs ON runs.id = projections.projection_run_id
        JOIN seasons ON seasons.id = runs.season_id
        JOIN actual
          ON actual.player_season_id = projections.player_season_id
         AND actual.gameweek_number = projections.gameweek_number
        WHERE seasons.code = ?
        """,
        (season_code, season_code),
    ).fetchall()
    by_version: dict[str, list[float]] = {}
    for row in rows:
        by_version.setdefault(row["model_version"], []).append(
            float(row["actual_points"]) - float(row["expected_points"])
        )
    versions = tuple(
        ModelVersionHealth(
            model_version=version,
            samples=len(errors),
            mean_absolute_error=round(
                sum(abs(error) for error in errors) / len(errors), 3
            ),
            bias=round(sum(errors) / len(errors), 3),
            root_mean_square_error=round(
                math.sqrt(sum(error**2 for error in errors) / len(errors)),
                3,
            ),
        )
        for version, errors in sorted(by_version.items())
    )
    weekly = database.connection.execute(
        """
        SELECT evaluations.score_error
        FROM weekly_evaluations evaluations
        JOIN weekly_decision_runs runs
          ON runs.id = evaluations.weekly_decision_run_id
        JOIN seasons ON seasons.id = runs.season_id
        WHERE seasons.code = ?
        """,
        (season_code,),
    ).fetchall()
    weekly_errors = [float(row["score_error"]) for row in weekly]
    return ModelHealthReport(
        season_code=season_code,
        versions=versions,
        weekly_decisions_scored=len(weekly_errors),
        weekly_mean_absolute_error=(
            None
            if not weekly_errors
            else round(
                sum(abs(error) for error in weekly_errors)
                / len(weekly_errors),
                3,
            )
        ),
        weekly_bias=(
            None
            if not weekly_errors
            else round(sum(weekly_errors) / len(weekly_errors), 3)
        ),
    )
