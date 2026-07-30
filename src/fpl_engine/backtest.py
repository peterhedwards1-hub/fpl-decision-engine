"""Leakage-aware walk-forward validation for player projections."""

from __future__ import annotations

import hashlib
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
    LEGACY_MODEL_VERSION,
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
    generated_prediction_count: int
    prediction_count: int
    missing_outcome_count: int
    source_ingestion_run_id: int | None
    data_fingerprint: str | None
    overall: BacktestMetrics
    by_position: tuple[BacktestMetrics, ...]
    by_horizon: tuple[BacktestMetrics, ...]
    by_participation: tuple[BacktestMetrics, ...]
    by_fixture_count: tuple[BacktestMetrics, ...]
    top_n: tuple[BacktestMetrics, ...]
    expected_minutes_per_match: float
    actual_minutes_per_match: float
    regulation_minutes_per_match: float
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
            "generated_prediction_count": self.generated_prediction_count,
            "prediction_count": self.prediction_count,
            "missing_outcome_count": self.missing_outcome_count,
            "source_ingestion_run_id": self.source_ingestion_run_id,
            "data_fingerprint": self.data_fingerprint,
            "overall": asdict(self.overall),
            "by_position": [asdict(metric) for metric in self.by_position],
            "by_horizon": [asdict(metric) for metric in self.by_horizon],
            "by_participation": [
                asdict(metric) for metric in self.by_participation
            ],
            "by_fixture_count": [
                asdict(metric) for metric in self.by_fixture_count
            ],
            "top_n": [asdict(metric) for metric in self.top_n],
            "expected_minutes_per_match": self.expected_minutes_per_match,
            "actual_minutes_per_match": self.actual_minutes_per_match,
            "regulation_minutes_per_match": (
                self.regulation_minutes_per_match
            ),
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
        origins_without_deadlines = [
            gameweek
            for gameweek in origins
            if available_gameweeks[gameweek] is None
        ]
        if origins_without_deadlines and evidence_policy == "pre_deadline_only":
            raise ValueError(
                "Pre-deadline backtest origins require recorded deadline "
                "times; missing for "
                + ", ".join(f"GW{gameweek}" for gameweek in origins_without_deadlines)
            )

        created = created_at or datetime.now(UTC)
        if created.tzinfo is None:
            raise ValueError("Backtest creation time must be timezone-aware")
        limitations = _limitations(evidence_policy)
        source_run = self.database.connection.execute(
            """
            SELECT MAX(id) AS id
            FROM ingestion_runs
            WHERE status = 'completed'
            """
        ).fetchone()
        source_ingestion_run_id = (
            None if source_run["id"] is None else int(source_run["id"])
        )
        data_fingerprint = _data_fingerprint(
            self.database,
            season_code,
            maximum_ingestion_run_id=source_ingestion_run_id,
        )
        cursor = self.database.connection.execute(
            """
            INSERT INTO projection_backtest_runs (
                season_id, model_version, created_at, origin_gameweek_start,
                origin_gameweek_end, horizon_gameweeks, evidence_policy,
                model_config_json, limitations_json, source_ingestion_run_id,
                data_fingerprint, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'running')
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
                source_ingestion_run_id,
                data_fingerprint,
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
        generated_prediction_count = 0
        prediction_count = 0
        missing_outcome_count = 0
        try:
            for origin in origins:
                generated_at = _historical_generation_time(
                    available_gameweeks[origin],
                    fallback=created,
                )
                projection_result = model.project(
                    season_code=season_code,
                    start_gameweek=origin,
                    horizon_gameweeks=horizon_gameweeks,
                    generated_at=generated_at,
                    observation_mode=evidence_policy,
                    use_availability=evidence_policy == "pre_deadline_only",
                    fixture_as_of=(
                        generated_at
                        if evidence_policy == "pre_deadline_only"
                        else None
                    ),
                    fixture_max_ingestion_run_id=source_ingestion_run_id,
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
                    generated_prediction_count += 1
                    outcome = actual.get(
                        (projection.source_player_id, projection.gameweek_number)
                    )
                    if (
                        outcome is None
                        or outcome[0] < projection.fixture_count
                    ):
                        missing_outcome_count += 1
                        continue
                    _, actual_minutes, actual_points = outcome
                    rows.append(
                        (
                            run_id,
                            origin,
                            projection.gameweek_number,
                            projection.gameweek_number - origin + 1,
                            player_season_id,
                            projection.fixture_count,
                            projection.expected_minutes,
                            projection.appearance_probability,
                            projection.sixty_probability,
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
                            expected_minutes, appearance_probability,
                            sixty_probability, actual_minutes,
                            expected_points, actual_points, uncertainty
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                SET status = 'completed', generated_prediction_count = ?,
                    prediction_count = ?, missing_outcome_count = ?
                WHERE id = ?
                """,
                (
                    generated_prediction_count,
                    prediction_count,
                    missing_outcome_count,
                    run_id,
                ),
            )
            self.database.connection.commit()
        except Exception as error:
            self.database.connection.rollback()
            with self.database.transaction():
                self.database.connection.execute(
                    """
                    DELETE FROM projection_backtest_predictions
                    WHERE backtest_run_id = ?
                    """,
                    (run_id,),
                )
                self.database.connection.execute(
                    """
                    UPDATE projection_backtest_runs
                    SET status = 'failed', error_message = ?,
                        generated_prediction_count = 0,
                        prediction_count = 0,
                        missing_outcome_count = 0
                    WHERE id = ?
                    """,
                    (str(error), run_id),
                )
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
    ) -> dict[tuple[str, int], tuple[int, int, int]]:
        rows = self.database.connection.execute(
            """
            SELECT player_seasons.source_player_id,
                   gameweeks.number AS gameweek_number,
                   COUNT(DISTINCT stats.fixture_id) AS observed_fixture_count,
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
                int(row["observed_fixture_count"]),
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
    by_participation = tuple(
        _metrics(
            "participation",
            value,
            [
                row
                for row in rows
                if (int(row["actual_minutes"]) == 0) == is_dnp
            ],
        )
        for value, is_dnp in (("DNP", True), ("played", False))
        if any(
            (int(row["actual_minutes"]) == 0) == is_dnp for row in rows
        )
    )
    fixture_counts = sorted({int(row["fixture_count"]) for row in rows})
    by_fixture_count = tuple(
        _metrics(
            "fixture_count",
            str(fixture_count),
            [
                row
                for row in rows
                if int(row["fixture_count"]) == fixture_count
            ],
        )
        for fixture_count in fixture_counts
    )
    ranked_groups: dict[tuple[int, int], list[object]] = {}
    for row in rows:
        ranked_groups.setdefault(
            (int(row["origin_gameweek"]), int(row["target_gameweek"])),
            [],
        ).append(row)
    top_n = tuple(
        _metrics(
            "top_n",
            str(cutoff),
            [
                row
                for group_rows in ranked_groups.values()
                for row in sorted(
                    group_rows,
                    key=lambda item: (
                        -float(item["expected_points"]),
                        int(item["player_season_id"]),
                    ),
                )[:cutoff]
            ],
        )
        for cutoff in (15, 50, 100)
    )
    evaluated_origin_targets = {
        (int(row["origin_gameweek"]), int(row["target_gameweek"]))
        for row in rows
    }
    match_count = sum(
        int(
            database.connection.execute(
                """
                SELECT COUNT(*)
                FROM fixtures
                JOIN gameweeks ON gameweeks.id = fixtures.gameweek_id
                JOIN seasons ON seasons.id = fixtures.season_id
                WHERE seasons.code = ? AND gameweeks.number = ?
                """,
                (run["season_code"], target_gameweek),
            ).fetchone()[0]
        )
        for _, target_gameweek in evaluated_origin_targets
    )
    expected_minutes_per_match = (
        sum(float(row["expected_minutes"]) for row in rows) / match_count
        if match_count
        else 0.0
    )
    actual_minutes_per_match = (
        sum(float(row["actual_minutes"]) for row in rows) / match_count
        if match_count
        else 0.0
    )
    model_config_values = json.loads(run["model_config_json"])
    if (
        run["model_version"] == LEGACY_MODEL_VERSION
        and "minutes_model" not in model_config_values
    ):
        model_config_values["minutes_model"] = "legacy"
        model_config_values["enforce_team_minutes"] = False
    return BacktestReport(
        backtest_run_id=run_id,
        season_code=run["season_code"],
        model_version=run["model_version"],
        evidence_policy=run["evidence_policy"],
        origin_gameweek_start=run["origin_gameweek_start"],
        origin_gameweek_end=run["origin_gameweek_end"],
        horizon_gameweeks=run["horizon_gameweeks"],
        generated_prediction_count=run["generated_prediction_count"],
        prediction_count=run["prediction_count"],
        missing_outcome_count=run["missing_outcome_count"],
        source_ingestion_run_id=run["source_ingestion_run_id"],
        data_fingerprint=run["data_fingerprint"],
        overall=overall,
        by_position=by_position,
        by_horizon=by_horizon,
        by_participation=by_participation,
        by_fixture_count=by_fixture_count,
        top_n=top_n,
        expected_minutes_per_match=round(
            expected_minutes_per_match, 4
        ),
        actual_minutes_per_match=round(actual_minutes_per_match, 4),
        regulation_minutes_per_match=1980.0,
        limitations=tuple(json.loads(run["limitations_json"])),
        model_config=ProjectionModelConfig(**model_config_values),
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
    deadline_time: str | None,
    *,
    fallback: datetime,
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
        "Predictions without an explicit player-fixture outcome row are "
        "excluded from scoring and reported as missing outcomes.",
        "Team-strength estimates use only the target season, so early-season "
        "forecasts regress heavily toward a common league-average prior.",
    )
    if evidence_policy == "pre_deadline_only":
        return (
            *common,
            "Fixture assignments, kickoff times and completed results are "
            "replayed from the latest fixture observation ingested before "
            "each forecast origin.",
            "Availability uses only exact live_pre_deadline observations whose "
            "capture time precedes the recorded deadline.",
        )
    return (
        *common,
        "The final reconstructed fixture slate is used because the historical "
        "source has no timestamped schedule archive; later reschedules may "
        "therefore be known to the replay.",
        "Historical reconstructed snapshots have unknown capture times, so "
        "status and chance-of-playing fields are ignored.",
        "Historical team membership is used to attach the player to fixtures; "
        "historical price is not a projection input.",
    )


def _data_fingerprint(
    database: HistoricalDatabase,
    season_code: str,
    *,
    maximum_ingestion_run_id: int | None,
) -> str:
    """Hash the persisted evidence revision evaluated by a backtest run."""

    digest = hashlib.sha256()
    digest.update(season_code.encode("utf-8"))
    digest.update(str(maximum_ingestion_run_id).encode("ascii"))
    queries = (
        (
            """
            SELECT id, source_name, identifier_namespace, retrieved_at,
                   content_sha256, source_revision, adapter_version, row_count
            FROM ingestion_runs
            WHERE status = 'completed' AND (? IS NULL OR id <= ?)
            ORDER BY id
            """,
            (maximum_ingestion_run_id, maximum_ingestion_run_id),
        ),
        (
            """
            SELECT gameweeks.number, gameweeks.deadline_time,
                   gameweeks.is_finished
            FROM gameweeks
            JOIN seasons ON seasons.id = gameweeks.season_id
            WHERE seasons.code = ?
            ORDER BY gameweeks.number
            """,
            (season_code,),
        ),
        (
            """
            SELECT fixtures.source_fixture_id, observations.gameweek_id,
                   observations.kickoff_time, observations.home_score,
                   observations.away_score, observations.finished,
                   observations.provenance_run_id
            FROM fixture_observations observations
            JOIN fixtures ON fixtures.id = observations.fixture_id
            JOIN seasons ON seasons.id = fixtures.season_id
            WHERE seasons.code = ?
              AND (? IS NULL OR observations.provenance_run_id <= ?)
            ORDER BY fixtures.source_fixture_id,
                     observations.provenance_run_id, observations.id
            """,
            (
                season_code,
                maximum_ingestion_run_id,
                maximum_ingestion_run_id,
            ),
        ),
        (
            """
            SELECT player_seasons.source_player_id, gameweeks.number,
                   observations.observation_kind,
                   observations.timing_quality, observations.observed_at,
                   observations.observed_on, teams.source_team_id,
                   observations.price_tenths, observations.status,
                   observations.chance_of_playing_next_round,
                   observations.source_observation_key,
                   observations.provenance_run_id
            FROM player_gameweek_observations observations
            JOIN player_seasons
              ON player_seasons.id = observations.player_season_id
            JOIN gameweeks ON gameweeks.id = observations.gameweek_id
            JOIN seasons ON seasons.id = player_seasons.season_id
            LEFT JOIN teams ON teams.id = observations.team_id
            WHERE seasons.code = ?
              AND (? IS NULL OR observations.provenance_run_id <= ?)
            ORDER BY player_seasons.source_player_id, gameweeks.number,
                     observations.id
            """,
            (
                season_code,
                maximum_ingestion_run_id,
                maximum_ingestion_run_id,
            ),
        ),
        (
            """
            SELECT seasons.code, player_seasons.source_player_id,
                   fixtures.source_fixture_id, stats.minutes, stats.starts,
                   stats.goals, stats.assists, stats.clean_sheet, stats.saves,
                   stats.bonus, stats.defensive_contributions,
                   stats.yellow_cards, stats.red_cards, stats.own_goals,
                   stats.total_points, stats.provenance_run_id
            FROM player_fixture_stats stats
            JOIN player_seasons
              ON player_seasons.id = stats.player_season_id
            JOIN seasons ON seasons.id = player_seasons.season_id
            JOIN fixtures ON fixtures.id = stats.fixture_id
            WHERE ? IS NULL OR stats.provenance_run_id <= ?
            ORDER BY seasons.code, player_seasons.source_player_id,
                     fixtures.source_fixture_id
            """,
            (maximum_ingestion_run_id, maximum_ingestion_run_id),
        ),
    )
    for sql, parameters in queries:
        for row in database.connection.execute(sql, parameters):
            digest.update(
                json.dumps(tuple(row), separators=(",", ":"), default=str).encode(
                    "utf-8"
                )
            )
            digest.update(b"\n")
    return digest.hexdigest()
