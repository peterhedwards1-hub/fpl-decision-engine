"""Selection of projection runs that are safe to drive live decisions."""

from __future__ import annotations

from dataclasses import dataclass

from .history.database import HistoricalDatabase
from .projections import MODEL_VERSION

#: The preseason opening-squad decision is a different decision from an
#: ordinary in-season projection, and it is allowed a different model. Runs
#: labelled with this version are eligible for that decision and no other.
#: Keeping it outside the incumbent family is what stops the generic
#: newest-qualified-run rule from picking it up in October.
#: Kept in step with ``PRESEASON_CARRY_FORWARD_MODEL_VERSION``. The ``-v2``
#: generation carries the corrected minutes reconciliation and calibrated
#: appearance probabilities; a run labelled with the previous suffix is a
#: different model and is deliberately no longer eligible.
PRESEASON_MODEL_VERSION_SUFFIX = "-preseason-carry-forward-v2"
PRESEASON_MODEL_VERSION = f"{MODEL_VERSION}{PRESEASON_MODEL_VERSION_SUFFIX}"

#: The Gameweek at which a preseason decision exists at all. After it, there is
#: real evidence in the target season and the incumbent selector governs.
PRESEASON_GAMEWEEK = 1


@dataclass(frozen=True)
class ProductionProjectionRun:
    """One explicitly qualified run used by a decision surface."""

    run_id: int
    model_version: str
    generated_at: str
    horizon_gameweeks: int


@dataclass(frozen=True)
class PlanningHorizonRecommendation:
    """How far a live rolling decision should look and why."""

    base_horizon_gameweeks: int
    required_horizon_gameweeks: int
    exceptional_gameweeks: tuple[int, ...]
    reasons: tuple[str, ...]


def recommend_planning_horizon(
    database: HistoricalDatabase,
    *,
    season_code: str,
    start_gameweek: int,
    base_horizon_gameweeks: int = 5,
    extension_window_gameweeks: int = 3,
    chip_expiry_gameweek: int | None = None,
) -> PlanningHorizonRecommendation:
    """Extend a rolling horizon for nearby known blanks, doubles or chip expiry."""

    if not season_code.strip():
        raise ValueError("A season code is required")
    if not 1 <= start_gameweek <= 38:
        raise ValueError("Start Gameweek must be between 1 and 38")
    if base_horizon_gameweeks < 1:
        raise ValueError("Base planning horizon must be positive")
    if extension_window_gameweeks < 0:
        raise ValueError("Horizon extension window cannot be negative")
    if chip_expiry_gameweek is not None and not 1 <= chip_expiry_gameweek <= 38:
        raise ValueError("Chip expiry Gameweek must be between 1 and 38")

    remaining = 39 - start_gameweek
    base = min(base_horizon_gameweeks, remaining)
    scan_start = start_gameweek + base
    scan_end = min(38, scan_start + extension_window_gameweeks - 1)
    if scan_start > scan_end:
        return PlanningHorizonRecommendation(base, base, (), ())

    rows = database.connection.execute(
        """
        WITH team_fixtures AS (
            SELECT fixtures.gameweek_id, fixtures.home_team_id AS team_id
            FROM fixtures
            UNION ALL
            SELECT fixtures.gameweek_id, fixtures.away_team_id AS team_id
            FROM fixtures
        )
        SELECT gameweeks.number AS gameweek_number,
               teams.id AS team_id,
               COUNT(team_fixtures.team_id) AS fixture_count
        FROM gameweeks
        JOIN seasons ON seasons.id = gameweeks.season_id
        CROSS JOIN teams
        LEFT JOIN team_fixtures
          ON team_fixtures.gameweek_id = gameweeks.id
         AND team_fixtures.team_id = teams.id
        WHERE seasons.code = ?
          AND teams.season_id = seasons.id
          AND gameweeks.number BETWEEN ? AND ?
          AND EXISTS (
              SELECT 1 FROM fixtures
              WHERE fixtures.gameweek_id = gameweeks.id
          )
        GROUP BY gameweeks.number, teams.id
        ORDER BY gameweeks.number, teams.id
        """,
        (season_code, scan_start, scan_end),
    ).fetchall()
    exceptional = sorted(
        {
            int(row["gameweek_number"])
            for row in rows
            if int(row["fixture_count"]) != 1
        }
    )
    reasons = [
        f"GW{gameweek} has a known blank or double in the extension window"
        for gameweek in exceptional
    ]
    if (
        chip_expiry_gameweek is not None
        and scan_start <= chip_expiry_gameweek <= scan_end
    ):
        exceptional.append(chip_expiry_gameweek)
        exceptional = sorted(set(exceptional))
        reasons.append(
            f"The active chip set expires in GW{chip_expiry_gameweek}"
        )
    required = (
        base
        if not exceptional
        else max(gameweek - start_gameweek + 1 for gameweek in exceptional)
    )
    return PlanningHorizonRecommendation(
        base_horizon_gameweeks=base,
        required_horizon_gameweeks=required,
        exceptional_gameweeks=tuple(exceptional),
        reasons=tuple(reasons),
    )


def select_production_projection_run(
    database: HistoricalDatabase,
    *,
    season_code: str,
    start_gameweek: int,
    minimum_horizon_gameweeks: int,
    production_model_version: str = MODEL_VERSION,
) -> ProductionProjectionRun | None:
    """Return the newest incumbent-family run with enough decision horizon.

    Projection runs serve several purposes. One-Gameweek diagnostics and
    unqualified challengers may be newer than the production forecast, but
    recency alone does not authorise either to drive squad or transfer advice.
    Reviewed-news suffixes remain eligible because they preserve the incumbent
    model family and the caller's required horizon.
    """

    if not season_code.strip():
        raise ValueError("A season code is required")
    if not 1 <= start_gameweek <= 38:
        raise ValueError("Start Gameweek must be between 1 and 38")
    if minimum_horizon_gameweeks <= 0:
        raise ValueError("Minimum projection horizon must be positive")
    if not production_model_version.strip():
        raise ValueError("A production model version is required")

    allowed_versions = (
        production_model_version,
        f"{production_model_version}-post-news-v2",
        f"{production_model_version}-post-research",
        f"{production_model_version}-post-news-v2-post-research",
    )
    row = database.connection.execute(
        """
        SELECT projection_runs.id, projection_runs.model_version,
               projection_runs.generated_at,
               projection_runs.horizon_gameweeks
        FROM projection_runs
        JOIN seasons ON seasons.id = projection_runs.season_id
        WHERE seasons.code = ?
          AND projection_runs.start_gameweek = ?
          AND projection_runs.horizon_gameweeks >= ?
          AND projection_runs.model_version IN (?, ?, ?, ?)
        ORDER BY datetime(projection_runs.generated_at) DESC,
                 projection_runs.id DESC
        LIMIT 1
        """,
        (
            season_code,
            start_gameweek,
            minimum_horizon_gameweeks,
            *allowed_versions,
        ),
    ).fetchone()
    if row is None:
        return None
    return ProductionProjectionRun(
        run_id=int(row["id"]),
        model_version=str(row["model_version"]),
        generated_at=str(row["generated_at"]),
        horizon_gameweeks=int(row["horizon_gameweeks"]),
    )


def select_preseason_projection_run(
    database: HistoricalDatabase,
    *,
    season_code: str,
    start_gameweek: int,
    minimum_horizon_gameweeks: int,
    preseason_model_version: str = PRESEASON_MODEL_VERSION,
) -> ProductionProjectionRun | None:
    """Return the newest validated preseason run for a GW1 opening squad.

    Separate from `select_production_projection_run` on purpose. The preseason
    question — every club has played nothing, so what do we believe about them
    — is not the in-season question, and a model that is the right answer to
    one is not automatically the right answer to the other. This selector only
    ever returns a run at the preseason Gameweek; asking it about GW12 returns
    nothing rather than quietly handing back a preseason forecast.
    """

    if not season_code.strip():
        raise ValueError("A season code is required")
    if not 1 <= start_gameweek <= 38:
        raise ValueError("Start Gameweek must be between 1 and 38")
    if minimum_horizon_gameweeks <= 0:
        raise ValueError("Minimum projection horizon must be positive")
    if not preseason_model_version.strip():
        raise ValueError("A preseason model version is required")
    if start_gameweek != PRESEASON_GAMEWEEK:
        return None

    allowed_versions = (
        preseason_model_version,
        f"{preseason_model_version}-post-news-v2",
        f"{preseason_model_version}-post-research",
        f"{preseason_model_version}-post-news-v2-post-research",
    )
    row = database.connection.execute(
        """
        SELECT projection_runs.id, projection_runs.model_version,
               projection_runs.generated_at,
               projection_runs.horizon_gameweeks
        FROM projection_runs
        JOIN seasons ON seasons.id = projection_runs.season_id
        WHERE seasons.code = ?
          AND projection_runs.start_gameweek = ?
          AND projection_runs.horizon_gameweeks >= ?
          AND projection_runs.model_version IN (?, ?, ?, ?)
        ORDER BY datetime(projection_runs.generated_at) DESC,
                 projection_runs.id DESC
        LIMIT 1
        """,
        (
            season_code,
            start_gameweek,
            minimum_horizon_gameweeks,
            *allowed_versions,
        ),
    ).fetchone()
    if row is None:
        return None
    return ProductionProjectionRun(
        run_id=int(row["id"]),
        model_version=str(row["model_version"]),
        generated_at=str(row["generated_at"]),
        horizon_gameweeks=int(row["horizon_gameweeks"]),
    )


def select_decision_projection_run(
    database: HistoricalDatabase,
    *,
    season_code: str,
    start_gameweek: int,
    minimum_horizon_gameweeks: int,
    preseason_model_validated: bool = False,
) -> tuple[ProductionProjectionRun | None, str]:
    """Pick the run for one decision, and say which decision it was.

    Returns the run and the decision context that chose it, so a caller can
    never report a preseason forecast as an in-season one. The preseason route
    is taken only at GW1 and only when a validation artifact has authorised
    it; otherwise the ordinary incumbent rules apply unchanged.
    """

    if start_gameweek == PRESEASON_GAMEWEEK and preseason_model_validated:
        preseason = select_preseason_projection_run(
            database,
            season_code=season_code,
            start_gameweek=start_gameweek,
            minimum_horizon_gameweeks=minimum_horizon_gameweeks,
        )
        if preseason is not None:
            return preseason, "preseason_opening_squad"
    return (
        select_production_projection_run(
            database,
            season_code=season_code,
            start_gameweek=start_gameweek,
            minimum_horizon_gameweeks=minimum_horizon_gameweeks,
        ),
        "in_season_live_projection",
    )
