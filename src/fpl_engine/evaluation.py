"""Simple, leakage-controlled benchmarks for persisted projection backtests."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .backtest import load_backtest_report
from .config import SeasonRules
from .decision_evaluation import (
    RealisedPlayerOutcome,
    TransferReplayWeek,
    replay_transfer_continuity,
    resolve_squad_gameweek,
    score_squad_gameweek,
)
from .domain import Position
from .history.database import HistoricalDatabase
from .optimisation import (
    CandidatePlayer,
    FullSquadResult,
    GameweekPlayerValue,
    optimise_full_squad,
)
from .transfers import CurrentSquad

#: Forecast methods a regret replay can select a squad with. "model" is the
#: run's own projection; the rest are the simple baselines it must beat.
SUPPORTED_REGRET_METHODS = (
    "model",
    "season_points_per_fixture",
    "recent_4_points_per_fixture",
    "season_points_per_90_model_minutes",
    "position_points_per_fixture",
)


@dataclass(frozen=True)
class ForecastBenchmarkMetrics:
    name: str
    horizon_step: int | None
    samples: int
    points_mae: float
    points_bias: float
    points_rmse: float
    captain_regret: float
    unconstrained_top_15_regret: float


@dataclass(frozen=True)
class BaselineComparisonReport:
    backtest_run_id: int
    season_code: str
    model_version: str
    horizon_gameweeks: int
    methods: tuple[ForecastBenchmarkMetrics, ...]
    by_horizon: tuple[ForecastBenchmarkMetrics, ...]
    limitations: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "backtest_run_id": self.backtest_run_id,
            "season_code": self.season_code,
            "model_version": self.model_version,
            "horizon_gameweeks": self.horizon_gameweeks,
            "methods": [asdict(metric) for metric in self.methods],
            "by_horizon": [asdict(metric) for metric in self.by_horizon],
            "limitations": list(self.limitations),
        }

    def write_json(self, output_path: str | Path) -> Path:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.as_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return path


@dataclass(frozen=True)
class LegalSquadOriginRegret:
    method: str
    origin_gameweek: int
    target_gameweeks: tuple[int, ...]
    predicted_objective: float
    realised_points: float
    hindsight_optimal_points: float
    regret: float


@dataclass(frozen=True)
class LegalSquadRegretReport:
    backtest_run_id: int
    season_code: str
    model_version: str
    origins: tuple[LegalSquadOriginRegret, ...]
    mean_regret_by_method: dict[str, float]
    total_regret_by_method: dict[str, float]
    limitations: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "backtest_run_id": self.backtest_run_id,
            "season_code": self.season_code,
            "model_version": self.model_version,
            "origins": [asdict(origin) for origin in self.origins],
            "mean_regret_by_method": self.mean_regret_by_method,
            "total_regret_by_method": self.total_regret_by_method,
            "limitations": list(self.limitations),
        }


def build_evaluation_suite(
    database: HistoricalDatabase,
    incumbent_run_ids: tuple[int, ...],
    *,
    challenger_run_ids: tuple[int, ...] = (),
) -> dict[str, Any]:
    """Compile reproducible horizon, baseline and challenger promotion evidence."""

    if not incumbent_run_ids:
        raise ValueError("At least one incumbent backtest run is required")
    incumbent = tuple(
        _evaluation_run_summary(database, run_id)
        for run_id in incumbent_run_ids
    )
    challenger = tuple(
        _evaluation_run_summary(database, run_id)
        for run_id in challenger_run_ids
    )
    incumbent_by_season = {
        str(run["season_code"]): run for run in incumbent
    }
    comparisons = []
    for run in challenger:
        season_code = str(run["season_code"])
        baseline = incumbent_by_season.get(season_code)
        if baseline is None:
            raise ValueError(
                f"Challenger season {season_code} has no incumbent comparison"
            )
        gates = {
            "overall_points_mae": (
                float(run["overall_points_mae"])
                <= float(baseline["overall_points_mae"])
            ),
            "top_100_points_mae": (
                float(run["top_100_points_mae"])
                <= float(baseline["top_100_points_mae"])
            ),
            "top_100_absolute_bias": (
                abs(float(run["top_100_points_bias"]))
                <= abs(float(baseline["top_100_points_bias"]))
            ),
            "captain_regret": (
                float(run["captain_regret"])
                <= float(baseline["captain_regret"])
            ),
            "unconstrained_top_15_regret": (
                float(run["unconstrained_top_15_regret"])
                <= float(baseline["unconstrained_top_15_regret"])
            ),
        }
        comparisons.append(
            {
                "season_code": season_code,
                "incumbent_run_id": baseline["backtest_run_id"],
                "challenger_run_id": run["backtest_run_id"],
                "changes": {
                    key: round(
                        float(run[key]) - float(baseline[key]),
                        4,
                    )
                    for key in (
                        "overall_points_mae",
                        "top_100_points_mae",
                        "top_100_points_bias",
                        "captain_regret",
                        "unconstrained_top_15_regret",
                    )
                },
                "gates": gates,
                "passed": all(gates.values()),
            }
        )
    coverage = [
        dict(row)
        for row in database.connection.execute(
            """
            SELECT seasons.code AS season_code,
                   COUNT(stats.id) AS fixture_rows,
                   SUM(stats.expected_goals IS NOT NULL) AS xg_rows,
                   SUM(stats.expected_assists IS NOT NULL) AS xa_rows
            FROM player_fixture_stats stats
            JOIN player_seasons
              ON player_seasons.id = stats.player_season_id
            JOIN seasons ON seasons.id = player_seasons.season_id
            GROUP BY seasons.code
            ORDER BY seasons.code
            """
        )
    ]
    return {
        "incumbent_runs": list(incumbent),
        "challenger_runs": list(challenger),
        "incumbent_pooled_horizon": _pooled_horizon_metrics(
            database,
            incumbent_run_ids,
        ),
        "challenger_pooled_horizon": (
            _pooled_horizon_metrics(database, challenger_run_ids)
            if challenger_run_ids
            else []
        ),
        "challenger_comparisons": comparisons,
        "challenger_passes_development_gate": (
            bool(comparisons)
            and all(comparison["passed"] for comparison in comparisons)
        ),
        "expected_event_coverage": coverage,
        "limitations": [
            "Simple baselines and unconstrained regrets do not enforce squad legality; "
            "use evaluate-squad-regret for the expensive legal replay.",
            "A challenger must pass every per-season gate before validation is queried.",
            "Historical availability snapshots have unknown timing and cannot validate "
            "availability recovery.",
        ],
    }


def write_json_report(report: dict[str, Any], output_path: str | Path) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def compare_backtest_to_baselines(
    database: HistoricalDatabase,
    backtest_run_id: int,
) -> BaselineComparisonReport:
    """Compare a persisted forecast with transparent expanding-window baselines."""

    run, forecasts = load_backtest_benchmark_rows(
        database,
        backtest_run_id,
    )
    rows = forecasts["model"]

    methods = tuple(
        _benchmark_metrics(name, method_rows, horizon_step=None)
        for name, method_rows in forecasts.items()
    )
    horizon_steps = sorted(
        {int(row["horizon_step"]) for row in rows}
    )
    by_horizon = tuple(
        _benchmark_metrics(
            name,
            [
                row
                for row in method_rows
                if int(row["horizon_step"]) == horizon_step
            ],
            horizon_step=horizon_step,
        )
        for horizon_step in horizon_steps
        for name, method_rows in forecasts.items()
    )
    return BaselineComparisonReport(
        backtest_run_id=backtest_run_id,
        season_code=str(run["season_code"]),
        model_version=str(run["model_version"]),
        horizon_gameweeks=int(run["horizon_gameweeks"]),
        methods=methods,
        by_horizon=by_horizon,
        limitations=(
            "Baselines use only player-fixture evidence before each forecast origin.",
            "The points-per-90 baseline borrows the evaluated model's expected minutes.",
            "The field captain_regret is retained for compatibility but measures "
            "global top-one forecast regret, not captain choice from a manager squad.",
            "Top-one and top-15 regret use the common forecast sample but do not enforce "
            "budget, formation or club constraints.",
        ),
    )


def load_backtest_benchmark_rows(
    database: HistoricalDatabase,
    backtest_run_id: int,
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    """Return aligned model and leakage-controlled baseline prediction rows."""

    run_row = database.connection.execute(
        """
        SELECT runs.model_version, runs.horizon_gameweeks,
               seasons.id AS season_id, seasons.code AS season_code
        FROM projection_backtest_runs runs
        JOIN seasons ON seasons.id = runs.season_id
        WHERE runs.id = ? AND runs.status = 'completed'
        """,
        (backtest_run_id,),
    ).fetchone()
    if run_row is None:
        raise ValueError(
            f"Completed backtest run {backtest_run_id} is unavailable"
        )
    run = dict(run_row)
    rows = [
        dict(row)
        for row in database.connection.execute(
            """
            SELECT predictions.origin_gameweek,
                   predictions.target_gameweek,
                   predictions.horizon_step,
                   predictions.player_season_id,
                   predictions.fixture_count,
                   predictions.expected_minutes,
                   predictions.appearance_probability,
                   predictions.sixty_probability,
                   predictions.expected_points,
                   predictions.actual_minutes,
                   predictions.actual_points,
                   predictions.component_points_json,
                   player_seasons.position
            FROM projection_backtest_predictions predictions
            JOIN player_seasons
              ON player_seasons.id = predictions.player_season_id
            WHERE predictions.backtest_run_id = ?
            ORDER BY predictions.origin_gameweek,
                     predictions.target_gameweek,
                     predictions.player_season_id
            """,
            (backtest_run_id,),
        )
    ]
    if not rows:
        raise ValueError("Backtest has no predictions to benchmark")
    player_history, position_history = _historical_prefixes(
        database,
        int(run["season_id"]),
    )
    forecasts: dict[str, list[dict[str, Any]]] = {
        "model": [],
        "season_points_per_fixture": [],
        "recent_4_points_per_fixture": [],
        "season_points_per_90_model_minutes": [],
        "position_points_per_fixture": [],
    }
    for row in rows:
        row["backtest_run_id"] = backtest_run_id
        row["season_code"] = str(run["season_code"])
        player_id = int(row["player_season_id"])
        origin = int(row["origin_gameweek"])
        position = str(row["position"])
        player_prefix = player_history.get(player_id, _empty_prefix())
        position_prefix = position_history.get(position, _empty_prefix())
        values = _baseline_point_forecasts(
            row,
            player_prefix,
            position_prefix,
            origin,
        )
        for name, expected_points in values.items():
            forecasts[name].append(
                {
                    **row,
                    "benchmark_expected_points": expected_points,
                }
            )
    return run, forecasts


def _evaluation_run_summary(
    database: HistoricalDatabase,
    run_id: int,
) -> dict[str, Any]:
    report = load_backtest_report(database, run_id)
    comparison = compare_backtest_to_baselines(database, run_id)
    model_benchmark = next(
        metric
        for metric in comparison.methods
        if metric.name == "model"
    )
    top_100 = next(
        metric for metric in report.top_n if metric.value == "100"
    )
    return {
        "backtest_run_id": run_id,
        "season_code": report.season_code,
        "model_version": report.model_version,
        "horizon_gameweeks": report.horizon_gameweeks,
        "samples": report.overall.samples,
        "overall_points_mae": report.overall.points_mae,
        "overall_points_bias": report.overall.points_bias,
        "top_100_points_mae": top_100.points_mae,
        "top_100_points_bias": top_100.points_bias,
        "captain_regret": model_benchmark.captain_regret,
        "unconstrained_top_15_regret": (
            model_benchmark.unconstrained_top_15_regret
        ),
        "baselines": [
            asdict(metric) for metric in comparison.methods
        ],
        "by_horizon": [
            asdict(metric) for metric in report.by_horizon
        ],
    }


def _pooled_horizon_metrics(
    database: HistoricalDatabase,
    run_ids: tuple[int, ...],
) -> list[dict[str, Any]]:
    placeholders = ", ".join("?" for _ in run_ids)
    return [
        dict(row)
        for row in database.connection.execute(
            f"""
            SELECT horizon_step, COUNT(*) AS samples,
                   ROUND(
                       AVG(ABS(actual_points - expected_points)),
                       4
                   ) AS points_mae,
                   ROUND(
                       AVG(actual_points - expected_points),
                       4
                   ) AS points_bias
            FROM projection_backtest_predictions
            WHERE backtest_run_id IN ({placeholders})
            GROUP BY horizon_step
            ORDER BY horizon_step
            """,
            run_ids,
        )
    ]


def _regret_run_context(
    database: HistoricalDatabase,
    backtest_run_id: int,
    rules: SeasonRules,
) -> tuple[Any, dict[int, list[Any]], dict[int, Any], dict[str, Any]]:
    """Load a completed run, its predictions by origin, and history prefixes."""

    run = database.connection.execute(
        """
        SELECT runs.model_version, runs.source_ingestion_run_id,
               seasons.id AS season_id, seasons.code AS season_code
        FROM projection_backtest_runs runs
        JOIN seasons ON seasons.id = runs.season_id
        WHERE runs.id = ? AND runs.status = 'completed'
        """,
        (backtest_run_id,),
    ).fetchone()
    if run is None:
        raise ValueError(
            f"Completed backtest run {backtest_run_id} is unavailable"
        )
    if rules.season != str(run["season_code"]):
        raise ValueError("Regret rules must match the backtest season")
    rows = database.connection.execute(
        """
        SELECT predictions.origin_gameweek,
               predictions.target_gameweek,
               predictions.player_season_id,
               predictions.fixture_count,
               predictions.expected_minutes,
               predictions.appearance_probability,
               predictions.sixty_probability,
               predictions.expected_points,
               predictions.actual_minutes,
               predictions.actual_points
        FROM projection_backtest_predictions predictions
        WHERE predictions.backtest_run_id = ?
        ORDER BY predictions.origin_gameweek,
                 predictions.target_gameweek,
                 predictions.player_season_id
        """,
        (backtest_run_id,),
    ).fetchall()
    by_origin: dict[int, list[Any]] = {}
    for row in rows:
        by_origin.setdefault(int(row["origin_gameweek"]), []).append(row)
    player_history, position_history = _historical_prefixes(
        database,
        int(run["season_id"]),
    )
    return run, by_origin, player_history, position_history


@dataclass(frozen=True)
class _OriginCandidates:
    """Everything one origin needs, built once and shared by both measures."""

    origin_gameweek: int
    target_gameweeks: tuple[int, ...]
    candidates_by_method: dict[str, list[CandidatePlayer]]
    actual_candidates: list[CandidatePlayer]
    actual_lookup: dict[tuple[str, int], GameweekPlayerValue]


def _origin_candidate_sets(
    database: HistoricalDatabase,
    run: Any,
    by_origin: dict[int, list[Any]],
    player_history: dict[int, Any],
    position_history: dict[str, Any],
    method_names: tuple[str, ...],
):
    """Yield forecast and actual candidate sets for each replayed origin."""

    for origin_gameweek, origin_rows in by_origin.items():
        metadata = player_metadata_as_of(
            database,
            int(run["season_id"]),
            origin_gameweek,
            (
                None
                if run["source_ingestion_run_id"] is None
                else int(run["source_ingestion_run_id"])
            ),
        )
        target_gameweeks = tuple(
            sorted({int(row["target_gameweek"]) for row in origin_rows})
        )
        forecasts: dict[int, dict[int, Any]] = {}
        for row in origin_rows:
            forecasts.setdefault(
                int(row["player_season_id"]),
                {},
            )[int(row["target_gameweek"])] = row
        candidates_by_method: dict[str, list[CandidatePlayer]] = {
            method: [] for method in method_names
        }
        actual_candidates = []
        for player_season_id, player_rows in forecasts.items():
            player = metadata.get(player_season_id)
            if player is None:
                continue
            values_by_method: dict[str, list[GameweekPlayerValue]] = {
                method: [] for method in method_names
            }
            actual_values = []
            for target_gameweek in target_gameweeks:
                row = player_rows.get(target_gameweek)
                if row is None:
                    for values in values_by_method.values():
                        values.append(
                            GameweekPlayerValue(
                                target_gameweek,
                                0.0,
                                0.0,
                                0.0,
                            )
                        )
                    actual_values.append(
                        GameweekPlayerValue(target_gameweek, 0.0, 0.0, 0.0)
                    )
                    continue
                appearance_probability = float(
                    row["appearance_probability"]
                )
                if (
                    appearance_probability == 0
                    and float(row["expected_minutes"]) > 0
                ):
                    appearance_probability = min(
                        1.0,
                        float(row["expected_minutes"]) / 60.0,
                    )
                baseline_points = _baseline_point_forecasts(
                    row,
                    player_history.get(
                        player_season_id,
                        _empty_prefix(),
                    ),
                    position_history.get(
                        str(player["position"]),
                        _empty_prefix(),
                    ),
                    origin_gameweek,
                )
                for method, expected_points in baseline_points.items():
                    if method not in values_by_method:
                        continue
                    values_by_method[method].append(
                        GameweekPlayerValue(
                            target_gameweek,
                            expected_points,
                            appearance_probability,
                            float(row["sixty_probability"]),
                        )
                    )
                actual_appearance = float(
                    int(row["actual_minutes"]) > 0
                )
                actual_values.append(
                    GameweekPlayerValue(
                        target_gameweek,
                        float(row["actual_points"]),
                        actual_appearance,
                        float(int(row["actual_minutes"]) >= 60),
                    )
                )
            common = {
                "source_player_id": str(player["source_player_id"]),
                "web_name": str(player["web_name"]),
                "team_id": str(player["team_id"]),
                "team_short_name": str(player["team_short_name"]),
                "position": Position(str(player["position"])),
                "price_tenths": int(player["price_tenths"]),
            }
            for method, method_values in values_by_method.items():
                candidates_by_method[method].append(
                    CandidatePlayer(
                        **common,
                        expected_points=sum(
                            value.expected_points
                            for value in method_values
                        ),
                        gameweek_expected_points=(
                            method_values[0].expected_points
                        ),
                        appearance_probability=(
                            method_values[0].appearance_probability
                        ),
                        gameweek_values=tuple(method_values),
                    )
                )
            actual_candidates.append(
                CandidatePlayer(
                    **common,
                    expected_points=sum(
                        value.expected_points for value in actual_values
                    ),
                    gameweek_expected_points=actual_values[0].expected_points,
                    appearance_probability=actual_values[0].appearance_probability,
                    gameweek_values=tuple(actual_values),
                )
            )
        yield _OriginCandidates(
            origin_gameweek=origin_gameweek,
            target_gameweeks=target_gameweeks,
            candidates_by_method=candidates_by_method,
            actual_candidates=actual_candidates,
            actual_lookup={
                (
                    candidate.source_player_id,
                    value.gameweek_number,
                ): value
                for candidate in actual_candidates
                for value in candidate.gameweek_values
            },
        )


@dataclass(frozen=True)
class OwnedCaptainGameweekRegret:
    """One Gameweek's captaincy decision, scored against the same squad."""

    origin_gameweek: int
    target_gameweek: int
    captain_id: str
    vice_captain_id: str
    effective_captain_id: str | None
    effective_captain_points: int
    best_available_id: str | None
    best_available_points: int
    regret: int
    vice_captain_applied: bool


@dataclass(frozen=True)
class OwnedCaptainRegretReport:
    backtest_run_id: int
    season_code: str
    model_version: str
    method: str
    gameweeks: tuple[OwnedCaptainGameweekRegret, ...]
    samples: int
    total_regret: int
    mean_regret: float
    vice_captain_applied_count: int
    no_captain_count: int
    limitations: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "backtest_run_id": self.backtest_run_id,
            "season_code": self.season_code,
            "model_version": self.model_version,
            "method": self.method,
            "gameweeks": [asdict(entry) for entry in self.gameweeks],
            "samples": self.samples,
            "total_regret": self.total_regret,
            "mean_regret": self.mean_regret,
            "vice_captain_applied_count": self.vice_captain_applied_count,
            "no_captain_count": self.no_captain_count,
            "limitations": list(self.limitations),
        }


def evaluate_owned_captain_regret(
    database: HistoricalDatabase,
    backtest_run_id: int,
    rules: SeasonRules,
    *,
    method: str = "model",
) -> OwnedCaptainRegretReport:
    """Score captaincy against the best armband available in the same squad.

    The comparator is deliberately narrow. Comparing against the highest
    scorer in the game measures player ranking, not captaincy: a manager can
    only captain someone they own and who actually played. So the hindsight
    choice is the best of that Gameweek's own scoring lineup — the players the
    armband could have been on and still counted — after autosubs.

    The model's side applies the real fallback: the vice captains when the
    captain records no minutes, and nobody does when neither plays.
    """

    if method not in SUPPORTED_REGRET_METHODS:
        raise ValueError(
            f"Unknown owned-captain regret method {method!r}; expected one of "
            + ", ".join(SUPPORTED_REGRET_METHODS)
        )
    run, by_origin, player_history, position_history = _regret_run_context(
        database,
        backtest_run_id,
        rules,
    )
    entries = []
    for origin in _origin_candidate_sets(
        database,
        run,
        by_origin,
        player_history,
        position_history,
        (method,),
    ):
        predicted = optimise_full_squad(
            tuple(origin.candidates_by_method[method]),
            budget_tenths=rules.squad.budget_tenths,
            rules=rules,
        )
        plans = {plan.gameweek_number: plan for plan in predicted.gameweek_plans}
        for target_gameweek in origin.target_gameweeks:
            plan = plans.get(target_gameweek)
            if plan is None:
                continue
            outcomes = _realised_outcomes(
                predicted,
                origin.actual_lookup,
                target_gameweek,
            )
            resolved = resolve_squad_gameweek(
                predicted,
                outcomes,
                rules,
                target_gameweek,
            )
            effective = resolved.effective_captain_id
            effective_points = (
                0 if effective is None else outcomes[effective].points
            )
            attainable = {
                player_id: outcomes[player_id].points
                for player_id in resolved.scoring_player_ids
            }
            best_id = (
                None
                if not attainable
                else max(sorted(attainable), key=lambda key: attainable[key])
            )
            best_points = 0 if best_id is None else attainable[best_id]
            entries.append(
                OwnedCaptainGameweekRegret(
                    origin_gameweek=origin.origin_gameweek,
                    target_gameweek=target_gameweek,
                    captain_id=plan.captain_id,
                    vice_captain_id=plan.vice_captain_id,
                    effective_captain_id=effective,
                    effective_captain_points=effective_points,
                    best_available_id=best_id,
                    best_available_points=best_points,
                    regret=max(0, best_points - effective_points),
                    vice_captain_applied=(
                        effective is not None and effective == plan.vice_captain_id
                        and plan.vice_captain_id != plan.captain_id
                    ),
                )
            )
    if not entries:
        raise ValueError(
            f"Backtest run {backtest_run_id} produced no captaincy decisions"
        )
    total = sum(entry.regret for entry in entries)
    return OwnedCaptainRegretReport(
        backtest_run_id=backtest_run_id,
        season_code=str(run["season_code"]),
        model_version=str(run["model_version"]),
        method=method,
        gameweeks=tuple(entries),
        samples=len(entries),
        total_regret=total,
        mean_regret=round(total / len(entries), 4),
        vice_captain_applied_count=sum(
            entry.vice_captain_applied for entry in entries
        ),
        no_captain_count=sum(
            entry.effective_captain_id is None for entry in entries
        ),
        limitations=(
            "The comparator is the best armband within the squad the model "
            "actually selected, not the best player in the game.",
            "Attainable captains are that Gameweek's scoring lineup after "
            "autosubs, because an armband on a player who did not appear "
            "returns nothing.",
            "Each origin re-selects a squad, so captaincy is measured given "
            "the model's own selection rather than a carried squad.",
            "Triple Captain is not applied; this measures the ordinary "
            "doubling only.",
        ),
    )


def _realised_outcomes(
    result: FullSquadResult,
    actual_lookup: dict[tuple[str, int], GameweekPlayerValue],
    gameweek: int,
) -> dict[str, RealisedPlayerOutcome]:
    outcomes = {}
    for player in result.players:
        value = actual_lookup[(player.source_player_id, gameweek)]
        outcomes[player.source_player_id] = RealisedPlayerOutcome(
            source_player_id=player.source_player_id,
            points=int(round(value.expected_points)),
            minutes=90 if value.appearance_probability > 0 else 0,
        )
    return outcomes


def evaluate_legal_squad_regret(
    database: HistoricalDatabase,
    backtest_run_id: int,
    rules: SeasonRules,
    *,
    methods: tuple[str, ...] = (
        "model",
        "season_points_per_fixture",
        "recent_4_points_per_fixture",
        "season_points_per_90_model_minutes",
        "position_points_per_fixture",
    ),
) -> LegalSquadRegretReport:
    """Replay each origin as one legal persistent-squad selection problem."""

    run, by_origin, player_history, position_history = _regret_run_context(
        database,
        backtest_run_id,
        rules,
    )
    unknown_methods = set(methods) - set(SUPPORTED_REGRET_METHODS)
    if unknown_methods:
        raise ValueError(
            f"Unknown legal-squad regret methods: {sorted(unknown_methods)}"
        )
    if not methods:
        raise ValueError("At least one legal-squad regret method is required")
    method_names = tuple(dict.fromkeys(methods))
    regrets = []
    for origin in _origin_candidate_sets(
        database,
        run,
        by_origin,
        player_history,
        position_history,
        method_names,
    ):
        origin_gameweek = origin.origin_gameweek
        target_gameweeks = origin.target_gameweeks
        candidates_by_method = origin.candidates_by_method
        actual_candidates = origin.actual_candidates
        actual_lookup = origin.actual_lookup
        hindsight = optimise_full_squad(
            tuple(actual_candidates),
            budget_tenths=rules.squad.budget_tenths,
            rules=rules,
        )
        # Both sides are replayed through the same scorer. Comparing a hindsight
        # solver objective against a replayed realised score would mix two
        # scoring conventions and misreport the difference as regret.
        hindsight_points = _replayed_squad_points(
            hindsight,
            actual_lookup,
            target_gameweeks,
            rules,
        )
        for method, method_candidates in candidates_by_method.items():
            predicted = optimise_full_squad(
                tuple(method_candidates),
                budget_tenths=rules.squad.budget_tenths,
                rules=rules,
            )
            realised = _replayed_squad_points(
                predicted,
                actual_lookup,
                target_gameweeks,
                rules,
            )
            regrets.append(
                LegalSquadOriginRegret(
                    method=method,
                    origin_gameweek=origin_gameweek,
                    target_gameweeks=target_gameweeks,
                    predicted_objective=predicted.horizon_expected_points,
                    realised_points=round(realised, 3),
                    hindsight_optimal_points=hindsight_points,
                    regret=round(
                        max(0.0, hindsight_points - realised),
                        3,
                    ),
                )
            )
    totals = {
        method: round(
            sum(
                origin.regret
                for origin in regrets
                if origin.method == method
            ),
            4,
        )
        for method in method_names
    }
    counts = {
        method: sum(origin.method == method for origin in regrets)
        for method in method_names
    }
    return LegalSquadRegretReport(
        backtest_run_id=backtest_run_id,
        season_code=str(run["season_code"]),
        model_version=str(run["model_version"]),
        origins=tuple(regrets),
        mean_regret_by_method={
            method: round(totals[method] / counts[method], 4)
            for method in method_names
        },
        total_regret_by_method=totals,
        limitations=(
            "Each origin selects a new £100m squad; transfer continuity and hits "
            "are outside this measure. Use replay_backtest_transfer_continuity "
            "for a persistent-squad season score.",
            "Realised and hindsight points both replay the selected squad's own "
            "bench order, exact autosubs and captain fallback.",
            "The hindsight squad maximises the solver objective under actual "
            "values, so it is a strong comparator rather than a proven ceiling "
            "on the autosub-inclusive score.",
            "Legacy backtests without persisted appearance probabilities use "
            "expected_minutes / 60 as a compatibility fallback.",
        ),
    )


def replay_backtest_transfer_continuity(
    database: HistoricalDatabase,
    backtest_run_id: int,
    rules: SeasonRules,
    *,
    first_gameweek: int | None = None,
    last_gameweek: int | None = None,
    max_transfers_per_week: int = 2,
) -> dict[str, Any]:
    """Replay a backtest as one persistent squad carried across Gameweeks.

    `evaluate_legal_squad_regret` grants a free wildcard at every origin, so its
    score is what an unconstrained re-pick would earn rather than what a manager
    could reach. This carries a single squad, bank and free-transfer count
    forward, charges hits, and scores each week with exact autosubs.
    """

    run = database.connection.execute(
        """
        SELECT runs.model_version, runs.source_ingestion_run_id,
               seasons.id AS season_id, seasons.code AS season_code
        FROM projection_backtest_runs runs
        JOIN seasons ON seasons.id = runs.season_id
        WHERE runs.id = ? AND runs.status = 'completed'
        """,
        (backtest_run_id,),
    ).fetchone()
    if run is None:
        raise ValueError(f"Completed backtest run {backtest_run_id} is unavailable")
    if rules.season != str(run["season_code"]):
        raise ValueError("Continuity rules must match the backtest season")
    rows = database.connection.execute(
        """
        SELECT origin_gameweek, player_season_id, expected_points,
               appearance_probability, expected_minutes,
               actual_points, actual_minutes
        FROM projection_backtest_predictions
        WHERE backtest_run_id = ? AND origin_gameweek = target_gameweek
        ORDER BY origin_gameweek, player_season_id
        """,
        (backtest_run_id,),
    ).fetchall()
    if not rows:
        raise ValueError(
            "Continuity replay needs same-Gameweek forecasts; this run has no "
            "row whose target Gameweek equals its origin"
        )
    by_gameweek: dict[int, dict[int, Any]] = {}
    for row in rows:
        by_gameweek.setdefault(int(row["origin_gameweek"]), {})[
            int(row["player_season_id"])
        ] = row
    gameweeks = tuple(
        gameweek
        for gameweek in sorted(by_gameweek)
        if (first_gameweek is None or gameweek >= first_gameweek)
        and (last_gameweek is None or gameweek <= last_gameweek)
    )
    if len(gameweeks) < 2:
        raise ValueError("Continuity replay needs at least two Gameweeks")

    season_id = int(run["season_id"])
    ingestion_run_id = (
        None
        if run["source_ingestion_run_id"] is None
        else int(run["source_ingestion_run_id"])
    )
    # Metadata is cumulative, so players known at the opening Gameweek stay
    # resolvable later. Restricting the universe to them keeps every week's
    # candidate set identical, which the replay requires; players who first
    # appear mid-window are therefore never signed.
    universe = player_metadata_as_of(
        database,
        season_id,
        gameweeks[0],
        ingestion_run_id,
    )
    weeks = []
    for gameweek in gameweeks:
        metadata = player_metadata_as_of(
            database,
            season_id,
            gameweek,
            ingestion_run_id,
        )
        forecasts = by_gameweek[gameweek]
        candidates = []
        outcomes = []
        for player_season_id in sorted(universe):
            player = metadata.get(player_season_id, universe[player_season_id])
            row = forecasts.get(player_season_id)
            source_player_id = str(player["source_player_id"])
            appearance = 0.0
            expected = 0.0
            if row is not None:
                expected = float(row["expected_points"])
                appearance = float(row["appearance_probability"])
                if appearance == 0 and float(row["expected_minutes"]) > 0:
                    appearance = min(1.0, float(row["expected_minutes"]) / 60.0)
            candidates.append(
                CandidatePlayer(
                    source_player_id=source_player_id,
                    web_name=str(player["web_name"]),
                    team_id=str(player["team_id"]),
                    team_short_name=str(player["team_short_name"]),
                    position=Position(str(player["position"])),
                    price_tenths=int(player["price_tenths"]),
                    expected_points=expected,
                    gameweek_expected_points=expected,
                    appearance_probability=appearance,
                )
            )
            outcomes.append(
                RealisedPlayerOutcome(
                    source_player_id=source_player_id,
                    points=0 if row is None else int(round(float(row["actual_points"]))),
                    minutes=0 if row is None else int(row["actual_minutes"]),
                )
            )
        weeks.append(
            TransferReplayWeek(
                gameweek_number=gameweek,
                forecast_candidates=tuple(candidates),
                realised_outcomes=tuple(outcomes),
            )
        )

    opening = optimise_full_squad(
        weeks[0].forecast_candidates,
        budget_tenths=rules.squad.budget_tenths,
        rules=rules,
    )
    opening_ids = frozenset(
        player.source_player_id for player in opening.players
    )
    initial = CurrentSquad(
        player_ids=opening_ids,
        selling_prices_tenths={
            player.source_player_id: player.price_tenths
            for player in opening.players
        },
        bank_tenths=rules.squad.budget_tenths - opening.total_cost_tenths,
        free_transfers=1,
    )
    # The opening squad is the week-one selection itself, so replaying from it
    # would let the model transfer twice into the same Gameweek.
    report = replay_transfer_continuity(
        weeks[1:],
        initial,
        rules=rules,
        max_transfers_per_week=max_transfers_per_week,
    )
    opening_outcomes = {
        outcome.source_player_id: outcome for outcome in weeks[0].realised_outcomes
    }
    opening_points, opening_autosubs, _ = score_squad_gameweek(
        opening,
        opening_outcomes,
        rules,
        weeks[0].gameweek_number,
    )
    result = report.as_dict()
    result["limitations"] = [
        *report.limitations,
        "The opening squad is selected at the first replayed Gameweek and is "
        "scored without a transfer decision.",
        "Chips are not played; every Gameweek uses the base scoring rules.",
        "The candidate universe is fixed at the opening Gameweek, so players "
        "who first appear later are never signed.",
        "Sale values apply the season's configured profit-sharing rule to a "
        "carried purchase-price ledger, so a price rise funds only its "
        "configured share of the next transfer.",
    ]
    return {
        "backtest_run_id": backtest_run_id,
        "season_code": str(run["season_code"]),
        "model_version": str(run["model_version"]),
        "gameweeks": list(gameweeks),
        "max_transfers_per_week": max_transfers_per_week,
        "opening_gameweek": weeks[0].gameweek_number,
        "opening_squad_points": opening_points,
        "opening_squad_autosubs": opening_autosubs,
        "opening_squad_cost_tenths": opening.total_cost_tenths,
        "season_points": opening_points + report.total_net_points,
        **result,
    }


def player_metadata_as_of(
    database: HistoricalDatabase,
    season_id: int,
    origin_gameweek: int,
    maximum_ingestion_run_id: int | None,
) -> dict[int, dict[str, Any]]:
    rows = database.connection.execute(
        """
        WITH ranked AS (
            SELECT observations.*,
                   ROW_NUMBER() OVER (
                       PARTITION BY observations.player_season_id
                       ORDER BY gameweeks.number DESC,
                                observations.observed_at DESC,
                                observations.observed_on DESC,
                                observations.id DESC
                   ) AS observation_rank
            FROM player_gameweek_observations observations
            JOIN gameweeks ON gameweeks.id = observations.gameweek_id
            WHERE gameweeks.season_id = ?
              AND gameweeks.number <= ?
              AND (? IS NULL OR observations.provenance_run_id <= ?)
        )
        SELECT player_seasons.id AS player_season_id,
               player_seasons.source_player_id,
               players.web_name, player_seasons.position,
               COALESCE(ranked.team_id, player_seasons.team_id) AS team_id,
               teams.short_name AS team_short_name,
               ranked.price_tenths
        FROM ranked
        JOIN player_seasons
          ON player_seasons.id = ranked.player_season_id
        JOIN players ON players.id = player_seasons.player_id
        JOIN teams
          ON teams.id = COALESCE(ranked.team_id, player_seasons.team_id)
        WHERE ranked.observation_rank = 1
          AND ranked.price_tenths IS NOT NULL
        """,
        (
            season_id,
            origin_gameweek,
            maximum_ingestion_run_id,
            maximum_ingestion_run_id,
        ),
    ).fetchall()
    return {
        int(row["player_season_id"]): dict(row)
        for row in rows
    }


def _baseline_point_forecasts(
    row: Any,
    player_prefix: dict[str, list[float]],
    position_prefix: dict[str, list[float]],
    origin_gameweek: int,
) -> dict[str, float]:
    fixture_count = int(row["fixture_count"])
    position_rate = _rate(
        position_prefix["points"][origin_gameweek - 1],
        position_prefix["fixtures"][origin_gameweek - 1],
    )
    season_rate = _rate(
        player_prefix["points"][origin_gameweek - 1],
        player_prefix["fixtures"][origin_gameweek - 1],
        fallback=position_rate,
    )
    recent_start = max(0, origin_gameweek - 5)
    recent_rate = _rate(
        (
            player_prefix["points"][origin_gameweek - 1]
            - player_prefix["points"][recent_start]
        ),
        (
            player_prefix["fixtures"][origin_gameweek - 1]
            - player_prefix["fixtures"][recent_start]
        ),
        fallback=season_rate,
    )
    per_90 = _rate(
        player_prefix["points"][origin_gameweek - 1] * 90.0,
        player_prefix["minutes"][origin_gameweek - 1],
        fallback=season_rate,
    )
    return {
        "model": float(row["expected_points"]),
        "season_points_per_fixture": season_rate * fixture_count,
        "recent_4_points_per_fixture": recent_rate * fixture_count,
        "season_points_per_90_model_minutes": (
            per_90 * float(row["expected_minutes"]) / 90.0
        ),
        "position_points_per_fixture": position_rate * fixture_count,
    }


def _replayed_squad_points(
    result: FullSquadResult,
    actual_lookup: dict[tuple[str, int], GameweekPlayerValue],
    target_gameweeks: tuple[int, ...],
    rules: SeasonRules,
) -> float:
    """Score a selected squad over the horizon exactly as FPL would pay it.

    The earlier measure summed the forecast XI and applied captain fallback but
    never substituted a blanking starter, so it charged the squad for every
    absence the bench actually covers and understated what the selection was
    worth.
    """

    realised = 0
    for gameweek in target_gameweeks:
        # Only the played/blanked distinction drives autosubs; the backtest
        # row's own minutes are not carried on these values.
        outcomes = _realised_outcomes(result, actual_lookup, gameweek)
        points, _, _ = score_squad_gameweek(result, outcomes, rules, gameweek)
        realised += points
    return float(realised)


def _historical_prefixes(
    database: HistoricalDatabase,
    season_id: int,
) -> tuple[
    dict[int, dict[str, list[float]]],
    dict[str, dict[str, list[float]]],
]:
    rows = database.connection.execute(
        """
        SELECT stats.player_season_id, player_seasons.position,
               gameweeks.number AS gameweek_number,
               SUM(stats.total_points) AS points,
               SUM(stats.minutes) AS minutes,
               COUNT(stats.id) AS fixtures
        FROM player_fixture_stats stats
        JOIN player_seasons
          ON player_seasons.id = stats.player_season_id
        JOIN fixtures ON fixtures.id = stats.fixture_id
        JOIN gameweeks ON gameweeks.id = fixtures.gameweek_id
        WHERE player_seasons.season_id = ?
        GROUP BY stats.player_season_id, gameweeks.number
        """,
        (season_id,),
    ).fetchall()
    player_values: dict[int, dict[str, list[float]]] = {}
    position_values: dict[str, dict[str, list[float]]] = {}
    for row in rows:
        player = player_values.setdefault(
            int(row["player_season_id"]),
            _empty_gameweeks(),
        )
        position = position_values.setdefault(
            str(row["position"]),
            _empty_gameweeks(),
        )
        gameweek = int(row["gameweek_number"])
        for target in (player, position):
            target["points"][gameweek] += float(row["points"])
            target["minutes"][gameweek] += float(row["minutes"])
            target["fixtures"][gameweek] += float(row["fixtures"])
    return (
        {
            player_id: _prefix(values)
            for player_id, values in player_values.items()
        },
        {
            position: _prefix(values)
            for position, values in position_values.items()
        },
    )


def _empty_gameweeks() -> dict[str, list[float]]:
    return {
        "points": [0.0] * 39,
        "minutes": [0.0] * 39,
        "fixtures": [0.0] * 39,
    }


def _empty_prefix() -> dict[str, list[float]]:
    return _prefix(_empty_gameweeks())


def _prefix(
    values: dict[str, list[float]],
) -> dict[str, list[float]]:
    result = _empty_gameweeks()
    for name, gameweeks in values.items():
        running = 0.0
        for gameweek, value in enumerate(gameweeks):
            running += value
            result[name][gameweek] = running
    return result


def _rate(
    numerator: float,
    denominator: float,
    *,
    fallback: float = 0.0,
) -> float:
    return fallback if denominator <= 0 else numerator / denominator


def _benchmark_metrics(
    name: str,
    rows: list[dict[str, Any]],
    *,
    horizon_step: int | None,
) -> ForecastBenchmarkMetrics:
    errors = [
        float(row["actual_points"])
        - float(row["benchmark_expected_points"])
        for row in rows
    ]
    groups: dict[tuple[int, int], list[dict[str, Any]]] = {}
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
                -float(row["benchmark_expected_points"]),
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
            - sum(float(row["actual_points"]) for row in predicted[:15])
        )
    count = len(rows)
    return ForecastBenchmarkMetrics(
        name=name,
        horizon_step=horizon_step,
        samples=count,
        points_mae=round(
            sum(abs(error) for error in errors) / count,
            4,
        ),
        points_bias=round(sum(errors) / count, 4),
        points_rmse=round(
            math.sqrt(sum(error**2 for error in errors) / count),
            4,
        ),
        captain_regret=round(
            sum(captain_regret) / len(captain_regret),
            4,
        ),
        unconstrained_top_15_regret=round(
            sum(top_15_regret) / len(top_15_regret),
            4,
        ),
    )
