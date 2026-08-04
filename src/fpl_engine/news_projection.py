"""Create and score paired projections before and after reviewed team news."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from .config import SeasonRules
from .history.database import HistoricalDatabase
from .projections import (
    DEFAULT_MODEL_CONFIG,
    MODEL_VERSION,
    ProjectionModelConfig,
    ProjectionOverride,
    RatesProjectionModel,
)
from .workflow import WorkflowError


@dataclass(frozen=True)
class NewsProjectionPair:
    pair_id: int
    pre_news_projection_run_id: int
    post_news_projection_run_id: int
    evidence_ids: tuple[int, ...]
    overrides: tuple[ProjectionOverride, ...]


@dataclass(frozen=True)
class NewsProjectionEvaluation:
    evaluation_id: int
    pair_id: int
    sample_count: int
    pre_news_points_mae: float
    post_news_points_mae: float
    pre_news_minutes_mae: float
    post_news_minutes_mae: float
    points_mae_change: float
    minutes_mae_change: float


def create_news_projection_pair(
    database: HistoricalDatabase,
    rules: SeasonRules,
    *,
    season_code: str,
    gameweek_number: int,
    horizon_gameweeks: int | None = None,
    config: ProjectionModelConfig = DEFAULT_MODEL_CONFIG,
    generated_at: datetime | None = None,
    observation_mode: str = "latest_available",
    input_package_id: str | None = None,
    research_run_id: str | None = None,
    pre_news_projection_run_id: int | None = None,
) -> NewsProjectionPair:
    """Generate comparable pre/post-news runs from one data state."""

    generated = generated_at or datetime.now(UTC)
    if generated.tzinfo is None:
        raise WorkflowError("Projection-pair time must be timezone-aware")
    context = database.connection.execute(
        """
        SELECT seasons.id AS season_id, gameweeks.id AS gameweek_id
        FROM seasons
        JOIN gameweeks ON gameweeks.season_id = seasons.id
        WHERE seasons.code = ? AND gameweeks.number = ?
        """,
        (season_code, gameweek_number),
    ).fetchone()
    if context is None:
        raise WorkflowError(f"{season_code} Gameweek {gameweek_number} is unavailable")
    pending = database.connection.execute(
        """
        SELECT COUNT(*) FROM news_evidence
        WHERE season_id = ? AND gameweek_id = ? AND review_status = 'pending'
        """,
        (context["season_id"], context["gameweek_id"]),
    ).fetchone()[0]
    if pending:
        raise WorkflowError(
            f"Cannot generate final news projections with {pending} pending evidence item(s)"
        )
    effective_horizon = 1 if horizon_gameweeks is None else horizon_gameweeks
    if effective_horizon <= 0:
        raise WorkflowError("Projection-pair horizon must be positive")
    evidence = database.connection.execute(
        """
        SELECT news_evidence.id, news_evidence.expected_minutes_adjustment,
               news_evidence.rationale, news_evidence.summary,
               news_evidence.expires_at, player_seasons.source_player_id
        FROM news_evidence
        JOIN player_seasons
          ON player_seasons.id = news_evidence.player_season_id
        WHERE news_evidence.season_id = ?
          AND news_evidence.gameweek_id = ?
          AND news_evidence.review_status = 'accepted'
          AND news_evidence.expected_minutes_adjustment IS NOT NULL
          AND (
              news_evidence.temporal_status IS NULL
              OR news_evidence.temporal_status = 'current_window'
          )
          AND news_evidence.evidence_at <= ?
          AND (
              news_evidence.expires_at IS NULL
              OR news_evidence.expires_at > ?
          )
        ORDER BY news_evidence.id
        """,
        (
            context["season_id"],
            context["gameweek_id"],
            generated.astimezone(UTC).isoformat(),
            generated.astimezone(UTC).isoformat(),
        ),
    ).fetchall()
    pre_source = None
    if pre_news_projection_run_id is None:
        pre_news = RatesProjectionModel(
            database,
            rules,
            config=config,
            model_version=f"{MODEL_VERSION}-pre-news",
        ).project(
            season_code=season_code,
            start_gameweek=gameweek_number,
            horizon_gameweeks=effective_horizon,
            generated_at=generated,
            observation_mode=observation_mode,
        )
        pre_projection_id = pre_news.projection_run_id
        expected_minutes = {
            projection.source_player_id: projection.expected_minutes
            for projection in pre_news.projections
            if projection.gameweek_number == gameweek_number
        }
    else:
        pre_news = None
        pre_row = database.connection.execute(
            """
            SELECT id, season_id, start_gameweek, horizon_gameweeks,
                   source_ingestion_run_id
            FROM projection_runs WHERE id = ?
            """,
            (pre_news_projection_run_id,),
        ).fetchone()
        if (
            pre_row is None
            or pre_row["season_id"] != context["season_id"]
            or pre_row["start_gameweek"] != gameweek_number
        ):
            raise WorkflowError("The supplied pre-news projection does not cover this Gameweek")
        stored_horizon = int(pre_row["horizon_gameweeks"])
        if horizon_gameweeks is not None and horizon_gameweeks != stored_horizon:
            raise WorkflowError(
                "The supplied pre-news projection horizon does not match the requested horizon"
            )
        effective_horizon = stored_horizon
        pre_projection_id = int(pre_row["id"])
        pre_source = pre_row["source_ingestion_run_id"]
        expected_minutes = {
            row["source_player_id"]: float(row["expected_minutes"])
            for row in database.connection.execute(
                """
                SELECT ps.source_player_id, projections.expected_minutes
                FROM player_gameweek_projections projections
                JOIN player_seasons ps ON ps.id = projections.player_season_id
                WHERE projections.projection_run_id = ? AND projections.gameweek_number = ?
                """,
                (pre_projection_id, gameweek_number),
            ).fetchall()
        }
    updates: list[tuple[float, float, float, int]] = []
    rationale: dict[str, list[str]] = {}
    evidence_ids: list[int] = []
    for item in evidence:
        player_id = item["source_player_id"]
        if player_id not in expected_minutes:
            raise WorkflowError(
                f"Accepted news player {player_id!r} is absent from the pre-news projection"
            )
        original = expected_minutes[player_id]
        proposed = _clamp(
            original + float(item["expected_minutes_adjustment"]),
            0.0,
            90.0,
        )
        expected_minutes[player_id] = proposed
        evidence_id = int(item["id"])
        evidence_ids.append(evidence_id)
        updates.append((original, proposed, proposed, evidence_id))
        rationale.setdefault(player_id, []).append(
            f"news:{evidence_id} {item['summary']} ({item['rationale']})"
        )

    all_current_evidence = database.connection.execute(
        """
        SELECT id FROM news_evidence
        WHERE season_id = ? AND gameweek_id = ? AND review_status = 'accepted'
          AND (temporal_status IS NULL OR temporal_status = 'current_window')
          AND evidence_at <= ? AND (expires_at IS NULL OR expires_at > ?)
        ORDER BY id
        """,
        (
            context["season_id"],
            context["gameweek_id"],
            generated.astimezone(UTC).isoformat(),
            generated.astimezone(UTC).isoformat(),
        ),
    ).fetchall()
    evidence_ids = [int(row["id"]) for row in all_current_evidence]
    if input_package_id is None and evidence:
        input_package_id = database.connection.execute(
            "SELECT input_package_id FROM news_evidence WHERE id = ?", (evidence[0]["id"],)
        ).fetchone()[0]
    if research_run_id is None and evidence:
        research_run_id = database.connection.execute(
            "SELECT research_run_id FROM news_evidence WHERE id = ?", (evidence[0]["id"],)
        ).fetchone()[0]

    overrides = tuple(
        ProjectionOverride(
            source_player_id=player_id,
            gameweek_number=gameweek_number,
            expected_minutes=expected_minutes[player_id],
            rationale="; ".join(reasons),
        )
        for player_id, reasons in sorted(rationale.items())
    )
    post_news = RatesProjectionModel(
        database,
        rules,
        config=config,
        model_version=f"{MODEL_VERSION}-post-news-v2",
    ).project(
        season_code=season_code,
        start_gameweek=gameweek_number,
        horizon_gameweeks=effective_horizon,
        overrides=overrides,
        generated_at=generated,
        observation_mode=observation_mode,
        fixture_max_ingestion_run_id=pre_source,
    )
    if pre_projection_id is None or post_news.projection_run_id is None:
        raise WorkflowError("News projection pairs must be persisted")
    with database.transaction():
        database.connection.executemany(
            """
            UPDATE news_evidence
            SET original_value = ?, proposed_value = ?, accepted_value = ?
            WHERE id = ? AND review_status = 'accepted'
            """,
            updates,
        )
        cursor = database.connection.execute(
            """
            INSERT INTO news_projection_pairs (
                season_id, gameweek_id, pre_news_projection_run_id,
                post_news_projection_run_id, created_at, evidence_ids_json,
                input_package_id, research_run_id, source_ingestion_run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING id
            """,
            (
                context["season_id"],
                context["gameweek_id"],
                pre_projection_id,
                post_news.projection_run_id,
                generated.astimezone(UTC).isoformat(),
                json.dumps(evidence_ids),
                input_package_id,
                research_run_id,
                database.connection.execute(
                    "SELECT source_ingestion_run_id FROM projection_runs WHERE id = ?",
                    (pre_projection_id,),
                ).fetchone()[0],
            ),
        )
        pair_id = int(cursor.fetchone()[0])
    return NewsProjectionPair(
        pair_id=pair_id,
        pre_news_projection_run_id=pre_projection_id,
        post_news_projection_run_id=post_news.projection_run_id,
        evidence_ids=tuple(evidence_ids),
        overrides=overrides,
    )


def evaluate_news_projection_pair(
    database: HistoricalDatabase,
    pair_id: int,
    *,
    evaluated_at: datetime | None = None,
) -> NewsProjectionEvaluation:
    """Score a paired run once the target Gameweek is complete."""

    pair = database.connection.execute(
        """
        SELECT pairs.*, gameweeks.number AS gameweek_number
        FROM news_projection_pairs pairs
        JOIN gameweeks ON gameweeks.id = pairs.gameweek_id
        WHERE pairs.id = ?
        """,
        (pair_id,),
    ).fetchone()
    if pair is None:
        raise WorkflowError(f"News projection pair {pair_id} is missing")
    unfinished = database.connection.execute(
        """
        SELECT COUNT(*) FROM fixtures
        WHERE gameweek_id = ? AND finished = 0
        """,
        (pair["gameweek_id"],),
    ).fetchone()[0]
    if unfinished:
        raise WorkflowError(
            f"Cannot evaluate news projections while {unfinished} fixture(s) are unfinished"
        )
    rows = database.connection.execute(
        """
        WITH actual AS (
            SELECT stats.player_season_id,
                   SUM(stats.minutes) AS actual_minutes,
                   SUM(stats.total_points) AS actual_points
            FROM player_fixture_stats stats
            JOIN fixtures ON fixtures.id = stats.fixture_id
            WHERE fixtures.gameweek_id = ?
            GROUP BY stats.player_season_id
        )
        SELECT pre.expected_minutes AS pre_minutes,
               post.expected_minutes AS post_minutes,
               pre.expected_points AS pre_points,
               post.expected_points AS post_points,
               COALESCE(actual.actual_minutes, 0) AS actual_minutes,
               COALESCE(actual.actual_points, 0) AS actual_points
        FROM player_gameweek_projections pre
        JOIN player_gameweek_projections post
          ON post.player_season_id = pre.player_season_id
         AND post.gameweek_number = pre.gameweek_number
        LEFT JOIN actual ON actual.player_season_id = pre.player_season_id
        WHERE pre.projection_run_id = ?
          AND post.projection_run_id = ?
          AND pre.gameweek_number = ?
        """,
        (
            pair["gameweek_id"],
            pair["pre_news_projection_run_id"],
            pair["post_news_projection_run_id"],
            pair["gameweek_number"],
        ),
    ).fetchall()
    if not rows:
        raise WorkflowError("News projection pair has no comparable predictions")
    pre_points_mae = _mae(rows, "pre_points", "actual_points")
    post_points_mae = _mae(rows, "post_points", "actual_points")
    pre_minutes_mae = _mae(rows, "pre_minutes", "actual_minutes")
    post_minutes_mae = _mae(rows, "post_minutes", "actual_minutes")
    evaluated = evaluated_at or datetime.now(UTC)
    if evaluated.tzinfo is None:
        raise WorkflowError("Evaluation time must be timezone-aware")
    with database.transaction():
        cursor = database.connection.execute(
            """
            INSERT INTO news_projection_evaluations (
                news_projection_pair_id, evaluated_at, sample_count,
                pre_news_points_mae, post_news_points_mae,
                pre_news_minutes_mae, post_news_minutes_mae,
                points_mae_change, minutes_mae_change
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING id
            """,
            (
                pair_id,
                evaluated.astimezone(UTC).isoformat(),
                len(rows),
                pre_points_mae,
                post_points_mae,
                pre_minutes_mae,
                post_minutes_mae,
                post_points_mae - pre_points_mae,
                post_minutes_mae - pre_minutes_mae,
            ),
        )
        evaluation_id = int(cursor.fetchone()[0])
    return NewsProjectionEvaluation(
        evaluation_id=evaluation_id,
        pair_id=pair_id,
        sample_count=len(rows),
        pre_news_points_mae=pre_points_mae,
        post_news_points_mae=post_points_mae,
        pre_news_minutes_mae=pre_minutes_mae,
        post_news_minutes_mae=post_minutes_mae,
        points_mae_change=round(post_points_mae - pre_points_mae, 4),
        minutes_mae_change=round(post_minutes_mae - pre_minutes_mae, 4),
    )


def _mae(rows: list, expected: str, actual: str) -> float:
    return round(
        sum(abs(float(row[actual]) - float(row[expected])) for row in rows) / len(rows),
        4,
    )


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return min(maximum, max(minimum, value))
