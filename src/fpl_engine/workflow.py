"""Two-pass weekly decision workflow and immutable audit records."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from .history.database import HistoricalDatabase

DecisionMode = Literal["provisional", "final"]
ReviewStatus = Literal["pending", "accepted", "rejected"]


class WorkflowError(ValueError):
    """Raised when a weekly workflow transition is invalid."""


@dataclass(frozen=True)
class WeeklyDecisionRun:
    run_id: int
    mode: DecisionMode
    season_code: str
    gameweek_number: int
    created_at: datetime
    frozen_at: datetime | None
    recommendation: dict[str, Any]
    decision_triggers: tuple[str, ...]
    overrides: tuple[dict[str, Any], ...]


class WeeklyWorkflowRepository:
    def __init__(self, database: HistoricalDatabase) -> None:
        self.database = database

    def add_news_evidence(
        self,
        *,
        season_code: str,
        gameweek_number: int,
        evidence_type: str,
        summary: str,
        confidence: Literal["low", "medium", "high"],
        evidence_at: datetime,
        source_player_id: str | None = None,
        source_url: str | None = None,
        schema_version: int = 1,
        source_name: str | None = None,
        source_tier: str | None = None,
        model_area: str | None = None,
        suggested_adjustment: dict[str, Any] | None = None,
        adjustment_basis: str | None = None,
        requires_decision: bool = True,
        decision_question: str | None = None,
        expires_at: datetime | None = None,
        prompt_version: str | None = None,
        research_run_id: str | None = None,
    ) -> int:
        if evidence_at.tzinfo is None:
            raise WorkflowError("Evidence time must be timezone-aware")
        if expires_at is not None and expires_at.tzinfo is None:
            raise WorkflowError("Evidence expiry must be timezone-aware")
        if expires_at is not None and expires_at <= evidence_at:
            raise WorkflowError("Evidence expiry must be after its publication time")
        if schema_version not in {1, 2}:
            raise WorkflowError("Unsupported news evidence schema version")
        season_id, gameweek_id = self._season_gameweek(
            season_code, gameweek_number
        )
        player_season_id = None
        if source_player_id is not None:
            row = self.database.connection.execute(
                """
                SELECT id FROM player_seasons
                WHERE season_id = ? AND identifier_namespace = 'official-fpl'
                  AND source_player_id = ?
                """,
                (season_id, source_player_id),
            ).fetchone()
            if row is None:
                raise WorkflowError(
                    f"Player {source_player_id!r} is not available"
                )
            player_season_id = int(row["id"])
        cursor = self.database.connection.execute(
            """
            INSERT INTO news_evidence (
                season_id, gameweek_id, player_season_id, evidence_type,
                summary, source_url, evidence_at, confidence, review_status,
                schema_version, source_name, published_at, source_tier,
                model_area, suggested_adjustment_json, adjustment_basis,
                requires_decision, decision_question, expires_at,
                prompt_version, research_run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?,
                      ?, ?, ?, ?, ?, ?)
            RETURNING id
            """,
            (
                season_id,
                gameweek_id,
                player_season_id,
                evidence_type,
                summary,
                source_url,
                evidence_at.astimezone(UTC).isoformat(),
                confidence,
                schema_version,
                source_name,
                evidence_at.astimezone(UTC).isoformat(),
                source_tier,
                model_area,
                (
                    None
                    if suggested_adjustment is None
                    else _json(suggested_adjustment)
                ),
                adjustment_basis,
                int(requires_decision),
                decision_question,
                (
                    None
                    if expires_at is None
                    else expires_at.astimezone(UTC).isoformat()
                ),
                prompt_version,
                research_run_id,
            ),
        )
        evidence_id = int(cursor.fetchone()[0])
        self.database.connection.commit()
        return evidence_id

    def review_evidence(
        self,
        evidence_id: int,
        *,
        status: Literal["accepted", "rejected"],
        rationale: str,
        expected_minutes_adjustment: float | None = None,
        decision_maker: str = "user",
        reviewed_at: datetime | None = None,
    ) -> None:
        if not rationale.strip():
            raise WorkflowError("A review rationale is required")
        if not decision_maker.strip():
            raise WorkflowError("A decision maker is required")
        evidence = self.database.connection.execute(
            """
            SELECT review_status, suggested_adjustment_json
            FROM news_evidence WHERE id = ?
            """,
            (evidence_id,),
        ).fetchone()
        if evidence is None or evidence["review_status"] != "pending":
            raise WorkflowError("Evidence is missing or has already been reviewed")
        suggested = (
            None
            if evidence["suggested_adjustment_json"] is None
            else json.loads(evidence["suggested_adjustment_json"])
        )
        if (
            status == "accepted"
            and expected_minutes_adjustment is None
            and suggested is not None
            and suggested.get("kind") == "expected_minutes_delta"
        ):
            expected_minutes_adjustment = float(suggested["value"])
        if (
            expected_minutes_adjustment is not None
            and not -90 <= expected_minutes_adjustment <= 90
        ):
            raise WorkflowError(
                "Expected-minutes adjustment must be between -90 and 90"
            )
        reviewed = reviewed_at or datetime.now(UTC)
        if reviewed.tzinfo is None:
            raise WorkflowError("Review time must be timezone-aware")
        cursor = self.database.connection.execute(
            """
            UPDATE news_evidence
            SET review_status = ?, expected_minutes_adjustment = ?, rationale = ?,
                reviewed_at = ?, decision_maker = ?, proposed_value = ?,
                accepted_value = ?
            WHERE id = ? AND review_status = 'pending'
            """,
            (
                status,
                (
                    expected_minutes_adjustment
                    if status == "accepted"
                    else None
                ),
                rationale.strip(),
                reviewed.astimezone(UTC).isoformat(),
                decision_maker.strip(),
                expected_minutes_adjustment,
                (
                    expected_minutes_adjustment
                    if status == "accepted"
                    else None
                ),
                evidence_id,
            ),
        )
        if cursor.rowcount != 1:
            self.database.connection.rollback()
            raise WorkflowError("Evidence is missing or has already been reviewed")
        self.database.connection.commit()

    def create_decision_run(
        self,
        *,
        manager_snapshot_id: int,
        projection_run_id: int,
        mode: DecisionMode,
        recommendation: dict[str, Any],
        decision_triggers: tuple[str, ...],
        overrides: tuple[dict[str, Any], ...] = (),
        created_at: datetime | None = None,
    ) -> int:
        created = created_at or datetime.now(UTC)
        if created.tzinfo is None:
            raise WorkflowError("Decision-run time must be timezone-aware")
        if "expected_points" not in recommendation:
            raise WorkflowError(
                "Recommendation must record its expected_points baseline"
            )
        context = self.database.connection.execute(
            """
            SELECT manager.season_id, manager.gameweek_id,
                   gameweeks.number AS gameweek_number,
                   projections.season_id AS projection_season_id,
                   projections.start_gameweek
            FROM manager_snapshots manager
            JOIN gameweeks ON gameweeks.id = manager.gameweek_id
            JOIN projection_runs projections ON projections.id = ?
            WHERE manager.id = ?
            """,
            (projection_run_id, manager_snapshot_id),
        ).fetchone()
        if context is None:
            raise WorkflowError("Manager snapshot or projection run is missing")
        if (
            context["season_id"] != context["projection_season_id"]
            or context["gameweek_number"] != context["start_gameweek"]
        ):
            raise WorkflowError(
                "Manager snapshot and projection run must cover the same Gameweek"
            )
        timestamp = created.astimezone(UTC).isoformat()
        if mode == "final":
            pending = self.database.connection.execute(
                """
                SELECT COUNT(*) FROM news_evidence
                WHERE season_id = ? AND gameweek_id = ?
                  AND review_status = 'pending'
                """,
                (context["season_id"], context["gameweek_id"]),
            ).fetchone()[0]
            if pending:
                raise WorkflowError(
                    f"Cannot freeze final run with {pending} pending evidence item(s)"
                )
            accepted_ids = {
                int(row["id"])
                for row in self.database.connection.execute(
                    """
                    SELECT id FROM news_evidence
                    WHERE season_id = ? AND gameweek_id = ?
                      AND review_status = 'accepted'
                      AND expected_minutes_adjustment IS NOT NULL
                      AND evidence_at <= ?
                      AND (expires_at IS NULL OR expires_at > ?)
                    """,
                    (
                        context["season_id"],
                        context["gameweek_id"],
                        timestamp,
                        timestamp,
                    ),
                )
            }
            if accepted_ids:
                pair = self.database.connection.execute(
                    """
                    SELECT evidence_ids_json
                    FROM news_projection_pairs
                    WHERE post_news_projection_run_id = ?
                    """,
                    (projection_run_id,),
                ).fetchone()
                paired_ids = (
                    set()
                    if pair is None
                    else {int(value) for value in json.loads(pair[0])}
                )
                if not accepted_ids.issubset(paired_ids):
                    raise WorkflowError(
                        "Final run must use a post-news projection containing "
                        "all current accepted adjustments"
                    )
        cursor = self.database.connection.execute(
            """
            INSERT INTO weekly_decision_runs (
                season_id, gameweek_id, manager_snapshot_id, projection_run_id,
                mode, created_at, frozen_at, recommendation_json,
                decision_triggers_json, overrides_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING id
            """,
            (
                context["season_id"],
                context["gameweek_id"],
                manager_snapshot_id,
                projection_run_id,
                mode,
                timestamp,
                timestamp if mode == "final" else None,
                _json(recommendation),
                _json(decision_triggers),
                _json(overrides),
            ),
        )
        run_id = int(cursor.fetchone()[0])
        self.database.connection.commit()
        return run_id

    def record_actual_action(
        self,
        weekly_run_id: int,
        *,
        action: dict[str, Any],
        followed_recommendation: bool,
        deviation_reason: str | None = None,
        recorded_at: datetime | None = None,
    ) -> int:
        if not followed_recommendation and not (deviation_reason or "").strip():
            raise WorkflowError(
                "A deviation reason is required when the recommendation was not followed"
            )
        mode = self.database.connection.execute(
            "SELECT mode FROM weekly_decision_runs WHERE id = ?",
            (weekly_run_id,),
        ).fetchone()
        if mode is None or mode["mode"] != "final":
            raise WorkflowError("Actual actions must attach to a frozen final run")
        recorded = recorded_at or datetime.now(UTC)
        cursor = self.database.connection.execute(
            """
            INSERT INTO actual_actions (
                weekly_decision_run_id, recorded_at, action_json,
                followed_recommendation, deviation_reason
            ) VALUES (?, ?, ?, ?, ?)
            RETURNING id
            """,
            (
                weekly_run_id,
                recorded.astimezone(UTC).isoformat(),
                _json(action),
                int(followed_recommendation),
                None if followed_recommendation else deviation_reason,
            ),
        )
        action_id = int(cursor.fetchone()[0])
        self.database.connection.commit()
        return action_id

    def evaluate(
        self,
        weekly_run_id: int,
        *,
        realised_points: float,
        review_notes: str | None = None,
        evaluated_at: datetime | None = None,
    ) -> int:
        row = self.database.connection.execute(
            """
            SELECT recommendation_json,
                   EXISTS (
                       SELECT 1 FROM actual_actions
                       WHERE weekly_decision_run_id = weekly_decision_runs.id
                   ) AS has_action
            FROM weekly_decision_runs WHERE id = ? AND mode = 'final'
            """,
            (weekly_run_id,),
        ).fetchone()
        if row is None or not row["has_action"]:
            raise WorkflowError(
                "Evaluation requires a final run with its actual action recorded"
            )
        forecast = float(json.loads(row["recommendation_json"])["expected_points"])
        evaluated = evaluated_at or datetime.now(UTC)
        cursor = self.database.connection.execute(
            """
            INSERT INTO weekly_evaluations (
                weekly_decision_run_id, evaluated_at, forecast_points,
                realised_points, score_error, review_notes
            ) VALUES (?, ?, ?, ?, ?, ?)
            RETURNING id
            """,
            (
                weekly_run_id,
                evaluated.astimezone(UTC).isoformat(),
                forecast,
                realised_points,
                realised_points - forecast,
                review_notes,
            ),
        )
        evaluation_id = int(cursor.fetchone()[0])
        self.database.connection.commit()
        return evaluation_id

    def load(self, run_id: int) -> WeeklyDecisionRun:
        row = self.database.connection.execute(
            """
            SELECT weekly_decision_runs.*, seasons.code AS season_code,
                   gameweeks.number AS gameweek_number
            FROM weekly_decision_runs
            JOIN seasons ON seasons.id = weekly_decision_runs.season_id
            JOIN gameweeks ON gameweeks.id = weekly_decision_runs.gameweek_id
            WHERE weekly_decision_runs.id = ?
            """,
            (run_id,),
        ).fetchone()
        if row is None:
            raise WorkflowError(f"Weekly decision run {run_id} is missing")
        return WeeklyDecisionRun(
            run_id=run_id,
            mode=row["mode"],
            season_code=row["season_code"],
            gameweek_number=row["gameweek_number"],
            created_at=datetime.fromisoformat(row["created_at"]),
            frozen_at=(
                None
                if row["frozen_at"] is None
                else datetime.fromisoformat(row["frozen_at"])
            ),
            recommendation=json.loads(row["recommendation_json"]),
            decision_triggers=tuple(json.loads(row["decision_triggers_json"])),
            overrides=tuple(json.loads(row["overrides_json"])),
        )

    def _season_gameweek(
        self, season_code: str, gameweek_number: int
    ) -> tuple[int, int]:
        row = self.database.connection.execute(
            """
            SELECT seasons.id AS season_id, gameweeks.id AS gameweek_id
            FROM seasons
            JOIN gameweeks ON gameweeks.season_id = seasons.id
            WHERE seasons.code = ? AND gameweeks.number = ?
            """,
            (season_code, gameweek_number),
        ).fetchone()
        if row is None:
            raise WorkflowError(
                f"{season_code} Gameweek {gameweek_number} is unavailable"
            )
        return int(row["season_id"]), int(row["gameweek_id"])


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))
