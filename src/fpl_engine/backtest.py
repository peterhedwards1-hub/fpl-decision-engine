"""Leakage-aware walk-forward validation for player projections."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from .config import SeasonRules
from .domain import Position
from .history.database import HistoricalDatabase
from .projections import (
    DEFAULT_MODEL_CONFIG,
    MODEL_VERSION,
    ProjectionModelConfig,
    RatesProjectionModel,
)

EvidencePolicy = Literal["performance_only", "pre_deadline_only"]


@dataclass(frozen=True)
class BacktestMetrics:
    group: str
    value: str
    samples: int
    expected_points_mean: float
    actual_points_mean: float
    points_mae: float
    points_bias: float
    points_rmse: float
    minutes_mae: float
    minutes_bias: float
    minutes_rmse: float


@dataclass(frozen=True)
class BacktestReport:
    backtest_run_id: int
    season_code: str
    model_version: str
    evidence_policy: EvidencePolicy
    origin_gameweek_start: int
    origin_gameweek_end: int
    horizon_gameweeks: int
    prediction_count: int
    overall: BacktestMetrics
    by_position: tuple[BacktestMetrics, ...]
    by_horizon: tuple[BacktestMetrics, ...]
    limitations: tuple[str, ...]
    model_config: ProjectionModelConfig

    def as_dict(self) -> dict[str, object]:
        return {
            "backtest_run_id": self.backtest_run_id,
            "season_code": self.season_code,
            "model_version": self.model_version,
            "evidence_policy": self.evidence_policy,
            "origin_gameweek_start": self.origin_gameweek_start,
            "origin_gameweek_end": self.origin_gameweek_end,
            "horizon_gameweeks": self.horizon_gameweeks,
            "prediction_count": self.prediction_count,
            "overall": asdict(self.overall),
            "by_position": [asdict(metric) for metric in self.by_position],
            "by_horizon": [asdict(metric) for metric in self.by_horizon],
            "limitations": list(self.limitations),
            "model_config": asdict(self.model_config),
        }


class ProjectionBacktester:
    """Generate each forecast at its historical origin and score later outcomes."""

    def __init__(
        self,
        database: HistoricalDatabase,
        rules: SeasonRules,
        *,
        config: ProjectionModelConfig = DEFAULT_MODEL_CONFIG,
        model_version: str = MODEL_VERSION,
    ) -> None:
        self.database = database
        self.rules = rules
        self.config = config
        self.model_version = model_version

    def run(
        self,
        *,
        season_code: str,
        origin_gameweek_start: int = 2,
        origin_gameweek_end: int = 38,
        horizon_gameweeks: int = 1,
        evidence_policy: EvidencePolicy = "performance_only",
        created_at: datetime | None = None,
    ) -> BacktestReport:
        if not 1 <= origin_gameweek_start <= origin_gameweek_end <= 38:
            raise ValueError("Backtest origin Gameweeks must be within 1–38")
        if horizon_gameweeks <= 0:
            raise ValueError("Backtest horizon must be positive")
        if evidence_policy not in {"performance_only", "pre_deadline_only"}:
            raise ValueError(f"Unknown evidence policy {evidence_policy!r}")
        season = self.database.connection.execute(
            "SELECT id FROM seasons WHERE code = ?", (season_code,)
        ).fetchone()
        if season is None:
            raise ValueError(f"Season {season_code!r} is unavailable")

        available_gameweeks = {
            int(row["number"]): row["deadline_time"]
            for row in self.database.connection.execute(
                """
                SELECT gameweeks.number, gameweeks.deadline_time
                FROM gameweeks
                JOIN seasons ON seasons.id = gameweeks.season_id
                WHERE seasons.code = ?
                """,
                (season_code,),
            )
        }
        origins = [
            gameweek
            for gameweek in range(
                origin_gameweek_start, origin_gameweek_end + 1
            )
            if gameweek in available_gameweeks
        ]
        if not origins:
            raise ValueError("No requested origin Gameweeks exist in the database")

        created = created_at or datetime.now(UTC)
        if created.tzinfo is None:
            raise ValueError("Backtest creation time must be timezone-aware")
        limitations = _limitations(evidence_policy)
        cursor = self.database.connection.execute(
            """
            INSERT INTO projection_backtest_runs (
                season_id, model_version, created_at, origin_gameweek_start,
                origin_gameweek_end, horizon_gameweeks, evidence_policy,
                model_config_json, limitations_json, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'running')
            RETURNING id
            """,
            (
                int(season["id"]),
                self.model_version,
                created.astimezone(UTC).isoformat(),
                origin_gameweek_start,
                origin_gameweek_end,
                horizon_gameweeks,
                evidence_policy,
                json.dumps(asdict(self.config), sort_keys=True),
                json.dumps(limitations),
            ),
        )
        run_id = int(cursor.fetchone()[0])
        self.database.connection.commit()

        actual = self._actual_outcomes(
            season_code,
            minimum_gameweek=origin_gameweek_start,
            maximum_gameweek=min(
                38, origin_gameweek_end + horizon_gameweeks - 1
            ),
        )
        player_season_ids = {
            row["source_player_id"]: int(row["id"])
            for row in self.database.connection.execute(
                """
                SELECT player_seasons.id, player_seasons.source_player_id
                FROM player_seasons
                JOIN seasons ON seasons.id = player_seasons.season_id
                WHERE seasons.code = ?
                  AND player_seasons.identifier_namespace = 'official-fpl'
                """,
                (season_code,),
            )
        }
        model = RatesProjectionModel(
            self.database,
            self.rules,
            config=self.config,
            model_version=self.model_version,
        )
        prediction_count = 0
        try:
            for origin in origins:
                generated_at = _historical_generation_time(
                    available_gameweeks[origin], created
                )
                projection_result = model.project(
                    season_code=season_code,
                    start_gameweek=origin,
                    horizon_gameweeks=horizon_gameweeks,
                    generated_at=generated_at,
                    observation_mode=evidence_policy,
                    use_availability=evidence_policy == "pre_deadline_only",
                    persist=False,
                )
                rows = []
                for projection in projection_result.projections:
                    if projection.fixture_count == 0:
                        continue
                    player_season_id = player_season_ids.get(
                        projection.source_player_id
                    )
                    if player_season_id is None:
                        continue
                    actual_minutes, actual_points = actual.get(
                        (
                            projection.source_player_id,
                            projection.gameweek_number,
                        ),
                        (0, 0),
                    )
                    rows.append(
                        (
                            run_id,
                            origin,
                            projection.gameweek_number,
                            projection.gameweek_number - origin + 1,
                            player_season_id,
                            projection.fixture_count,
                            projection.expected_minutes,
                            actual_minutes,
                            projection.expected_points,
                            actual_points,
                            projection.uncertainty,
                        )
                    )
                with self.database.transaction():
                    self.database.connection.executemany(
                        """
                        INSERT INTO projection_backtest_predictions (
                            backtest_run_id, origin_gameweek, target_gameweek,
                            horizon_step, player_season_id, fixture_count,
                            expected_minutes, actual_minutes, expected_points,
                            actual_points, uncertainty
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        rows,
                    )
                prediction_count += len(rows)
            if prediction_count == 0:
                raise ValueError(
                    "Backtest generated no scorable predictions; the evidence "
                    "policy may not match the available snapshots"
                )
            self.database.connection.execute(
                """
                UPDATE projection_backtest_runs
                SET status = 'completed', prediction_count = ?
                WHERE id = ?
                """,
                (prediction_count, run_id),
            )
            self.database.connection.commit()
        except Exception as error:
            self.database.connection.rollback()
            self.database.connection.execute(
                """
                UPDATE projection_backtest_runs
                SET status = 'failed', error_message = ?
                WHERE id = ?
                """,
                (str(error), run_id),
            )
            self.database.connection.commit()
            raise
        return load_backtest_report(self.database, run_id)

    def report(self, run_id: int) -> BacktestReport:
        return load_backtest_report(self.database, run_id)

    def _actual_outcomes(
        self,
        season_code: str,
        *,
        minimum_gameweek: int,
        maximum_gameweek: int,
    ) -> dict[tuple[str, int], tuple[int, int]]:
        rows = self.database.connection.execute(
            """
            SELECT player_seasons.source_player_id,
                   gameweeks.number AS gameweek_number,
                   SUM(stats.minutes) AS actual_minutes,
                   SUM(stats.total_points) AS actual_points
            FROM player_fixture_stats stats
            JOIN player_seasons
              ON player_seasons.id = stats.player_season_id
            JOIN fixtures ON fixtures.id = stats.fixture_id
            JOIN gameweeks ON gameweeks.id = fixtures.gameweek_id
            JOIN seasons ON seasons.id = fixtures.season_id
            WHERE seasons.code = ?
              AND gameweeks.number BETWEEN ? AND ?
            GROUP BY player_seasons.id, gameweeks.number
            """,
            (season_code, minimum_gameweek, maximum_gameweek),
        ).fetchall()
        return {
            (row["source_player_id"], int(row["gameweek_number"])): (
                int(row["actual_minutes"]),
                int(row["actual_points"]),
            )
            for row in rows
        }


def load_backtest_report(
    database: HistoricalDatabase, run_id: int
) -> BacktestReport:
    """Load a completed persisted backtest and calculate its scorecard."""

    run = database.connection.execute(
        """
        SELECT backtests.*, seasons.code AS season_code
        FROM projection_backtest_runs backtests
        JOIN seasons ON seasons.id = backtests.season_id
        WHERE backtests.id = ? AND backtests.status = 'completed'
        """,
        (run_id,),
    ).fetchone()
    if run is None:
        raise ValueError(f"Completed backtest run {run_id} is unavailable")
    rows = database.connection.execute(
        """
        SELECT predictions.*, player_seasons.position
        FROM projection_backtest_predictions predictions
        JOIN player_seasons
          ON player_seasons.id = predictions.player_season_id
        WHERE predictions.backtest_run_id = ?
        """,
        (run_id,),
    ).fetchall()
    overall = _metrics("overall", "all", rows)
    by_position = tuple(
        _metrics(
            "position",
            position.value,
            [row for row in rows if row["position"] == position.value],
        )
        for position in Position
        if any(row["position"] == position.value for row in rows)
    )
    horizon_steps = sorted({int(row["horizon_step"]) for row in rows})
    by_horizon = tuple(
        _metrics(
            "horizon",
            str(step),
            [row for row in rows if row["horizon_step"] == step],
        )
        for step in horizon_steps
    )
    return BacktestReport(
        backtest_run_id=run_id,
        season_code=run["season_code"],
        model_version=run["model_version"],
        evidence_policy=run["evidence_policy"],
        origin_gameweek_start=run["origin_gameweek_start"],
        origin_gameweek_end=run["origin_gameweek_end"],
        horizon_gameweeks=run["horizon_gameweeks"],
        prediction_count=run["prediction_count"],
        overall=overall,
        by_position=by_position,
        by_horizon=by_horizon,
        limitations=tuple(json.loads(run["limitations_json"])),
        model_config=ProjectionModelConfig(
            **json.loads(run["model_config_json"])
        ),
    )


def _metrics(group: str, value: str, rows: list[object]) -> BacktestMetrics:
    if not rows:
        raise ValueError(f"Cannot calculate empty backtest metric {group}:{value}")
    point_errors = [
        float(row["actual_points"]) - float(row["expected_points"])
        for row in rows
    ]
    minute_errors = [
        float(row["actual_minutes"]) - float(row["expected_minutes"])
        for row in rows
    ]
    count = len(rows)
    return BacktestMetrics(
        group=group,
        value=value,
        samples=count,
        expected_points_mean=round(
            sum(float(row["expected_points"]) for row in rows) / count, 4
        ),
        actual_points_mean=round(
            sum(float(row["actual_points"]) for row in rows) / count, 4
        ),
        points_mae=round(sum(abs(error) for error in point_errors) / count, 4),
        points_bias=round(sum(point_errors) / count, 4),
        points_rmse=round(
            math.sqrt(sum(error**2 for error in point_errors) / count), 4
        ),
        minutes_mae=round(
            sum(abs(error) for error in minute_errors) / count, 4
        ),
        minutes_bias=round(sum(minute_errors) / count, 4),
        minutes_rmse=round(
            math.sqrt(sum(error**2 for error in minute_errors) / count), 4
        ),
    )


def _historical_generation_time(
    deadline_time: str | None, fallback: datetime
) -> datetime:
    if deadline_time is None:
        return fallback
    deadline = datetime.fromisoformat(deadline_time.replace("Z", "+00:00"))
    return deadline.astimezone(UTC) - timedelta(seconds=1)


def _limitations(evidence_policy: EvidencePolicy) -> tuple[str, ...]:
    common = (
        "Only structured database fields are replayed; press conferences, "
        "predicted lineups and manual role judgements are unavailable unless "
        "they were captured separately.",
        "Actual outcomes are used only after projections have been generated "
        "with a strict Gameweek and season cutoff.",
    )
    if evidence_policy == "pre_deadline_only":
        return (
            *common,
            "Availability uses only exact live_pre_deadline observations whose "
            "capture time precedes the recorded deadline.",
        )
    return (
        *common,
        "Historical reconstructed snapshots have unknown capture times, so "
        "status and chance-of-playing fields are ignored.",
        "Historical team membership is used to attach the player to fixtures; "
        "historical price is not a projection input.",
    )
