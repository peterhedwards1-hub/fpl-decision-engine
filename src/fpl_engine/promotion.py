"""Immutable forward-candidate declarations and two-tier promotion gates."""

from __future__ import annotations

import hashlib
import json
import math
import random
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from .backtest import ProjectionBacktester
from .config import SeasonRules
from .declaration import ModelDeclaration
from .evaluation import (
    evaluate_legal_squad_regret,
    evaluate_owned_captain_regret,
    evaluate_transfer_regret,
)
from .history.database import HistoricalDatabase
from .projections import MODEL_VERSION, ProjectionModelConfig


@dataclass(frozen=True)
class PromotionGatePolicy:
    forward_season: str = "2026-27"
    minimum_samples: int = 1_000
    minimum_position_samples: int = 250
    bootstrap_samples: int = 2_000
    moving_block_gameweeks: int = 3
    rmse_change_maximum: float = 0.0
    rmse_change_ci95_maximum: float = 0.0
    absolute_bias_change_maximum: float = 0.02
    position_rmse_regression_maximum: float = 0.02
    probability_brier_regression_maximum: float = 0.002
    probability_log_loss_regression_maximum: float = 0.002
    global_top_one_regret_change_maximum: float = 0.0
    top_15_regret_change_maximum: float = 0.0
    legal_squad_regret_change_maximum: float = 0.0
    owned_captain_regret_change_maximum: float = 0.0
    transfer_regret_change_maximum: float = 0.0


@dataclass(frozen=True)
class DecisionGateEvidence:
    legal_squad_regret_change: float
    owned_captain_regret_change: float
    transfer_regret_change: float
    source_report: str


@dataclass(frozen=True)
class ForwardCandidateRunPair:
    """Matched incumbent and challenger runs over one identical forward scope."""

    candidate_key: str
    season_code: str
    origin_gameweek_start: int
    origin_gameweek_end: int
    horizon_gameweeks: int
    evidence_policy: str
    incumbent_run_id: int
    incumbent_model_version: str
    challenger_run_id: int
    challenger_model_version: str
    model_config_sha256: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def register_forward_candidate(
    database: HistoricalDatabase,
    *,
    candidate_key: str,
    season_code: str,
    model_version: str,
    model_config: dict[str, Any],
    gate_policy: PromotionGatePolicy | None = None,
    registered_at: datetime | None = None,
) -> dict[str, Any]:
    """Persist an immutable model and gate declaration before outcomes."""

    policy = gate_policy or PromotionGatePolicy()
    if not candidate_key.strip():
        raise ValueError("Candidate key cannot be empty")
    if season_code < policy.forward_season:
        raise ValueError("Candidates can only be registered for forward seasons")
    season = database.connection.execute(
        "SELECT id FROM seasons WHERE code = ?",
        (season_code,),
    ).fetchone()
    if season is None:
        raise ValueError(f"Season {season_code!r} is unavailable")
    created = registered_at or datetime.now(UTC)
    if created.tzinfo is None:
        raise ValueError("Registration time must be timezone-aware")
    config_json = json.dumps(model_config, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(config_json.encode("utf-8")).hexdigest()
    policy_json = json.dumps(asdict(policy), sort_keys=True)
    with database.transaction():
        database.connection.execute(
            """
            INSERT INTO model_candidate_registrations (
                candidate_key, season_id, model_version, registered_at,
                model_config_json, model_config_sha256, gate_policy_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                candidate_key,
                int(season["id"]),
                model_version,
                created.astimezone(UTC).isoformat(),
                config_json,
                digest,
                policy_json,
            ),
        )
    return {
        "candidate_key": candidate_key,
        "season_code": season_code,
        "model_version": model_version,
        "registered_at": created.astimezone(UTC).isoformat(),
        "model_config_sha256": digest,
        "gate_policy": asdict(policy),
        "status": "declared",
    }


def load_forward_candidate(
    database: HistoricalDatabase,
    candidate_key: str,
) -> dict[str, Any]:
    """Return the immutable declaration for a candidate still awaiting outcomes."""

    registration = database.connection.execute(
        """
        SELECT registrations.*, seasons.code AS season_code
        FROM model_candidate_registrations registrations
        JOIN seasons ON seasons.id = registrations.season_id
        WHERE registrations.candidate_key = ?
        """,
        (candidate_key,),
    ).fetchone()
    if registration is None:
        raise ValueError(f"Candidate {candidate_key!r} is not registered")
    if registration["status"] != "declared":
        raise ValueError(f"Candidate is already {registration['status']}")
    return {
        "candidate_key": candidate_key,
        "season_code": str(registration["season_code"]),
        "model_version": str(registration["model_version"]),
        "registered_at": str(registration["registered_at"]),
        "model_config": json.loads(registration["model_config_json"]),
        "model_config_sha256": str(registration["model_config_sha256"]),
        "gate_policy": json.loads(registration["gate_policy_json"]),
    }


def declared_challenger_declaration(
    declaration: dict[str, Any],
) -> ModelDeclaration:
    """Rebuild the whole declared model, refusing anything the gate would reject.

    `evaluate_forward_candidate` compares a run's persisted configuration to
    the declaration key by key. A declaration that no longer round-trips can
    never qualify, so fail here rather than after the backtest has been spent.

    This rebuilds the team-strength settings and the contextual-adjustment
    manifest as well as the projection config, because those also decide the
    forecast and are therefore also part of what was preregistered.
    """

    declared = declaration["model_config"]
    try:
        model = ModelDeclaration.from_dict(declared)
    except (TypeError, ValueError) as error:
        raise ValueError(
            f"Declared model for {declaration['candidate_key']!r} does not "
            f"rebuild: {error}"
        ) from error
    rebuilt = model.as_dict()
    if rebuilt != declared:
        divergent = sorted(
            set(rebuilt) ^ set(declared)
            | {key for key in set(rebuilt) & set(declared) if rebuilt[key] != declared[key]}
        )
        raise ValueError(
            "Declared model no longer round-trips; the promotion gate would "
            f"reject every run. Divergent fields: {', '.join(divergent)}"
        )
    return model


def declared_challenger_config(
    declaration: dict[str, Any],
) -> ProjectionModelConfig:
    """The projection config alone, for callers that need only that."""

    return declared_challenger_declaration(declaration).model_config


def run_forward_candidate_pair(
    database: HistoricalDatabase,
    rules: SeasonRules,
    *,
    candidate_key: str,
    incumbent_config: ProjectionModelConfig,
    incumbent_model_version: str = MODEL_VERSION,
    origin_gameweek_start: int,
    origin_gameweek_end: int,
    horizon_gameweeks: int = 1,
    created_at: datetime | None = None,
) -> ForwardCandidateRunPair:
    """Produce the matched run pair `evaluate_forward_candidate` requires.

    The challenger runs the declaration verbatim; the incumbent runs the declared
    control over an identical scope. Both are forced to pre-deadline evidence, so
    the pair cannot fail the gate's scope, version or configuration checks for
    reasons of operator error.
    """

    declaration = load_forward_candidate(database, candidate_key)
    season_code = declaration["season_code"]
    if rules.season != season_code:
        raise ValueError(
            f"Rules season {rules.season!r} does not match candidate season {season_code!r}"
        )
    if incumbent_model_version == declaration["model_version"]:
        raise ValueError(
            "Incumbent and challenger runs must use different model versions"
        )
    challenger = declared_challenger_declaration(declaration)
    challenger_config = challenger.model_config
    if asdict(incumbent_config) == asdict(challenger_config):
        raise ValueError(
            "Incumbent control is identical to the declared candidate; the pair "
            "would measure nothing"
        )
    scope = {
        "season_code": season_code,
        "origin_gameweek_start": origin_gameweek_start,
        "origin_gameweek_end": origin_gameweek_end,
        "horizon_gameweeks": horizon_gameweeks,
        "evidence_policy": "pre_deadline_only",
        "created_at": created_at,
    }
    incumbent = ProjectionBacktester(
        database,
        rules,
        config=incumbent_config,
        model_version=incumbent_model_version,
    ).run(**scope)
    challenger_run = ProjectionBacktester(
        database,
        rules,
        config=challenger_config,
        model_version=declaration["model_version"],
        team_strength_settings=challenger.team_strength_settings,
        team_strength_adjustments=challenger.contextual_adjustments,
    ).run(**scope)
    return ForwardCandidateRunPair(
        candidate_key=candidate_key,
        season_code=season_code,
        origin_gameweek_start=origin_gameweek_start,
        origin_gameweek_end=origin_gameweek_end,
        horizon_gameweeks=horizon_gameweeks,
        evidence_policy="pre_deadline_only",
        incumbent_run_id=incumbent.backtest_run_id,
        incumbent_model_version=incumbent_model_version,
        challenger_run_id=challenger_run.backtest_run_id,
        challenger_model_version=declaration["model_version"],
        model_config_sha256=declaration["model_config_sha256"],
    )


def build_decision_gate_evidence(
    database: HistoricalDatabase,
    rules: SeasonRules,
    *,
    incumbent_run_id: int,
    challenger_run_id: int,
    method: str = "model",
    max_transfers_per_week: int = 1,
) -> DecisionGateEvidence:
    """Measure every decision gate from a matched run pair.

    All three changes are now derived: legal-squad regret from replayed squad
    selection, owned-captain regret from the armband available in that squad,
    and transfer regret from the same-state one-Gameweek comparison. Nothing
    here is supplied by an operator, so the gate cannot be passed on an
    asserted number.
    """

    incumbent = evaluate_legal_squad_regret(
        database,
        incumbent_run_id,
        rules,
        methods=(method,),
    )
    challenger = evaluate_legal_squad_regret(
        database,
        challenger_run_id,
        rules,
        methods=(method,),
    )
    incumbent_origins = {origin.origin_gameweek for origin in incumbent.origins}
    challenger_origins = {origin.origin_gameweek for origin in challenger.origins}
    if incumbent_origins != challenger_origins:
        raise ValueError(
            "Decision evidence requires both runs to cover identical origins"
        )
    change = (
        challenger.mean_regret_by_method[method] - incumbent.mean_regret_by_method[method]
    )
    incumbent_captain = evaluate_owned_captain_regret(
        database,
        incumbent_run_id,
        rules,
        method=method,
    )
    challenger_captain = evaluate_owned_captain_regret(
        database,
        challenger_run_id,
        rules,
        method=method,
    )
    if incumbent_captain.samples != challenger_captain.samples:
        raise ValueError(
            "Captain evidence requires both runs to cover identical Gameweeks"
        )
    captain_change = (
        challenger_captain.mean_regret - incumbent_captain.mean_regret
    )
    incumbent_transfer = evaluate_transfer_regret(
        database,
        incumbent_run_id,
        rules,
        max_transfers_per_week=max_transfers_per_week,
    )
    challenger_transfer = evaluate_transfer_regret(
        database,
        challenger_run_id,
        rules,
        max_transfers_per_week=max_transfers_per_week,
    )
    if incumbent_transfer.gameweeks != challenger_transfer.gameweeks:
        raise ValueError(
            "Transfer evidence requires both runs to cover identical Gameweeks"
        )
    transfer_change = (
        challenger_transfer.same_state_mean_regret
        - incumbent_transfer.same_state_mean_regret
    )
    return DecisionGateEvidence(
        legal_squad_regret_change=round(change, 6),
        owned_captain_regret_change=round(captain_change, 6),
        transfer_regret_change=round(transfer_change, 6),
        source_report=(
            f"legal-squad, owned-captain and same-state transfer regret "
            f"method={method} incumbent_run={incumbent_run_id} "
            f"challenger_run={challenger_run_id} "
            f"origins={len(incumbent_origins)} "
            f"captain_decisions={incumbent_captain.samples} "
            f"transfer_decisions={incumbent_transfer.decisions}"
        ),
    )


def evaluate_forward_candidate(
    database: HistoricalDatabase,
    *,
    candidate_key: str,
    incumbent_run_ids: tuple[int, ...],
    challenger_run_ids: tuple[int, ...],
    decision_evidence: DecisionGateEvidence,
    seed: int = 20260730,
    finalize_failure: bool = False,
) -> dict[str, Any]:
    """Evaluate matched forward rows; never use historical design folds."""

    registration = database.connection.execute(
        """
        SELECT registrations.*, seasons.code AS season_code
        FROM model_candidate_registrations registrations
        JOIN seasons ON seasons.id = registrations.season_id
        WHERE registrations.candidate_key = ?
        """,
        (candidate_key,),
    ).fetchone()
    if registration is None:
        raise ValueError(f"Candidate {candidate_key!r} is not registered")
    if registration["status"] != "declared":
        raise ValueError(f"Candidate is already {registration['status']}")
    policy = PromotionGatePolicy(**json.loads(registration["gate_policy_json"]))
    if not incumbent_run_ids or len(incumbent_run_ids) != len(challenger_run_ids):
        raise ValueError("Matched incumbent and challenger runs are required")
    incumbent_scope = _run_scope(database, incumbent_run_ids)
    challenger_scope = _run_scope(database, challenger_run_ids)
    pairs = []
    registered_at = str(registration["registered_at"])
    registered_config = json.loads(registration["model_config_json"])
    for incumbent_id, challenger_id in zip(incumbent_run_ids, challenger_run_ids, strict=True):
        incumbent = incumbent_scope[incumbent_id]
        challenger = challenger_scope[challenger_id]
        comparable = (
            incumbent["season_code"] == challenger["season_code"]
            and incumbent["origin_gameweek_start"] == challenger["origin_gameweek_start"]
            and incumbent["origin_gameweek_end"] == challenger["origin_gameweek_end"]
            and incumbent["horizon_gameweeks"] == challenger["horizon_gameweeks"]
        )
        if not comparable:
            raise ValueError("Backtest run pairs must have identical scopes")
        if challenger["season_code"] < policy.forward_season:
            raise ValueError("Historical design folds cannot qualify a model")
        if challenger["evidence_policy"] != "pre_deadline_only":
            raise ValueError("Promotion requires pre-deadline-only evidence")
        if challenger["model_version"] != registration["model_version"]:
            raise ValueError("Challenger model version differs from declaration")
        if json.loads(challenger["model_config_json"]) != registered_config:
            raise ValueError("Challenger configuration differs from declaration")
        pairs.append((incumbent_id, challenger_id))
    rows = _paired_rows(database, pairs, registered_at)
    if not rows:
        raise ValueError("No post-registration matched forward outcomes exist")

    overall = _forecast_metrics(rows)
    position = {
        value: _forecast_metrics([row for row in rows if row["position"] == value])
        for value in ("GK", "DEF", "MID", "FWD")
        if sum(row["position"] == value for row in rows) >= policy.minimum_position_samples
    }
    interval = _moving_block_rmse_interval(
        rows,
        samples=policy.bootstrap_samples,
        block_length=policy.moving_block_gameweeks,
        seed=seed,
    )
    probability = _probability_metrics(rows)
    ranking = _ranking_metrics(rows)
    has_probability_evidence = probability["samples"] > 0
    gates = {
        "minimum_samples": len(rows) >= policy.minimum_samples,
        "overall_rmse": (overall["rmse_change"] <= policy.rmse_change_maximum),
        "rmse_interval": (interval["ci95_high"] <= policy.rmse_change_ci95_maximum),
        "absolute_bias": (overall["absolute_bias_change"] <= policy.absolute_bias_change_maximum),
        "every_position_rmse": (
            len(position) == 4
            and all(
                metric["rmse_change"] <= policy.position_rmse_regression_maximum
                for metric in position.values()
            )
        ),
        "probability_evidence": has_probability_evidence,
        "appearance_brier": (
            has_probability_evidence
            and probability["appearance_brier_change"]
            <= policy.probability_brier_regression_maximum
        ),
        "appearance_log_loss": (
            has_probability_evidence
            and probability["appearance_log_loss_change"]
            <= policy.probability_log_loss_regression_maximum
        ),
        "sixty_brier": (
            has_probability_evidence
            and probability["sixty_brier_change"]
            <= policy.probability_brier_regression_maximum
        ),
        "sixty_log_loss": (
            has_probability_evidence
            and probability["sixty_log_loss_change"]
            <= policy.probability_log_loss_regression_maximum
        ),
        "global_top_one_regret": (
            ranking["global_top_one_regret_change"] <= policy.global_top_one_regret_change_maximum
        ),
        "top_15_regret": (
            ranking["unconstrained_top_15_regret_change"] <= policy.top_15_regret_change_maximum
        ),
        "legal_squad_regret": (
            decision_evidence.legal_squad_regret_change <= policy.legal_squad_regret_change_maximum
        ),
        "owned_captain_regret": (
            decision_evidence.owned_captain_regret_change
            <= policy.owned_captain_regret_change_maximum
        ),
        "transfer_regret": (
            decision_evidence.transfer_regret_change <= policy.transfer_regret_change_maximum
        ),
    }
    passed = all(gates.values())
    report = {
        "schema_version": 1,
        "candidate_key": candidate_key,
        "season_code": registration["season_code"],
        "registered_at": registered_at,
        "model_config_sha256": registration["model_config_sha256"],
        "samples": len(rows),
        "forecast": overall,
        "by_position": position,
        "paired_moving_block_bootstrap": interval,
        "probability": probability,
        "ranking": ranking,
        "decision": asdict(decision_evidence),
        "gates": gates,
        "passed": passed,
        "status": ("qualified" if passed else "rejected" if finalize_failure else "declared"),
        "note": (
            "Historical seasons are structurally rejected. Only matched "
            "post-registration, pre-deadline forward rows are scored."
        ),
    }
    if passed or finalize_failure:
        status = "qualified" if passed else "rejected"
        with database.transaction():
            database.connection.execute(
                """
                UPDATE model_candidate_registrations
                SET status = ?, evaluated_at = ?,
                    evaluation_report_json = ?
                WHERE candidate_key = ?
                """,
                (
                    status,
                    datetime.now(UTC).isoformat(),
                    json.dumps(report, sort_keys=True),
                    candidate_key,
                ),
            )
    return report


def _run_scope(
    database: HistoricalDatabase,
    run_ids: tuple[int, ...],
) -> dict[int, dict[str, Any]]:
    placeholders = ",".join("?" for _ in run_ids)
    rows = database.connection.execute(
        f"""
        SELECT runs.*, seasons.code AS season_code
        FROM projection_backtest_runs runs
        JOIN seasons ON seasons.id = runs.season_id
        WHERE runs.id IN ({placeholders})
        """,
        run_ids,
    ).fetchall()
    result = {int(row["id"]): dict(row) for row in rows}
    if set(run_ids) != set(result):
        raise ValueError("One or more backtest runs are unavailable")
    if any(row["status"] != "completed" for row in result.values()):
        raise ValueError("Promotion requires completed backtest runs")
    return result


def _paired_rows(
    database: HistoricalDatabase,
    pairs: list[tuple[int, int]],
    registered_at: str,
) -> list[dict[str, Any]]:
    result = []
    for incumbent_id, challenger_id in pairs:
        rows = database.connection.execute(
            """
            SELECT seasons.code AS season_code,
                   incumbent.origin_gameweek,
                   incumbent.target_gameweek,
                   incumbent.player_season_id,
                   player_seasons.position,
                   incumbent.fixture_count,
                   incumbent.actual_minutes,
                   incumbent.actual_points,
                   incumbent.expected_points AS incumbent_points,
                   challenger.expected_points AS challenger_points,
                   incumbent.appearance_probability AS incumbent_appearance,
                   challenger.appearance_probability AS challenger_appearance,
                   incumbent.sixty_probability AS incumbent_sixty,
                   challenger.sixty_probability AS challenger_sixty
            FROM projection_backtest_predictions incumbent
            JOIN projection_backtest_predictions challenger
              ON challenger.origin_gameweek = incumbent.origin_gameweek
             AND challenger.target_gameweek = incumbent.target_gameweek
             AND challenger.player_season_id = incumbent.player_season_id
            JOIN projection_backtest_runs runs
              ON runs.id = incumbent.backtest_run_id
            JOIN seasons ON seasons.id = runs.season_id
            JOIN gameweeks
              ON gameweeks.season_id = seasons.id
             AND gameweeks.number = incumbent.target_gameweek
            JOIN player_seasons
              ON player_seasons.id = incumbent.player_season_id
            WHERE incumbent.backtest_run_id = ?
              AND challenger.backtest_run_id = ?
              AND datetime(gameweeks.deadline_time) > datetime(?)
              AND incumbent.actual_points = challenger.actual_points
              AND incumbent.actual_minutes = challenger.actual_minutes
            """,
            (incumbent_id, challenger_id, registered_at),
        ).fetchall()
        result.extend(dict(row) for row in rows)
    return result


def _forecast_metrics(rows: list[dict[str, Any]]) -> dict[str, float | int]:
    incumbent_errors = [
        float(row["actual_points"]) - float(row["incumbent_points"]) for row in rows
    ]
    challenger_errors = [
        float(row["actual_points"]) - float(row["challenger_points"]) for row in rows
    ]
    incumbent_rmse = _rmse(incumbent_errors)
    challenger_rmse = _rmse(challenger_errors)
    incumbent_bias = sum(incumbent_errors) / len(rows)
    challenger_bias = sum(challenger_errors) / len(rows)
    return {
        "samples": len(rows),
        "incumbent_rmse": round(incumbent_rmse, 6),
        "challenger_rmse": round(challenger_rmse, 6),
        "rmse_change": round(challenger_rmse - incumbent_rmse, 6),
        "incumbent_bias": round(incumbent_bias, 6),
        "challenger_bias": round(challenger_bias, 6),
        "absolute_bias_change": round(abs(challenger_bias) - abs(incumbent_bias), 6),
    }


def _probability_metrics(rows: list[dict[str, Any]]) -> dict[str, float | int]:
    eligible = [row for row in rows if int(row["fixture_count"]) == 1]
    appeared = [int(row["actual_minutes"]) > 0 for row in eligible]
    sixty = [int(row["actual_minutes"]) >= 60 for row in eligible]
    result: dict[str, float | int] = {"samples": len(eligible)}
    if not eligible:
        for name in ("appearance", "sixty"):
            result[f"incumbent_{name}_brier"] = 0.0
            result[f"challenger_{name}_brier"] = 0.0
            result[f"{name}_brier_change"] = 0.0
            result[f"incumbent_{name}_log_loss"] = 0.0
            result[f"challenger_{name}_log_loss"] = 0.0
            result[f"{name}_log_loss_change"] = 0.0
        return result
    for name, actual in (("appearance", appeared), ("sixty", sixty)):
        incumbent = [float(row[f"incumbent_{name}"]) for row in eligible]
        challenger = [float(row[f"challenger_{name}"]) for row in eligible]
        incumbent_brier = _brier(incumbent, actual)
        challenger_brier = _brier(challenger, actual)
        incumbent_loss = _log_loss(incumbent, actual)
        challenger_loss = _log_loss(challenger, actual)
        result[f"incumbent_{name}_brier"] = incumbent_brier
        result[f"challenger_{name}_brier"] = challenger_brier
        result[f"{name}_brier_change"] = round(challenger_brier - incumbent_brier, 6)
        result[f"incumbent_{name}_log_loss"] = incumbent_loss
        result[f"challenger_{name}_log_loss"] = challenger_loss
        result[f"{name}_log_loss_change"] = round(challenger_loss - incumbent_loss, 6)
    return result


def _ranking_metrics(rows: list[dict[str, Any]]) -> dict[str, float]:
    groups: dict[tuple[str, int, int], list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(
            (
                str(row["season_code"]),
                int(row["origin_gameweek"]),
                int(row["target_gameweek"]),
            ),
            [],
        ).append(row)
    values: dict[str, list[float]] = {
        "incumbent_top_one": [],
        "challenger_top_one": [],
        "incumbent_top_15": [],
        "challenger_top_15": [],
    }
    for group in groups.values():
        actual = sorted(
            group,
            key=lambda row: (
                -float(row["actual_points"]),
                int(row["player_season_id"]),
            ),
        )
        for name in ("incumbent", "challenger"):
            forecast = sorted(
                group,
                key=lambda row: (
                    -float(row[f"{name}_points"]),
                    int(row["player_season_id"]),
                ),
            )
            values[f"{name}_top_one"].append(
                float(actual[0]["actual_points"]) - float(forecast[0]["actual_points"])
            )
            values[f"{name}_top_15"].append(
                sum(float(row["actual_points"]) for row in actual[:15])
                - sum(float(row["actual_points"]) for row in forecast[:15])
            )
    means = {name: sum(value) / len(value) for name, value in values.items()}
    return {
        **{name: round(value, 6) for name, value in means.items()},
        "global_top_one_regret_change": round(
            means["challenger_top_one"] - means["incumbent_top_one"],
            6,
        ),
        "unconstrained_top_15_regret_change": round(
            means["challenger_top_15"] - means["incumbent_top_15"],
            6,
        ),
    }


def _moving_block_rmse_interval(
    rows: list[dict[str, Any]],
    *,
    samples: int,
    block_length: int,
    seed: int,
) -> dict[str, float | int]:
    if samples < 100:
        raise ValueError("At least 100 bootstrap samples are required")
    if block_length <= 0:
        raise ValueError("Block length must be positive")
    by_season: dict[str, dict[int, list[dict[str, Any]]]] = {}
    for row in rows:
        by_season.setdefault(str(row["season_code"]), {}).setdefault(
            int(row["target_gameweek"]), []
        ).append(row)
    blocks = []
    for season, gameweeks in by_season.items():
        ordered = sorted(gameweeks)
        for index in range(len(ordered)):
            selected = [
                ordered[(index + offset) % len(ordered)]
                for offset in range(min(block_length, len(ordered)))
            ]
            block_rows = [row for gameweek in selected for row in gameweeks[gameweek]]
            blocks.append((season, block_rows))
    rng = random.Random(seed)
    differences = []
    draw_count = max(1, math.ceil(len(by_season) * 38 / block_length))
    for _ in range(samples):
        sampled = [rng.choice(blocks)[1] for _ in range(draw_count)]
        flat = [row for block in sampled for row in block]
        differences.append(float(_forecast_metrics(flat)["rmse_change"]))
    differences.sort()
    return {
        "samples": samples,
        "block_length_gameweeks": block_length,
        "ci80_low": _quantile(differences, 0.10),
        "ci80_high": _quantile(differences, 0.90),
        "ci95_low": _quantile(differences, 0.025),
        "ci95_high": _quantile(differences, 0.975),
    }


def _rmse(errors: list[float]) -> float:
    return math.sqrt(sum(error**2 for error in errors) / len(errors))


def _brier(predicted: list[float], actual: list[bool]) -> float:
    return round(
        sum(
            (prediction - int(outcome)) ** 2
            for prediction, outcome in zip(predicted, actual, strict=True)
        )
        / len(actual),
        6,
    )


def _log_loss(predicted: list[float], actual: list[bool]) -> float:
    return round(
        -sum(
            int(outcome) * math.log(min(1 - 1e-9, max(1e-9, prediction)))
            + (1 - int(outcome)) * math.log(1 - min(1 - 1e-9, max(1e-9, prediction)))
            for prediction, outcome in zip(predicted, actual, strict=True)
        )
        / len(actual),
        6,
    )


def _quantile(values: list[float], probability: float) -> float:
    index = probability * (len(values) - 1)
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return round(values[lower], 6)
    fraction = index - lower
    return round(
        values[lower] * (1 - fraction) + values[upper] * fraction,
        6,
    )
