"""Auditable, human-reviewed projection modifiers.

The research importer never creates a modifier.  A modifier is created only
after an accepted evidence item has been explicitly reviewed by an operator.
This module keeps that boundary and converts active modifiers into the small
projection-override interface used by :mod:`fpl_engine.projections`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .history.database import HistoricalDatabase

SUPPORTED_MODIFIER_TYPES = frozenset(
    {
        "expected_minutes",
        "expected_minutes_delta",
        "appearance_probability",
        "appearance_probability_delta",
        "starting_probability",
        "starting_probability_delta",
        "sixty_probability",
        "sixty_probability_delta",
        "availability",
    }
)
SUPPORTED_OPERATIONS = frozenset({"set", "delta", "multiplier", "unavailable"})
PROBABILITY_TYPES = frozenset(
    {
        "appearance_probability",
        "appearance_probability_delta",
        "starting_probability",
        "starting_probability_delta",
        "sixty_probability",
        "sixty_probability_delta",
        "availability",
    }
)


class ModifierValidationError(ValueError):
    """Raised when a reviewed modifier cannot safely affect projections."""


def _utc(value: datetime, field: str) -> str:
    if value.tzinfo is None:
        raise ModifierValidationError(f"{field} must be timezone-aware")
    return value.astimezone(UTC).isoformat()


def _parse_timestamp(value: str | None, field: str) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ModifierValidationError(f"{field} must be timezone-aware")
    return parsed.astimezone(UTC)


@dataclass(frozen=True)
class ReviewedProjectionModifier:
    source_player_id: str | None
    modifier_type: str
    operation: str
    value: float
    start_gameweek: int
    end_gameweek: int
    evidence_ids: tuple[int, ...]
    rationale: str
    reviewed_by: str
    reviewed_at: datetime
    expires_at: datetime | None = None
    source_team_id: str | None = None
    research_run_id: str | None = None
    input_package_id: str | None = None
    status: str = "accepted"
    model_support: str = "supported"
    version: int = 1
    supersedes_id: int | None = None
    id: int | None = None

    def __post_init__(self) -> None:
        if bool(self.source_player_id) == bool(self.source_team_id):
            raise ModifierValidationError(
                "A modifier must identify exactly one player or team"
            )
        if self.modifier_type not in SUPPORTED_MODIFIER_TYPES:
            raise ModifierValidationError(
                f"Unsupported modifier type {self.modifier_type!r}"
            )
        if self.operation not in SUPPORTED_OPERATIONS:
            raise ModifierValidationError(f"Unsupported modifier operation {self.operation!r}")
        if self.operation == "unavailable" and self.modifier_type != "availability":
            raise ModifierValidationError(
                "The unavailable operation is only valid for availability"
            )
        if self.start_gameweek < 1 or self.end_gameweek > 38:
            raise ModifierValidationError("Modifier Gameweeks must be between 1 and 38")
        if self.start_gameweek > self.end_gameweek:
            raise ModifierValidationError("Modifier start Gameweek must not exceed its end")
        if not self.evidence_ids:
            raise ModifierValidationError("A modifier must cite at least one evidence item")
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ModifierValidationError("Evidence IDs must be unique")
        if any(int(item) <= 0 for item in self.evidence_ids):
            raise ModifierValidationError("Evidence IDs must be positive integers")
        if not self.rationale.strip():
            raise ModifierValidationError("A modifier rationale is required")
        if not self.reviewed_by.strip():
            raise ModifierValidationError("A modifier reviewer is required")
        if self.status not in {"accepted", "superseded", "rejected"}:
            raise ModifierValidationError("Invalid modifier status")
        if self.model_support != "supported":
            raise ModifierValidationError("Only model-supported modifiers can be applied")
        if self.version < 1:
            raise ModifierValidationError("Modifier version must be positive")
        _utc(self.reviewed_at, "reviewed_at")
        if self.expires_at is not None:
            _utc(self.expires_at, "expires_at")
        if self.modifier_type in PROBABILITY_TYPES and self.operation != "unavailable":
            if self.modifier_type.endswith("_delta") or self.operation == "delta":
                if not -1.0 <= self.value <= 1.0:
                    raise ModifierValidationError("Probability deltas must be between -1 and 1")
            elif not 0.0 <= self.value <= 1.0:
                raise ModifierValidationError("Probability values must be between 0 and 1")
        if self.modifier_type.startswith("expected_minutes"):
            if self.operation == "multiplier" and self.value < 0:
                raise ModifierValidationError("Expected minutes multipliers must be non-negative")
            if self.modifier_type == "expected_minutes" and self.operation == "set":
                if not 0.0 <= self.value <= 90.0:
                    raise ModifierValidationError("Expected minutes must be between 0 and 90")
            elif self.operation != "multiplier" and not -90.0 <= self.value <= 90.0:
                raise ModifierValidationError("Expected minutes deltas must be between -90 and 90")


def _row_to_modifier(row: Any) -> ReviewedProjectionModifier:
    return ReviewedProjectionModifier(
        id=int(row["id"]),
        source_player_id=row["source_player_id"],
        source_team_id=row["source_team_id"],
        modifier_type=row["modifier_type"],
        operation=row["operation"],
        value=float(row["value"]),
        start_gameweek=int(row["start_gameweek"]),
        end_gameweek=int(row["end_gameweek"]),
        evidence_ids=tuple(int(item) for item in json.loads(row["evidence_ids_json"])),
        rationale=row["rationale"],
        reviewed_by=row["reviewed_by"],
        reviewed_at=_parse_timestamp(row["reviewed_at"], "reviewed_at") or datetime.now(UTC),
        expires_at=_parse_timestamp(row["expires_at"], "expires_at"),
        research_run_id=row["research_run_id"],
        input_package_id=row["input_package_id"],
        status=row["status"],
        model_support=row["model_support"],
        version=int(row["version"]),
        supersedes_id=row["supersedes_id"],
    )


def create_reviewed_modifier(
    database: HistoricalDatabase,
    *,
    season_code: str,
    gameweek_number: int,
    modifier: ReviewedProjectionModifier,
    commit: bool = True,
) -> int:
    """Persist a reviewed modifier after validating its evidence boundary."""

    if modifier.source_team_id is not None:
        raise ModifierValidationError("Team modifiers are not yet model-supported")
    season = database.connection.execute(
        "SELECT id FROM seasons WHERE code = ?", (season_code,)
    ).fetchone()
    if season is None:
        raise ModifierValidationError(f"Season {season_code!r} is unavailable")
    gameweek = database.connection.execute(
        "SELECT id FROM gameweeks WHERE season_id = ? AND number = ?",
        (season["id"], gameweek_number),
    ).fetchone()
    if gameweek is None:
        raise ModifierValidationError(f"Gameweek {gameweek_number} is unavailable")
    placeholders = ",".join("?" for _ in modifier.evidence_ids)
    rows = database.connection.execute(
        f"""
        SELECT evidence.id, evidence.review_status, evidence.research_run_id,
               evidence.input_package_id, ps.source_player_id
        FROM news_evidence evidence
        LEFT JOIN player_seasons ps ON ps.id = evidence.player_season_id
        WHERE evidence.id IN ({placeholders})
        """,
        modifier.evidence_ids,
    ).fetchall()
    if len(rows) != len(set(modifier.evidence_ids)):
        raise ModifierValidationError("Every cited evidence item must exist")
    for row in rows:
        if row["review_status"] != "accepted":
            raise ModifierValidationError("Only accepted evidence can support a modifier")
        if row["source_player_id"] != modifier.source_player_id:
            raise ModifierValidationError("Modifier player does not match its evidence")
        if modifier.research_run_id and row["research_run_id"] != modifier.research_run_id:
            raise ModifierValidationError("Modifier research run does not match its evidence")
        if modifier.input_package_id and row["input_package_id"] != modifier.input_package_id:
            raise ModifierValidationError("Modifier package does not match its evidence")
    if modifier.supersedes_id is not None:
        superseded = database.connection.execute(
            """
            SELECT source_player_id, source_team_id, status
            FROM reviewed_projection_modifiers
            WHERE id = ?
            """,
            (modifier.supersedes_id,),
        ).fetchone()
        if superseded is None:
            raise ModifierValidationError("Superseded modifier does not exist")
        if (
            superseded["source_player_id"] != modifier.source_player_id
            or superseded["source_team_id"] != modifier.source_team_id
        ):
            raise ModifierValidationError("A superseding modifier must keep the same subject")
    if modifier.research_run_id:
        run = database.connection.execute(
            """
            SELECT generated_at, target_deadline
            FROM team_news_research_runs
            WHERE research_run_id = ?
            """,
            (modifier.research_run_id,),
        ).fetchone()
        if run is not None and run["target_deadline"]:
            generated = datetime.fromisoformat(str(run["generated_at"]).replace("Z", "+00:00"))
            deadline = datetime.fromisoformat(str(run["target_deadline"]).replace("Z", "+00:00"))
            if (
                generated.tzinfo is not None
                and deadline.tzinfo is not None
                and generated > deadline
            ):
                raise ModifierValidationError("Post-deadline research cannot affect projections")
    cursor = database.connection.execute(
        """
        INSERT INTO reviewed_projection_modifiers (
            season_id, gameweek_id, source_player_id, source_team_id,
            modifier_type, operation, value, start_gameweek, end_gameweek,
            evidence_ids_json, rationale, reviewed_by, reviewed_at, expires_at,
            research_run_id, input_package_id, status, model_support, version,
            supersedes_id, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        RETURNING id
        """,
        (
            season["id"],
            gameweek["id"],
            modifier.source_player_id,
            modifier.source_team_id,
            modifier.modifier_type,
            modifier.operation,
            modifier.value,
            modifier.start_gameweek,
            modifier.end_gameweek,
            json.dumps(modifier.evidence_ids),
            modifier.rationale.strip(),
            modifier.reviewed_by.strip(),
            _utc(modifier.reviewed_at, "reviewed_at"),
            None if modifier.expires_at is None else _utc(modifier.expires_at, "expires_at"),
            modifier.research_run_id,
            modifier.input_package_id,
            modifier.status,
            modifier.model_support,
            modifier.version,
            modifier.supersedes_id,
            datetime.now(UTC).isoformat(),
        ),
    )
    modifier_id = int(cursor.fetchone()[0])
    if commit:
        database.connection.commit()
    return modifier_id


def active_modifiers(
    database: HistoricalDatabase,
    *,
    season_code: str,
    start_gameweek: int,
    horizon_gameweeks: int,
    now: datetime | None = None,
) -> tuple[ReviewedProjectionModifier, ...]:
    """Load accepted, non-expired modifiers overlapping a projection horizon."""

    if horizon_gameweeks <= 0:
        raise ValueError("Projection horizon must be positive")
    as_of = now or datetime.now(UTC)
    if as_of.tzinfo is None:
        raise ModifierValidationError("Modifier evaluation time must be timezone-aware")
    end_gameweek = start_gameweek + horizon_gameweeks - 1
    rows = database.connection.execute(
        """
        SELECT modifiers.*
        FROM reviewed_projection_modifiers modifiers
        JOIN seasons ON seasons.id = modifiers.season_id
        WHERE seasons.code = ?
          AND modifiers.status = 'accepted'
          AND NOT EXISTS (
              SELECT 1
              FROM reviewed_projection_modifiers superseding
              WHERE superseding.supersedes_id = modifiers.id
                AND superseding.status = 'accepted'
          )
          AND modifiers.start_gameweek <= ?
          AND modifiers.end_gameweek >= ?
          AND (modifiers.expires_at IS NULL OR datetime(modifiers.expires_at) > datetime(?))
        ORDER BY modifiers.id
        """,
        (season_code, end_gameweek, start_gameweek, as_of.astimezone(UTC).isoformat()),
    ).fetchall()
    return tuple(_row_to_modifier(row) for row in rows)


def modifier_overrides(
    database: HistoricalDatabase,
    *,
    season_code: str,
    start_gameweek: int,
    horizon_gameweeks: int,
    now: datetime | None = None,
) -> tuple[tuple[Any, ...], tuple[tuple[int, int, str], ...]]:
    """Return projection overrides and their modifier provenance.

    The second return value contains ``(gameweek, modifier_id, effective_json)``
    rows for persistence alongside a revised projection run.
    """

    from .projections import ProjectionOverride

    modifiers = active_modifiers(
        database,
        season_code=season_code,
        start_gameweek=start_gameweek,
        horizon_gameweeks=horizon_gameweeks,
        now=now,
    )
    grouped: dict[tuple[str, int], list[ReviewedProjectionModifier]] = {}
    for modifier in modifiers:
        if modifier.source_player_id is None:
            continue
        for gameweek in range(
            max(start_gameweek, modifier.start_gameweek),
            min(start_gameweek + horizon_gameweeks - 1, modifier.end_gameweek) + 1,
        ):
            grouped.setdefault((modifier.source_player_id, gameweek), []).append(modifier)
    overrides = []
    provenance: list[tuple[int, int, str]] = []
    for (player_id, gameweek), items in sorted(grouped.items()):
        state: dict[str, Any] = {"modifier_ids": []}
        seen_sets: set[str] = set()
        for item in items:
            base_type = item.modifier_type.removesuffix("_delta")
            if item.operation == "unavailable":
                base_type = "availability"
                value = 0.0
                operation = "set"
            else:
                value = item.value
                operation = "delta" if item.modifier_type.endswith("_delta") else item.operation
            if operation == "set":
                if base_type in seen_sets:
                    raise ModifierValidationError(
                        f"Contradictory set modifiers for {player_id} GW{gameweek}: {base_type}"
                    )
                seen_sets.add(base_type)
                state[base_type] = value
            elif operation == "delta":
                state[f"{base_type}_delta"] = state.get(f"{base_type}_delta", 0.0) + value
            elif operation == "multiplier":
                state[f"{base_type}_multiplier"] = (
                    state.get(f"{base_type}_multiplier", 1.0) * value
                )
            state["modifier_ids"].append(item.id)
        overrides.append(
            ProjectionOverride(
                source_player_id=player_id,
                gameweek_number=gameweek,
                expected_minutes=state.get("expected_minutes"),
                expected_minutes_delta=state.get("expected_minutes_delta", 0.0),
                expected_minutes_multiplier=state.get("expected_minutes_multiplier", 1.0),
                appearance_probability=state.get("appearance_probability"),
                appearance_probability_delta=state.get("appearance_probability_delta", 0.0),
                appearance_probability_multiplier=state.get(
                    "appearance_probability_multiplier", 1.0
                ),
                start_probability=state.get("starting_probability"),
                start_probability_delta=state.get("starting_probability_delta", 0.0),
                start_probability_multiplier=state.get("starting_probability_multiplier", 1.0),
                sixty_probability=state.get("sixty_probability"),
                sixty_probability_delta=state.get("sixty_probability_delta", 0.0),
                sixty_probability_multiplier=state.get("sixty_probability_multiplier", 1.0),
                availability=state.get("availability"),
                availability_delta=state.get("availability_delta", 0.0),
                availability_multiplier=state.get("availability_multiplier", 1.0),
                rationale="; ".join(item.rationale for item in items),
                source="reviewed-research",
                confidence="medium",
                modifier_ids=tuple(state["modifier_ids"]),
            )
        )
        effective = json.dumps(state, sort_keys=True)
        for item in items:
            provenance.append((gameweek, int(item.id), effective))
    return tuple(overrides), tuple(provenance)


def apply_reviewed_modifiers(
    database: HistoricalDatabase,
    *,
    season_code: str,
    start_gameweek: int,
    horizon_gameweeks: int,
    now: datetime | None = None,
) -> tuple[tuple[Any, ...], tuple[tuple[int, int, str], ...]]:
    """Build deterministic projection inputs from current reviewed modifiers."""

    return modifier_overrides(
        database,
        season_code=season_code,
        start_gameweek=start_gameweek,
        horizon_gameweeks=horizon_gameweeks,
        now=now,
    )
