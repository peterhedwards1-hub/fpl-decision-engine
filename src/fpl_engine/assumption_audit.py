"""Predeclared football-assumption ablations on rolling development folds."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from .backtest import (
    BacktestReport,
    ProjectionBacktester,
    load_backtest_report,
)
from .config import SeasonRules
from .history.database import HistoricalDatabase
from .learned_challenger import (
    ChallengerMetrics,
    LearnedChallengerReport,
    train_and_evaluate_learned_challenger,
)
from .projections import ROBUST_V4_MODEL_CONFIG, ProjectionModelConfig
from .tuning import ROLLING_STABILITY_WEIGHT, tuning_objective

AUDIT_VERSION = "football-assumptions-v1"
RECENT_SCORING_WEIGHT = 3.0
LEARNED_LOSSES = ("absolute_error", "squared_error", "poisson")


@dataclass(frozen=True)
class DecisionMetrics:
    origin_targets: int
    captain_regret: float
    unconstrained_top_15_regret: float


@dataclass(frozen=True)
class SeasonAuditMetrics:
    season_code: str
    backtest_run_id: int
    samples: int
    top_100_samples: int
    objective: float
    overall_points_mae: float
    overall_points_bias: float
    overall_minutes_mae: float
    top_100_points_mae: float
    top_100_points_bias: float
    top_100_actual_points_mean: float
    captain_regret: float
    unconstrained_top_15_regret: float
    by_position: dict[str, dict[str, float | int]]


@dataclass(frozen=True)
class VariantAudit:
    name: str
    changed_assumption: str
    config: ProjectionModelConfig
    seasons: tuple[SeasonAuditMetrics, ...]
    development_score: float
    cross_season_stability_penalty: float
    aggregate: dict[str, Any]
    change_vs_reference: dict[str, float]
    passes_development_gate: bool


@dataclass(frozen=True)
class LearnedLossAudit:
    loss: str
    estimand: str
    folds: tuple[LearnedChallengerReport, ...]
    aggregate: dict[str, float]
    change_vs_absolute_error: dict[str, float]
    passes_mean_target_gate: bool


@dataclass(frozen=True)
class AssumptionAuditReport:
    audit_version: str
    development_seasons: tuple[str, ...]
    origin_gameweek_start: int
    origin_gameweek_end: int
    horizon_gameweeks: int
    variants: tuple[VariantAudit, ...]
    learned_losses: tuple[LearnedLossAudit, ...]
    limitations: tuple[str, ...]
    output_path: str

    def as_dict(self) -> dict[str, object]:
        return {
            "audit_version": self.audit_version,
            "development_seasons": self.development_seasons,
            "origin_gameweek_start": self.origin_gameweek_start,
            "origin_gameweek_end": self.origin_gameweek_end,
            "horizon_gameweeks": self.horizon_gameweeks,
            "variants": [
                {
                    **asdict(variant),
                    "config": asdict(variant.config),
                    "seasons": [
                        asdict(season) for season in variant.seasons
                    ],
                }
                for variant in self.variants
            ],
            "learned_losses": [
                {
                    "loss": result.loss,
                    "estimand": result.estimand,
                    "folds": [fold.as_dict() for fold in result.folds],
                    "aggregate": result.aggregate,
                    "change_vs_absolute_error": (
                        result.change_vs_absolute_error
                    ),
                    "passes_mean_target_gate": (
                        result.passes_mean_target_gate
                    ),
                }
                for result in self.learned_losses
            ],
            "limitations": self.limitations,
            "output_path": self.output_path,
        }


def run_assumption_audit(
    database: HistoricalDatabase,
    rules_by_season: dict[str, SeasonRules],
    *,
    development_seasons: tuple[str, ...],
    origin_gameweek_start: int = 2,
    origin_gameweek_end: int = 38,
    horizon_gameweeks: int = 1,
    output_path: str | Path = "data/models/assumption-audit-v1.json",
    artifact_directory: str | Path = "data/models/assumption-audit",
    seed: int = 20260729,
) -> AssumptionAuditReport:
    """Run isolated football-assumption variants without a holdout query."""

    if len(development_seasons) < 2:
        raise ValueError(
            "Assumption audit requires at least two development seasons"
        )
    if set(rules_by_season) != set(development_seasons):
        raise ValueError(
            "Rules must match the assumption-audit development seasons"
        )
    if not 1 <= origin_gameweek_start <= origin_gameweek_end <= 38:
        raise ValueError("Audit Gameweeks must be within 1–38")
    if horizon_gameweeks <= 0:
        raise ValueError("Audit horizon must be positive")

    variants = _projection_variants()
    variant_seasons: dict[str, tuple[SeasonAuditMetrics, ...]] = {}
    for name, config in variants:
        season_results = []
        for season_code in development_seasons:
            model_version = f"{AUDIT_VERSION}-{name}"
            report = _existing_report(
                database,
                season_code=season_code,
                model_version=model_version,
                config=config,
                origin_gameweek_start=origin_gameweek_start,
                origin_gameweek_end=origin_gameweek_end,
                horizon_gameweeks=horizon_gameweeks,
            )
            if report is None:
                report = ProjectionBacktester(
                    database,
                    rules_by_season[season_code],
                    config=config,
                    model_version=model_version,
                ).run(
                    season_code=season_code,
                    origin_gameweek_start=origin_gameweek_start,
                    origin_gameweek_end=origin_gameweek_end,
                    horizon_gameweeks=horizon_gameweeks,
                    evidence_policy="performance_only",
                )
            season_results.append(
                _season_metrics(database, report)
            )
        variant_seasons[name] = tuple(season_results)

    reference_aggregate = _aggregate_seasons(
        variant_seasons["reference"]
    )
    audited_variants = tuple(
        _variant_audit(
            name,
            config,
            variant_seasons[name],
            reference_aggregate,
        )
        for name, config in variants
    )

    artifact_root = Path(artifact_directory)
    reference_runs = {
        result.season_code: result.backtest_run_id
        for result in variant_seasons["reference"]
    }
    loss_folds: dict[str, tuple[LearnedChallengerReport, ...]] = {}
    for loss in LEARNED_LOSSES:
        folds = []
        for validation_index in range(1, len(development_seasons)):
            validation_season = development_seasons[validation_index]
            training_seasons = development_seasons[:validation_index]
            folds.append(
                train_and_evaluate_learned_challenger(
                    database,
                    training_run_ids=tuple(
                        reference_runs[season]
                        for season in training_seasons
                    ),
                    validation_run_id=reference_runs[validation_season],
                    artifact_path=(
                        artifact_root
                        / f"{loss}-through-{validation_season}.joblib"
                    ),
                    seed=seed,
                    loss=loss,
                )
            )
        loss_folds[loss] = tuple(folds)

    absolute_aggregate = _aggregate_learned(
        loss_folds["absolute_error"]
    )
    learned_losses = tuple(
        _learned_loss_audit(
            loss,
            loss_folds[loss],
            absolute_aggregate,
        )
        for loss in LEARNED_LOSSES
    )

    report = AssumptionAuditReport(
        audit_version=AUDIT_VERSION,
        development_seasons=development_seasons,
        origin_gameweek_start=origin_gameweek_start,
        origin_gameweek_end=origin_gameweek_end,
        horizon_gameweeks=horizon_gameweeks,
        variants=audited_variants,
        learned_losses=learned_losses,
        limitations=(
            "Development-fold evidence only; no previously inspected "
            "validation season is queried.",
            "Club-change resets cannot yet be isolated because historical "
            "fixture rows do not preserve a reliable per-fixture club role.",
            "Defensive-contribution threshold scoring has no effect before "
            "the rule exists and therefore needs forward 2026/27 evidence.",
            "Top-15 regret is unconstrained by budget, formation and club "
            "limits; full optimiser regret remains a later promotion gate.",
            "Historical reconstructed schedules may contain later "
            "rescheduling knowledge.",
        ),
        output_path=str(Path(output_path)),
    )
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(report.as_dict(), indent=2),
        encoding="utf-8",
    )
    return report


def _existing_report(
    database: HistoricalDatabase,
    *,
    season_code: str,
    model_version: str,
    config: ProjectionModelConfig,
    origin_gameweek_start: int,
    origin_gameweek_end: int,
    horizon_gameweeks: int,
) -> BacktestReport | None:
    source_run = database.connection.execute(
        """
        SELECT MAX(id) AS id
        FROM ingestion_runs
        WHERE status = 'completed'
        """
    ).fetchone()
    source_run_id = (
        None if source_run["id"] is None else int(source_run["id"])
    )
    row = database.connection.execute(
        """
        SELECT runs.id
        FROM projection_backtest_runs runs
        JOIN seasons ON seasons.id = runs.season_id
        WHERE seasons.code = ?
          AND runs.model_version = ?
          AND runs.origin_gameweek_start = ?
          AND runs.origin_gameweek_end = ?
          AND runs.horizon_gameweeks = ?
          AND runs.evidence_policy = 'performance_only'
          AND runs.model_config_json = ?
          AND runs.source_ingestion_run_id IS ?
          AND runs.status = 'completed'
        ORDER BY runs.id DESC
        LIMIT 1
        """,
        (
            season_code,
            model_version,
            origin_gameweek_start,
            origin_gameweek_end,
            horizon_gameweeks,
            json.dumps(asdict(config), sort_keys=True),
            source_run_id,
        ),
    ).fetchone()
    return (
        None
        if row is None
        else load_backtest_report(database, int(row["id"]))
    )


def _projection_variants(
) -> tuple[tuple[str, ProjectionModelConfig], ...]:
    reference = ROBUST_V4_MODEL_CONFIG
    position_minutes = replace(
        reference,
        minutes_allocation="position_aware",
    )
    recent_scoring = replace(
        reference,
        scoring_recent_evidence_weight=RECENT_SCORING_WEIGHT,
    )
    corrected_scoring = replace(
        reference,
        defensive_contribution_model="threshold_poisson",
        include_penalty_events=True,
    )
    combined = replace(
        corrected_scoring,
        minutes_allocation="position_aware",
        scoring_recent_evidence_weight=RECENT_SCORING_WEIGHT,
    )
    return (
        ("reference", reference),
        ("position_minutes", position_minutes),
        ("recent_scoring", recent_scoring),
        ("corrected_scoring", corrected_scoring),
        ("combined", combined),
    )


def _season_metrics(
    database: HistoricalDatabase,
    report: BacktestReport,
) -> SeasonAuditMetrics:
    top_100 = next(
        metric for metric in report.top_n if metric.value == "100"
    )
    decision = _decision_metrics(database, report.backtest_run_id)
    return SeasonAuditMetrics(
        season_code=report.season_code,
        backtest_run_id=report.backtest_run_id,
        samples=report.overall.samples,
        top_100_samples=top_100.samples,
        objective=round(tuning_objective(report), 6),
        overall_points_mae=report.overall.points_mae,
        overall_points_bias=report.overall.points_bias,
        overall_minutes_mae=report.overall.minutes_mae,
        top_100_points_mae=top_100.points_mae,
        top_100_points_bias=top_100.points_bias,
        top_100_actual_points_mean=top_100.actual_points_mean,
        captain_regret=decision.captain_regret,
        unconstrained_top_15_regret=(
            decision.unconstrained_top_15_regret
        ),
        by_position={
            metric.value: {
                "samples": metric.samples,
                "points_mae": metric.points_mae,
                "points_bias": metric.points_bias,
                "minutes_mae": metric.minutes_mae,
                "minutes_bias": metric.minutes_bias,
            }
            for metric in report.by_position
        },
    )


def _decision_metrics(
    database: HistoricalDatabase,
    run_id: int,
) -> DecisionMetrics:
    rows = database.connection.execute(
        """
        SELECT origin_gameweek, target_gameweek, player_season_id,
               expected_points, actual_points
        FROM projection_backtest_predictions
        WHERE backtest_run_id = ?
        ORDER BY origin_gameweek, target_gameweek, player_season_id
        """,
        (run_id,),
    ).fetchall()
    groups: dict[tuple[int, int], list[Any]] = {}
    for row in rows:
        groups.setdefault(
            (int(row["origin_gameweek"]), int(row["target_gameweek"])),
            [],
        ).append(row)
    captain_regret = []
    top_15_regret = []
    for group_rows in groups.values():
        predicted = sorted(
            group_rows,
            key=lambda row: (
                -float(row["expected_points"]),
                int(row["player_season_id"]),
            ),
        )
        actual = sorted(
            group_rows,
            key=lambda row: (
                -float(row["actual_points"]),
                int(row["player_season_id"]),
            ),
        )
        captain_regret.append(
            float(actual[0]["actual_points"])
            - float(predicted[0]["actual_points"])
        )
        top_15_regret.append(
            sum(float(row["actual_points"]) for row in actual[:15])
            - sum(
                float(row["actual_points"])
                for row in predicted[:15]
            )
        )
    return DecisionMetrics(
        origin_targets=len(groups),
        captain_regret=round(
            sum(captain_regret) / len(captain_regret),
            4,
        ),
        unconstrained_top_15_regret=round(
            sum(top_15_regret) / len(top_15_regret),
            4,
        ),
    )


def _aggregate_seasons(
    seasons: tuple[SeasonAuditMetrics, ...],
) -> dict[str, Any]:
    top_weight = sum(result.top_100_samples for result in seasons)
    sample_weight = sum(result.samples for result in seasons)
    objective_mean = sum(
        result.objective * result.top_100_samples
        for result in seasons
    ) / top_weight
    worst_objective = max(result.objective for result in seasons)
    stability_penalty = ROLLING_STABILITY_WEIGHT * (
        worst_objective - objective_mean
    )
    return {
        "development_score": round(
            objective_mean + stability_penalty,
            6,
        ),
        "cross_season_stability_penalty": round(
            stability_penalty,
            6,
        ),
        "overall_points_mae": _weighted(
            seasons,
            "overall_points_mae",
            "samples",
            sample_weight,
        ),
        "overall_absolute_points_bias": abs(
            _weighted(
                seasons,
                "overall_points_bias",
                "samples",
                sample_weight,
            )
        ),
        "overall_minutes_mae": _weighted(
            seasons,
            "overall_minutes_mae",
            "samples",
            sample_weight,
        ),
        "top_100_points_mae": _weighted(
            seasons,
            "top_100_points_mae",
            "top_100_samples",
            top_weight,
        ),
        "top_100_absolute_points_bias": abs(
            _weighted(
                seasons,
                "top_100_points_bias",
                "top_100_samples",
                top_weight,
            )
        ),
        "top_100_actual_points_mean": _weighted(
            seasons,
            "top_100_actual_points_mean",
            "top_100_samples",
            top_weight,
        ),
        "captain_regret": round(
            sum(result.captain_regret for result in seasons)
            / len(seasons),
            4,
        ),
        "unconstrained_top_15_regret": round(
            sum(
                result.unconstrained_top_15_regret
                for result in seasons
            )
            / len(seasons),
            4,
        ),
        "by_position": _aggregate_positions(seasons),
    }


def _variant_audit(
    name: str,
    config: ProjectionModelConfig,
    seasons: tuple[SeasonAuditMetrics, ...],
    reference: dict[str, Any],
) -> VariantAudit:
    aggregate = _aggregate_seasons(seasons)
    changes = {
        key: round(value - reference[key], 4)
        for key, value in aggregate.items()
        if key != "cross_season_stability_penalty"
        and isinstance(value, int | float)
    }
    passes = (
        name != "reference"
        and aggregate["overall_points_mae"]
        <= reference["overall_points_mae"]
        and aggregate["top_100_points_mae"]
        <= reference["top_100_points_mae"]
        and aggregate["top_100_absolute_points_bias"]
        <= reference["top_100_absolute_points_bias"]
        and aggregate["captain_regret"]
        <= reference["captain_regret"]
    )
    descriptions = {
        "reference": "Frozen robust-v4 transparent reference",
        "position_minutes": (
            "Separate 90 goalkeeper and 900 outfield expected minutes"
        ),
        "recent_scoring": (
            "Triple weight for scoring events in the recent window"
        ),
        "corrected_scoring": (
            "Threshold defensive contributions plus penalty events"
        ),
        "combined": "All implemented football-assumption corrections",
    }
    return VariantAudit(
        name=name,
        changed_assumption=descriptions[name],
        config=config,
        seasons=seasons,
        development_score=aggregate["development_score"],
        cross_season_stability_penalty=(
            aggregate["cross_season_stability_penalty"]
        ),
        aggregate=aggregate,
        change_vs_reference=changes,
        passes_development_gate=passes,
    )


def _aggregate_learned(
    folds: tuple[LearnedChallengerReport, ...],
) -> dict[str, float]:
    metrics = tuple(fold.challenger for fold in folds)
    sample_weight = sum(metric.samples for metric in metrics)
    top_weight = sum(metric.top_100_samples for metric in metrics)
    return {
        "points_mae": _weighted_metrics(
            metrics, "points_mae", "samples", sample_weight
        ),
        "absolute_points_bias": abs(
            _weighted_metrics(
                metrics, "points_bias", "samples", sample_weight
            )
        ),
        "top_100_points_mae": _weighted_metrics(
            metrics,
            "top_100_points_mae",
            "top_100_samples",
            top_weight,
        ),
        "top_100_absolute_points_bias": abs(
            _weighted_metrics(
                metrics,
                "top_100_points_bias",
                "top_100_samples",
                top_weight,
            )
        ),
        "top_100_actual_points_mean": _weighted_metrics(
            metrics,
            "top_100_actual_points_mean",
            "top_100_samples",
            top_weight,
        ),
        "captain_regret": round(
            sum(metric.captain_regret for metric in metrics)
            / len(metrics),
            4,
        ),
        "unconstrained_top_15_regret": round(
            sum(
                metric.unconstrained_top_15_regret
                for metric in metrics
            )
            / len(metrics),
            4,
        ),
    }


def _learned_loss_audit(
    loss: str,
    folds: tuple[LearnedChallengerReport, ...],
    absolute_reference: dict[str, float],
) -> LearnedLossAudit:
    aggregate = _aggregate_learned(folds)
    changes = {
        key: round(value - absolute_reference[key], 4)
        for key, value in aggregate.items()
    }
    passes = (
        loss != "absolute_error"
        and aggregate["absolute_points_bias"]
        < absolute_reference["absolute_points_bias"]
        and aggregate["top_100_absolute_points_bias"]
        < absolute_reference["top_100_absolute_points_bias"]
        and aggregate["top_100_points_mae"]
        <= absolute_reference["top_100_points_mae"]
        and aggregate["captain_regret"]
        <= absolute_reference["captain_regret"]
    )
    return LearnedLossAudit(
        loss=loss,
        estimand=(
            "conditional_median"
            if loss == "absolute_error"
            else "conditional_mean"
        ),
        folds=folds,
        aggregate=aggregate,
        change_vs_absolute_error=changes,
        passes_mean_target_gate=passes,
    )


def _aggregate_positions(
    seasons: tuple[SeasonAuditMetrics, ...],
) -> dict[str, dict[str, float | int]]:
    positions = sorted(
        {
            position
            for season in seasons
            for position in season.by_position
        }
    )
    result: dict[str, dict[str, float | int]] = {}
    for position in positions:
        position_rows = [
            season.by_position[position]
            for season in seasons
            if position in season.by_position
        ]
        samples = sum(int(row["samples"]) for row in position_rows)
        points_bias = sum(
            float(row["points_bias"]) * int(row["samples"])
            for row in position_rows
        ) / samples
        minutes_bias = sum(
            float(row["minutes_bias"]) * int(row["samples"])
            for row in position_rows
        ) / samples
        result[position] = {
            "samples": samples,
            "points_mae": round(
                sum(
                    float(row["points_mae"]) * int(row["samples"])
                    for row in position_rows
                )
                / samples,
                4,
            ),
            "points_bias": round(points_bias, 4),
            "minutes_mae": round(
                sum(
                    float(row["minutes_mae"]) * int(row["samples"])
                    for row in position_rows
                )
                / samples,
                4,
            ),
            "minutes_bias": round(minutes_bias, 4),
        }
    return result


def _weighted(
    values: tuple[SeasonAuditMetrics, ...],
    value_name: str,
    weight_name: str,
    total_weight: int,
) -> float:
    return round(
        sum(
            float(getattr(value, value_name))
            * int(getattr(value, weight_name))
            for value in values
        )
        / total_weight,
        4,
    )


def _weighted_metrics(
    values: tuple[ChallengerMetrics, ...],
    value_name: str,
    weight_name: str,
    total_weight: int,
) -> float:
    return round(
        sum(
            float(getattr(value, value_name))
            * int(getattr(value, weight_name))
            for value in values
        )
        / total_weight,
        4,
    )
