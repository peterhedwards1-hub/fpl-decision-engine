"""Immutable pre-deadline forecast capture for a live season.

A missed pre-deadline snapshot cannot be reconstructed honestly afterwards, and
no amount of later modelling recovers it. This captures, in one call before
each deadline, the forecasts the forward gate will need: the incumbent and
every declared candidate, generated from the same pre-deadline observation and
tied to the ingestion run that produced it.

The incumbent matters as much as the candidates. A challenger run with nothing
to compare against is not evidence, and the gate requires matched pairs.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from .config import SeasonRules
from .history.database import HistoricalDatabase
from .projections import (
    DEFAULT_MODEL_CONFIG,
    MODEL_VERSION,
    ProjectionModelConfig,
    RatesProjectionModel,
)


@dataclass(frozen=True)
class CapturedForecast:
    role: str
    key: str
    projection_run_id: int
    model_version: str
    model_config_sha256: str
    projection_rows: int
    priced_rows: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def capture_gameweek_forecasts(
    database: HistoricalDatabase,
    rules: SeasonRules,
    *,
    season_code: str,
    gameweek: int,
    horizon_gameweeks: int = 8,
    incumbent_config: ProjectionModelConfig = DEFAULT_MODEL_CONFIG,
    incumbent_model_version: str = MODEL_VERSION,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Persist the incumbent and every declared candidate before the deadline.

    Fails closed: if the deadline has passed there is no honest pre-deadline
    forecast to take, so nothing is written.
    """

    from .promotion import declared_challenger_config

    season = database.connection.execute(
        "SELECT id FROM seasons WHERE code = ?", (season_code,)
    ).fetchone()
    if season is None:
        raise ValueError(f"Season {season_code!r} is unavailable")
    if rules.season != season_code:
        raise ValueError(
            f"Rules season {rules.season!r} does not match {season_code!r}"
        )
    deadline_row = database.connection.execute(
        """
        SELECT deadline_time FROM gameweeks
        WHERE season_id = ? AND number = ?
        """,
        (int(season["id"]), gameweek),
    ).fetchone()
    if deadline_row is None or deadline_row["deadline_time"] is None:
        raise ValueError(
            f"GW{gameweek} has no recorded deadline, so a pre-deadline capture "
            "cannot be proven"
        )
    deadline = datetime.fromisoformat(
        str(deadline_row["deadline_time"]).replace("Z", "+00:00")
    )
    captured_at = generated_at or datetime.now(UTC)
    if captured_at.tzinfo is None:
        raise ValueError("Capture time must be timezone-aware")
    if captured_at.astimezone(UTC) >= deadline.astimezone(UTC):
        raise ValueError(
            f"GW{gameweek}'s deadline has passed; a pre-deadline forecast can "
            "no longer be captured and must not be back-filled"
        )
    snapshot = _pre_deadline_snapshot(
        database,
        season_id=int(season["id"]),
        gameweek=gameweek,
        deadline=deadline,
    )
    if snapshot is None:
        raise ValueError(
            f"GW{gameweek} has no exact pre-deadline snapshot yet; collect one "
            "before generating forecasts against it"
        )

    declarations = [
        dict(row)
        for row in database.connection.execute(
            """
            SELECT candidate_key, model_version, model_config_json,
                   model_config_sha256
            FROM model_candidate_registrations
            WHERE season_id = ? AND status = 'declared'
            ORDER BY candidate_key
            """,
            (int(season["id"]),),
        )
    ]
    plan: list[tuple[str, str, str, ProjectionModelConfig, str]] = [
        (
            "incumbent",
            incumbent_model_version,
            incumbent_model_version,
            incumbent_config,
            _config_digest(incumbent_config),
        )
    ]
    for declaration in declarations:
        config = declared_challenger_config(
            {
                "candidate_key": declaration["candidate_key"],
                "model_config": json.loads(declaration["model_config_json"]),
            }
        )
        plan.append(
            (
                "challenger",
                str(declaration["candidate_key"]),
                str(declaration["model_version"]),
                config,
                str(declaration["model_config_sha256"]),
            )
        )
    versions = [entry[2] for entry in plan]
    if len(set(versions)) != len(versions):
        raise ValueError(
            "The incumbent and every candidate must use distinct model "
            "versions, or their runs cannot be told apart later"
        )

    captured = []
    for role, key, model_version, config, digest in plan:
        result = RatesProjectionModel(
            database,
            rules,
            config=config,
            model_version=model_version,
        ).project(
            season_code=season_code,
            start_gameweek=gameweek,
            horizon_gameweeks=horizon_gameweeks,
            generated_at=captured_at,
            observation_mode="latest_pre_deadline",
            use_availability=True,
            fixture_as_of=captured_at,
            persist=True,
        )
        if result.projection_run_id is None:
            raise ValueError(f"{key} projection was not persisted")
        captured.append(
            CapturedForecast(
                role=role,
                key=key,
                projection_run_id=int(result.projection_run_id),
                model_version=model_version,
                model_config_sha256=digest,
                projection_rows=len(result.projections),
                priced_rows=sum(
                    projection.fixture_count > 0
                    for projection in result.projections
                ),
            )
        )
    return {
        "season_code": season_code,
        "gameweek": gameweek,
        "horizon_gameweeks": horizon_gameweeks,
        "captured_at": captured_at.astimezone(UTC).isoformat(),
        "deadline_time": deadline.astimezone(UTC).isoformat(),
        "source_ingestion_run_id": snapshot["provenance_run_id"],
        "snapshot_observed_at": snapshot["observed_at"],
        "forecasts": [entry.as_dict() for entry in captured],
        "declared_candidates": [
            str(declaration["candidate_key"]) for declaration in declarations
        ],
        "note": (
            "Outcomes are joined afterwards. This record is the part that "
            "cannot be reconstructed once the deadline passes."
        ),
    }


def _pre_deadline_snapshot(
    database: HistoricalDatabase,
    *,
    season_id: int,
    gameweek: int,
    deadline: datetime,
) -> dict[str, Any] | None:
    row = database.connection.execute(
        """
        SELECT observations.provenance_run_id, MAX(observations.observed_at)
                   AS observed_at
        FROM player_gameweek_observations observations
        JOIN gameweeks ON gameweeks.id = observations.gameweek_id
        WHERE gameweeks.season_id = ?
          AND gameweeks.number = ?
          AND observations.observation_kind = 'live_pre_deadline'
          AND observations.timing_quality = 'exact'
          AND datetime(observations.observed_at) < datetime(?)
        GROUP BY observations.provenance_run_id
        ORDER BY observed_at DESC
        LIMIT 1
        """,
        (season_id, gameweek, deadline.astimezone(UTC).isoformat()),
    ).fetchone()
    return None if row is None else dict(row)


def _config_digest(config: ProjectionModelConfig) -> str:
    import hashlib

    return hashlib.sha256(
        json.dumps(
            asdict(config), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
