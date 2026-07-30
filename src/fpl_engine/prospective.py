"""Prospective evidence completeness checks for a live FPL season."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from .history.database import HistoricalDatabase


def build_prospective_capture_status(
    database: HistoricalDatabase,
    season_code: str,
    *,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    """Report unrecoverable live-evidence gaps by Gameweek."""

    checked_at = as_of or datetime.now(UTC)
    if checked_at.tzinfo is None:
        raise ValueError("Prospective status time must be timezone-aware")
    season = database.connection.execute(
        "SELECT id, name FROM seasons WHERE code = ?",
        (season_code,),
    ).fetchone()
    if season is None:
        raise ValueError(f"Season {season_code!r} is unavailable")
    gameweeks = database.connection.execute(
        """
        SELECT id, number, deadline_time, is_finished
        FROM gameweeks
        WHERE season_id = ?
        ORDER BY number
        """,
        (int(season["id"]),),
    ).fetchall()
    candidates = database.connection.execute(
        """
        SELECT candidate_key, model_version, model_config_sha256
        FROM model_candidate_registrations
        WHERE season_id = ? AND status = 'declared'
        ORDER BY candidate_key
        """,
        (int(season["id"]),),
    ).fetchall()
    results = []
    for gameweek in gameweeks:
        gameweek_id = int(gameweek["id"])
        deadline = _parse_time(gameweek["deadline_time"])
        deadline_passed = (
            deadline is not None
            and checked_at.astimezone(UTC) >= deadline.astimezone(UTC)
        )
        counts = _capture_counts(database, gameweek_id)
        candidate_capture = _candidate_capture(
            database,
            season_id=int(season["id"]),
            gameweek_id=gameweek_id,
            gameweek_number=int(gameweek["number"]),
            deadline_time=gameweek["deadline_time"],
            candidates=candidates,
        )
        required = []
        if deadline_passed:
            required.extend(
                (
                    "pre_deadline_snapshot",
                    "manager_snapshot",
                    "paired_news_projections",
                    "final_decision",
                    "actual_action",
                )
            )
            required.extend(
                f"candidate_projection:{row['candidate_key']}"
                for row in candidates
            )
        if bool(gameweek["is_finished"]):
            required.extend(
                (
                    "recorded_outcomes",
                    "weekly_evaluation",
                )
            )
        missing = []
        for item in required:
            if item.startswith("candidate_projection:"):
                key = item.split(":", 1)[1]
                if candidate_capture[key]["valid_pre_deadline_runs"] == 0:
                    missing.append(item)
            elif int(counts[item]) == 0:
                missing.append(item)
        results.append(
            {
                "gameweek": int(gameweek["number"]),
                "deadline_time": gameweek["deadline_time"],
                "deadline_passed": deadline_passed,
                "is_finished": bool(gameweek["is_finished"]),
                "status": (
                    "upcoming"
                    if not required
                    else "complete" if not missing else "incomplete"
                ),
                "counts": counts,
                "candidate_projections": candidate_capture,
                "missing_required": missing,
            }
        )
    completed = [
        gameweek for gameweek in results if gameweek["status"] == "complete"
    ]
    incomplete = [
        gameweek
        for gameweek in results
        if gameweek["status"] == "incomplete"
    ]
    return {
        "season_code": season_code,
        "season_name": str(season["name"]),
        "checked_at": checked_at.astimezone(UTC).isoformat(),
        "gameweeks": results,
        "summary": {
            "complete_gameweeks": len(completed),
            "incomplete_gameweeks": len(incomplete),
            "upcoming_gameweeks": sum(
                gameweek["status"] == "upcoming"
                for gameweek in results
            ),
            "no_missed_required_evidence_to_date": not incomplete,
            "promotion_grade_capture_complete": (
                bool(results)
                and all(gameweek["is_finished"] for gameweek in results)
                and not incomplete
            ),
        },
        "policy": [
            "A passed deadline requires an exact pre-deadline official snapshot, "
            "manager state, paired pre/post-news projections, a frozen decision "
            "and the actual action.",
            "A finished Gameweek additionally requires a completed-fixture capture "
            "with cumulative player outcomes and decision evaluation.",
            "Every declared forward candidate requires a pre-deadline projection "
            "whose model version and canonical config match its immutable registration.",
        ],
    }


def _candidate_capture(
    database: HistoricalDatabase,
    *,
    season_id: int,
    gameweek_id: int,
    gameweek_number: int,
    deadline_time: object,
    candidates: list[Any],
) -> dict[str, dict[str, int | str]]:
    if not candidates:
        return {}
    runs = database.connection.execute(
        """
        SELECT projection_runs.model_version,
               projection_runs.assumptions_json
        FROM projection_runs
        WHERE projection_runs.season_id = ?
          AND projection_runs.start_gameweek = ?
          AND datetime(projection_runs.generated_at) < datetime(?)
          AND EXISTS (
              SELECT 1
              FROM player_gameweek_observations observations
              WHERE observations.gameweek_id = ?
                AND observations.provenance_run_id =
                    projection_runs.source_ingestion_run_id
                AND observations.observation_kind = 'live_pre_deadline'
                AND observations.timing_quality = 'exact'
                AND datetime(observations.observed_at) < datetime(?)
          )
        """,
        (
            season_id,
            gameweek_number,
            deadline_time,
            gameweek_id,
            deadline_time,
        ),
    ).fetchall()
    result = {}
    for candidate in candidates:
        valid = 0
        version_runs = 0
        for run in runs:
            if run["model_version"] != candidate["model_version"]:
                continue
            version_runs += 1
            assumptions = json.loads(run["assumptions_json"])
            config_json = json.dumps(
                assumptions.get("model_config", {}),
                sort_keys=True,
                separators=(",", ":"),
            )
            digest = hashlib.sha256(
                config_json.encode("utf-8")
            ).hexdigest()
            valid += int(digest == candidate["model_config_sha256"])
        result[str(candidate["candidate_key"])] = {
            "model_version": str(candidate["model_version"]),
            "version_matched_runs": version_runs,
            "valid_pre_deadline_runs": valid,
        }
    return result


def _capture_counts(
    database: HistoricalDatabase,
    gameweek_id: int,
) -> dict[str, int]:
    row = database.connection.execute(
        """
        SELECT
            (
                SELECT COUNT(DISTINCT provenance_run_id)
                FROM player_gameweek_observations
                WHERE gameweek_id = ?
                  AND observation_kind = 'live_pre_deadline'
                  AND timing_quality = 'exact'
            ) AS pre_deadline_snapshot,
            (
                SELECT COUNT(DISTINCT provenance_run_id)
                FROM player_gameweek_observations
                WHERE gameweek_id = ?
                  AND observation_kind = 'post_gameweek'
                  AND timing_quality = 'exact'
            ) AS post_gameweek_snapshot,
            (
                SELECT COUNT(*) FROM manager_snapshots
                WHERE gameweek_id = ?
            ) AS manager_snapshot,
            (
                SELECT COUNT(*) FROM news_projection_pairs
                WHERE gameweek_id = ?
            ) AS paired_news_projections,
            (
                SELECT COUNT(*) FROM weekly_decision_runs
                WHERE gameweek_id = ? AND mode = 'final'
            ) AS final_decision,
            (
                SELECT COUNT(*)
                FROM actual_actions actions
                JOIN weekly_decision_runs runs
                  ON runs.id = actions.weekly_decision_run_id
                WHERE runs.gameweek_id = ? AND runs.mode = 'final'
            ) AS actual_action,
            (
                SELECT COUNT(*)
                FROM weekly_evaluations evaluations
                JOIN weekly_decision_runs runs
                  ON runs.id = evaluations.weekly_decision_run_id
                WHERE runs.gameweek_id = ? AND runs.mode = 'final'
            ) AS weekly_evaluation,
            (
                SELECT COUNT(*)
                FROM player_season_stats_observations stats
                WHERE stats.provenance_run_id = (
                    SELECT observations.provenance_run_id
                    FROM fixture_observations observations
                    JOIN fixtures
                      ON fixtures.id = observations.fixture_id
                    WHERE fixtures.gameweek_id = ?
                    GROUP BY observations.provenance_run_id
                    HAVING MIN(observations.finished) = 1
                       AND COUNT(*) = (
                           SELECT COUNT(*) FROM fixtures expected
                           WHERE expected.gameweek_id = ?
                       )
                    ORDER BY observations.provenance_run_id
                    LIMIT 1
                )
            ) AS recorded_outcomes
        """,
        (
            gameweek_id,
            gameweek_id,
            gameweek_id,
            gameweek_id,
            gameweek_id,
            gameweek_id,
            gameweek_id,
            gameweek_id,
            gameweek_id,
        ),
    ).fetchone()
    return {
        name: int(value)
        for name, value in dict(row).items()
    }


def _parse_time(value: object) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"Deadline {value!r} is not timezone-aware")
    return parsed
