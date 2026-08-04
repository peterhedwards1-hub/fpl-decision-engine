"""Decision-focused team-news v3 packages and imports.

This module deliberately keeps research execution outside the application.  It
exports a bounded, auditable input package and imports the strict JSON returned
by a manually-run research chat.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .history.database import HistoricalDatabase

PROMPT_VERSION = "fpl-team-news-v3"
SCHEMA_VERSION = 3
RESEARCH_MODES = {"preseason", "provisional", "final"}
PRIORITIES = {"critical", "starting_xi", "bench_cover", "squad", "alternative", "broad_scan"}
COVERAGE_STATUSES = {
    "checked_material_evidence",
    "checked_no_material_evidence",
    "partially_checked",
    "source_unavailable",
    "identity_unresolved",
    "not_checked",
}
SOURCE_TIERS = {"official", "strong_reporting", "predicted_lineup", "rumour"}
EVIDENCE_TYPES = {
    "injury",
    "suspension",
    "training",
    "manager_quote",
    "predicted_lineup",
    "tactical_role",
    "transfer",
    "set_piece",
    "team_disruption",
    "other",
}
MODEL_AREAS = {
    "expected_minutes",
    "appearance_probability",
    "starting_probability",
    "sixty_probability",
    "availability",
    "return_date",
    "penalties",
    "corners",
    "direct_free_kicks",
    "tactical_role",
    "attacking_position",
    "team_attack",
    "team_defence",
    "fixture_status",
    "informational",
}
ADJUSTMENT_SUPPORT = {"supported_numeric", "structured_flag", "informational", "unsupported"}
ADJUSTMENT_KINDS = {
    "expected_minutes_delta",
    "appearance_probability_delta",
    "starting_probability_delta",
    "sixty_probability_delta",
    "availability_flag",
    "return_date",
    "penalty_taker",
    "corner_taker",
    "free_kick_taker",
    "tactical_role",
    "attacking_position",
    "team_attack",
    "team_defence",
    "fixture_status",
    "informational",
}


def _timestamp(value: str | datetime, field: str, *, allow_none: bool = False) -> str | None:
    if value is None and allow_none:
        return None
    parsed = (
        value
        if isinstance(value, datetime)
        else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    )
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.astimezone(UTC).isoformat()


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def compute_package_hash(package: dict[str, Any]) -> str:
    """Hash a package independently of its echoed hash field."""

    body = {key: value for key, value in package.items() if key != "input_package_hash"}
    return hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()


def _row_assumptions(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return [str(raw)]
    if isinstance(value, dict):
        return [f"{key}={value[key]}" for key in sorted(value)]
    return [str(value)]


def _questions(player: dict[str, Any]) -> list[str]:
    questions: list[str] = []
    if player["is_starting_xi"]:
        questions.append("Is the player expected to start?")
    if player["official_chance_of_playing"] is not None or player[
        "current_official_status"
    ] not in (None, "a"):
        questions.extend(("Is the player fully fit?", "Is the player training normally?"))
    if player["is_starting_xi"] or player["priority"] == "alternative":
        questions.append("Has the player's tactical position changed?")
    return questions


def _player_row(
    row: Any,
    projection_rows: list[Any],
    *,
    is_starting_xi: bool,
    bench_position: int | None,
    captain: bool,
    vice_captain: bool,
    priority: str,
) -> dict[str, Any]:
    if not projection_rows:
        raise ValueError(f"Projection run has no rows for player {row['source_player_id']!r}")
    current = projection_rows[0]
    horizon_points = sum(float(item["expected_points"]) for item in projection_rows)
    uncertainty = sum(float(item["uncertainty"]) for item in projection_rows)
    player = {
        "source_player_id": str(row["source_player_id"]),
        "full_name": " ".join(
            part for part in (row["first_name"], row["second_name"]) if part
        ).strip(),
        "display_name": row["web_name"],
        "club": row["club"],
        "position": row["position"],
        "price": float(row["price_tenths"]) / 10,
        "current_official_status": row["status"],
        "official_chance_of_playing": row["chance_of_playing"],
        "is_starting_xi": is_starting_xi,
        "bench_position": bench_position,
        "is_captain": captain,
        "is_vice_captain": vice_captain,
        "expected_minutes": float(current["expected_minutes"]),
        "appearance_probability": float(current["appearance_probability"]),
        "sixty_minute_probability": float(current["sixty_probability"]),
        "gw_projected_points": float(current["expected_points"]),
        "planning_horizon_projected_points": horizon_points,
        "projection_uncertainty": uncertainty,
        "manual_overrides": current["override_rationale"],
        "uncertain_model_assumptions": _row_assumptions(current["assumptions_json"]),
        "priority": priority,
    }
    player["research_questions"] = _questions(player)
    return player


def generate_team_news_research_package(
    database: HistoricalDatabase,
    *,
    season_code: str,
    gameweek_number: int,
    projection_run_id: int,
    research_mode: str,
    research_window_start: str | datetime,
    recommendation_run_id: str | int | None = None,
    recommendation: dict[str, Any] | None = None,
    alternatives_limit: int = 15,
    research_timestamp: str | datetime | None = None,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    """Create and persist a deterministic package for one projection snapshot."""

    if research_mode not in RESEARCH_MODES:
        raise ValueError(f"Unknown research mode {research_mode!r}")
    if not 1 <= alternatives_limit <= 20:
        raise ValueError("alternatives_limit must be between 1 and 20")
    window_start = _timestamp(research_window_start, "research_window_start")
    timestamp = _timestamp(research_timestamp or datetime.now(UTC), "research_timestamp")
    context = database.connection.execute(
        """
        SELECT seasons.id AS season_id, gameweeks.id AS gameweek_id,
               gameweeks.deadline_time
        FROM seasons JOIN gameweeks ON gameweeks.season_id = seasons.id
        WHERE seasons.code = ? AND gameweeks.number = ?
        """,
        (season_code, gameweek_number),
    ).fetchone()
    if context is None:
        raise ValueError(f"{season_code} Gameweek {gameweek_number} is unavailable")
    projection = database.connection.execute(
        """
        SELECT id, generated_at, horizon_gameweeks, source_ingestion_run_id
        FROM projection_runs
        WHERE id = ? AND season_id = ? AND start_gameweek = ?
        """,
        (projection_run_id, context["season_id"], gameweek_number),
    ).fetchone()
    if projection is None:
        raise ValueError("Projection run does not cover the requested season and Gameweek")
    if context["deadline_time"] is not None:
        deadline = datetime.fromisoformat(str(context["deadline_time"]).replace("Z", "+00:00"))
        if datetime.fromisoformat(str(timestamp)) >= deadline:
            raise ValueError("Research package must be generated before the target deadline")
    snapshot = database.connection.execute(
        """
        SELECT manager_snapshots.id, manager_snapshots.captain_player_season_id,
               manager_snapshots.vice_captain_player_season_id
        FROM manager_snapshots
        WHERE season_id = ? AND gameweek_id = ?
        ORDER BY captured_at DESC, id DESC LIMIT 1
        """,
        (context["season_id"], context["gameweek_id"]),
    ).fetchone()
    if snapshot is None:
        raise ValueError("A saved manager snapshot is required to export the selected squad")
    entries = database.connection.execute(
        """
        SELECT entries.player_season_id, entries.is_starter, entries.bench_order,
               ps.source_player_id, ps.position, players.first_name, players.second_name,
               players.web_name, teams.short_name AS club,
               COALESCE(observation.price_tenths, ps.start_price_tenths) AS price_tenths,
               observation.status, observation.chance_of_playing_next_round AS chance_of_playing
        FROM manager_squad_entries entries
        JOIN player_seasons ps ON ps.id = entries.player_season_id
        JOIN players ON players.id = ps.player_id
        JOIN teams ON teams.id = ps.team_id
        LEFT JOIN player_gameweek_observations observation
          ON observation.player_season_id = ps.id AND observation.gameweek_id = ?
         AND observation.id = (
             SELECT latest.id FROM player_gameweek_observations latest
             WHERE latest.player_season_id = ps.id AND latest.gameweek_id = ?
               AND (? IS NULL OR latest.provenance_run_id = ?)
             ORDER BY latest.observed_at DESC, latest.observed_on DESC, latest.id DESC LIMIT 1
         )
        WHERE entries.manager_snapshot_id = ?
        ORDER BY entries.is_starter DESC, entries.bench_order, ps.source_player_id
        """,
        (
            context["gameweek_id"],
            context["gameweek_id"],
            projection["source_ingestion_run_id"],
            projection["source_ingestion_run_id"],
            snapshot["id"],
        ),
    ).fetchall()
    if not entries:
        raise ValueError("The selected manager snapshot has no squad entries")
    projection_rows = database.connection.execute(
        """
        SELECT projections.gameweek_number, projections.expected_minutes,
               projections.appearance_probability, projections.sixty_probability,
               projections.expected_points, projections.uncertainty,
               projections.assumptions_json, projections.override_rationale,
               ps.source_player_id
        FROM player_gameweek_projections projections
        JOIN player_seasons ps ON ps.id = projections.player_season_id
        WHERE projections.projection_run_id = ?
        ORDER BY ps.source_player_id, projections.gameweek_number
        """,
        (projection_run_id,),
    ).fetchall()
    by_player: dict[str, list[Any]] = {}
    for row in projection_rows:
        by_player.setdefault(str(row["source_player_id"]), []).append(row)
    selected_ids = {str(row["source_player_id"]) for row in entries}
    squad: list[dict[str, Any]] = []
    for row in entries:
        source_id = str(row["source_player_id"])
        bench = None if int(row["is_starter"]) else int(row["bench_order"])
        priority = (
            "critical"
            if (
                row["player_season_id"]
                in (snapshot["captain_player_season_id"], snapshot["vice_captain_player_season_id"])
            )
            else (
                "starting_xi"
                if int(row["is_starter"])
                else ("bench_cover" if bench == 1 else "squad")
            )
        )
        squad.append(
            _player_row(
                row,
                by_player.get(source_id, []),
                is_starting_xi=bool(row["is_starter"]),
                bench_position=bench,
                captain=row["player_season_id"] == snapshot["captain_player_season_id"],
                vice_captain=row["player_season_id"] == snapshot["vice_captain_player_season_id"],
                priority=priority,
            )
        )
    directory_rows = database.connection.execute(
        """
        SELECT ps.source_player_id, players.first_name, players.second_name,
               players.web_name, teams.short_name AS club, ps.position
        FROM player_seasons ps JOIN players ON players.id = ps.player_id
        JOIN teams ON teams.id = ps.team_id
        WHERE ps.season_id = ? AND ps.identifier_namespace = 'official-fpl'
        ORDER BY ps.source_player_id
        """,
        (context["season_id"],),
    ).fetchall()
    directory = [
        {
            "source_player_id": str(row["source_player_id"]),
            "full_name": " ".join(
                part for part in (row["first_name"], row["second_name"]) if part
            ).strip(),
            "display_name": row["web_name"],
            "club": row["club"],
            "position": row["position"],
        }
        for row in directory_rows
    ]
    selected_by_position = {item["position"]: item for item in squad}
    ranking_rows = database.connection.execute(
        """
        SELECT ps.source_player_id, players.first_name, players.second_name,
               players.web_name, teams.short_name AS club, ps.position,
               COALESCE(observation.price_tenths, ps.start_price_tenths) AS price_tenths,
               SUM(projections.expected_points) AS horizon_points,
               SUM(projections.uncertainty) AS uncertainty
        FROM player_gameweek_projections projections
        JOIN player_seasons ps ON ps.id = projections.player_season_id
        JOIN players ON players.id = ps.player_id JOIN teams ON teams.id = ps.team_id
        LEFT JOIN player_gameweek_observations observation
          ON observation.player_season_id = ps.id AND observation.gameweek_id = ?
         AND observation.id = (SELECT latest.id FROM player_gameweek_observations latest
             WHERE latest.player_season_id = ps.id AND latest.gameweek_id = ?
               AND (? IS NULL OR latest.provenance_run_id = ?)
             ORDER BY latest.observed_at DESC, latest.observed_on DESC, latest.id DESC LIMIT 1)
        WHERE projections.projection_run_id = ? AND ps.source_player_id NOT IN ({})
        GROUP BY ps.source_player_id
        ORDER BY horizon_points DESC, ps.source_player_id
        """.format(",".join("?" for _ in selected_ids)),
        (
            context["gameweek_id"],
            context["gameweek_id"],
            projection["source_ingestion_run_id"],
            projection["source_ingestion_run_id"],
            projection_run_id,
            *sorted(selected_ids),
        ),
    ).fetchall()
    alternatives: list[dict[str, Any]] = []
    for row in ranking_rows:
        if row["position"] not in selected_by_position:
            continue
        selected = selected_by_position[row["position"]]
        alternatives.append(
            {
                "source_player_id": str(row["source_player_id"]),
                "full_name": " ".join(
                    part for part in (row["first_name"], row["second_name"]) if part
                ).strip(),
                "display_name": row["web_name"],
                "club": row["club"],
                "position": row["position"],
                "price": float(row["price_tenths"] or 0) / 10,
                "selected_player_id": selected["source_player_id"],
                "projected_objective_difference": float(row["horizon_points"])
                - float(selected["planning_horizon_projected_points"]),
                "decision_relevance": (
                    "highest-ranked legal same-position replacement outside the selected squad"
                ),
                "method": "projection_ranked_legal_replacement_fallback",
                "priority": "alternative",
            }
        )
        if len(alternatives) >= alternatives_limit:
            break
    body = {
        "season_code": season_code,
        "gameweek": gameweek_number,
        "target_deadline": _timestamp(context["deadline_time"], "target_deadline", allow_none=True),
        "research_timestamp": timestamp,
        "research_window_start": window_start,
        "research_mode": research_mode,
        "projection_run_id": int(projection_run_id),
        "optimisation_or_recommendation_run_id": None
        if recommendation_run_id is None
        else str(recommendation_run_id),
        "source_ingestion_run_id": projection["source_ingestion_run_id"],
        "prompt_version": PROMPT_VERSION,
        "selected_squad": squad,
        "alternatives": alternatives,
        "alternatives_method": "supplied_recommendation"
        if recommendation
        else "projection_ranked_legal_replacement_fallback",
        "alternatives_limit": alternatives_limit,
        "player_directory": directory,
        "operator_recommendation_context": recommendation or {},
    }
    seed = hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()
    package = {"input_package_id": f"tnp-{seed[:24]}", **body}
    package["input_package_hash"] = compute_package_hash(package)
    package_json = _canonical(package)
    with database.transaction():
        database.connection.execute(
            """
            INSERT OR IGNORE INTO team_news_input_packages (
                package_id, package_hash, season_id, gameweek_id, target_deadline,
                research_timestamp, research_window_start, research_mode,
                projection_run_id, recommendation_run_id, source_ingestion_run_id,
                prompt_version, package_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                package["input_package_id"],
                package["input_package_hash"],
                context["season_id"],
                context["gameweek_id"],
                package["target_deadline"],
                package["research_timestamp"],
                package["research_window_start"],
                research_mode,
                projection_run_id,
                package["optimisation_or_recommendation_run_id"],
                package["source_ingestion_run_id"],
                PROMPT_VERSION,
                package_json,
                timestamp,
            ),
        )
    if output_path is not None:
        Path(output_path).write_text(
            json.dumps(package, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    return package


def validate_package_against_database(
    database: HistoricalDatabase, result: dict[str, Any]
) -> dict[str, Any]:
    """Validate package identity and temporal boundaries before importing a result."""

    package_id = result.get("input_package_id")
    package_hash = result.get("input_package_hash")
    if not isinstance(package_id, str) or not isinstance(package_hash, str):
        raise ValueError("v3 result must echo input_package_id and input_package_hash")
    if package_id in {"inp-package-id", "tnp-package-id", "input-package-id"}:
        raise ValueError(
            "The JSON still contains a template package ID. Generate a research "
            "package in this app and preserve its exact input_package_id and "
            "input_package_hash in the ChatGPT result."
        )
    package_row = database.connection.execute(
        "SELECT * FROM team_news_input_packages WHERE package_id = ?", (package_id,)
    ).fetchone()
    if package_row is None:
        raise ValueError(f"Unknown input package {package_id!r}")
    if package_row["package_hash"] != package_hash:
        raise ValueError("Input package hash does not match the stored package")
    if compute_package_hash(json.loads(package_row["package_json"])) != package_hash:
        raise ValueError("Stored input package hash is invalid")
    stored_package = json.loads(package_row["package_json"])
    if result.get("season_code") != stored_package["season_code"] or int(
        result.get("gameweek", -1)
    ) != int(stored_package["gameweek"]):
        raise ValueError("Research result does not match the input package Gameweek")
    for field in ("research_mode", "research_window_start", "target_deadline"):
        if result.get(field) != stored_package.get(field):
            raise ValueError(f"Research result {field} does not match the input package")
    generated = datetime.fromisoformat(str(result["generated_at"]).replace("Z", "+00:00"))
    deadline = package_row["target_deadline"]
    if deadline is not None and generated >= datetime.fromisoformat(
        str(deadline).replace("Z", "+00:00")
    ):
        raise ValueError("Research result was generated after the target deadline")
    return dict(package_row)
