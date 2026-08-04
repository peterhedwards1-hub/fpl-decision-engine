"""Simple, leakage-controlled benchmarks for persisted projection backtests."""

from __future__ import annotations

import json
import math
import random
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .backtest import load_backtest_report
from .chip_state import ScoringChipPolicy
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
    OptimisationError,
    optimise_full_squad,
    optimise_opening_squads,
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


@dataclass(frozen=True)
class SquadConstructionPolicy:
    """A reproducible opening-squad search policy to compare historically."""

    name: str
    minimum_mean_appearance: float = 0.0
    candidate_pool_size: int = 1

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("Squad-construction policy name cannot be empty")
        if not 0.0 <= self.minimum_mean_appearance <= 1.0:
            raise ValueError("Minimum mean appearance must be between zero and one")
        if self.candidate_pool_size < 1:
            raise ValueError("Candidate pool size must be positive")


@dataclass(frozen=True)
class SquadPolicyOriginResult:
    """Forecast and realised evidence for one policy at one historical origin."""

    policy_name: str
    origin_gameweek: int
    target_gameweeks: tuple[int, ...]
    eligible_players: int
    status: str
    failure_reason: str | None = None
    predicted_decision_value: float | None = None
    predicted_horizon_points: float | None = None
    realised_points: float | None = None
    realised_autosub_points: float | None = None
    squad_cost_tenths: int | None = None
    bench_cost_tenths: int | None = None
    squad_mean_appearance: float | None = None
    bench_mean_appearance: float | None = None
    bench_projected_points: float | None = None
    selected_player_ids: tuple[str, ...] = ()
    bench_player_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class SquadPolicySummary:
    policy_name: str
    origins_succeeded: int
    origins_failed: int
    total_realised_points: float | None
    mean_realised_points: float | None
    mean_delta_vs_baseline: float | None
    paired_wins: int
    paired_losses: int
    paired_ties: int
    mean_bench_cost_tenths: float | None
    mean_bench_appearance: float | None
    mean_realised_autosub_points: float | None


@dataclass(frozen=True)
class SquadPolicyEvaluationReport:
    backtest_run_id: int
    season_code: str
    model_version: str
    baseline_policy: str
    policies: tuple[SquadConstructionPolicy, ...]
    origins: tuple[SquadPolicyOriginResult, ...]
    summaries: tuple[SquadPolicySummary, ...]
    limitations: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "backtest_run_id": self.backtest_run_id,
            "season_code": self.season_code,
            "model_version": self.model_version,
            "baseline_policy": self.baseline_policy,
            "policies": [asdict(policy) for policy in self.policies],
            "origins": [asdict(origin) for origin in self.origins],
            "summaries": [asdict(summary) for summary in self.summaries],
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
class ChipWeekValue:
    """What a scoring chip was worth in one Gameweek, given the owned squad."""

    gameweek_number: int
    chip: str
    legal: bool
    realised_gain: int


@dataclass(frozen=True)
class ChipRegretReport:
    """Timing regret for the two chips whose value is local to one Gameweek."""

    backtest_run_id: int
    season_code: str
    model_version: str
    gameweeks: tuple[int, ...]
    played: tuple[dict[str, Any], ...]
    by_chip: dict[str, dict[str, Any]]
    total_regret: int
    limitations: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "backtest_run_id": self.backtest_run_id,
            "season_code": self.season_code,
            "model_version": self.model_version,
            "gameweeks": list(self.gameweeks),
            "played": [dict(entry) for entry in self.played],
            "by_chip": self.by_chip,
            "total_regret": self.total_regret,
            "limitations": list(self.limitations),
        }


def evaluate_chip_regret(
    database: HistoricalDatabase,
    backtest_run_id: int,
    rules: SeasonRules,
    *,
    first_gameweek: int | None = None,
    last_gameweek: int | None = None,
    max_transfers_per_week: int = 1,
    chip_policy: ScoringChipPolicy | None = None,
    candidate_pool_size: int = 1,
) -> ChipRegretReport:
    """Score chip timing against the best week it could have been played in.

    Only Bench Boost and Triple Captain are measured. Their value is local: on
    a fixed squad, the gain is what the bench or the extra captain multiple
    actually scored that Gameweek. Wildcard and Free Hit change which squad
    exists, so the same argument does not reach them.

    Regret is the gain from the best legal Gameweek in the replayed window,
    less the gain actually taken. A chip left unplayed is charged the full best
    week, because not playing it is itself a timing decision.
    """

    replay = replay_backtest_transfer_continuity(
        database,
        backtest_run_id,
        rules,
        first_gameweek=first_gameweek,
        last_gameweek=last_gameweek,
        max_transfers_per_week=max_transfers_per_week,
        chip_policy=chip_policy,
        candidate_pool_size=candidate_pool_size,
    )
    weeks = replay["weeks"]
    if not weeks:
        raise ValueError(
            f"Backtest run {backtest_run_id} produced no chip decisions"
        )
    counterfactual = replay["chip_counterfactual"]
    played = [
        {
            "gameweek_number": week["gameweek_number"],
            "chip": week["active_chip"],
            "forecast_gain": week["chip_forecast_gain"],
            "realised_gain": week["chip_realised_gain"],
        }
        for week in weeks
        if week["active_chip"] is not None
    ]
    by_chip: dict[str, dict[str, Any]] = {}
    total = 0
    for chip, values in sorted(counterfactual.items()):
        legal = [entry for entry in values if entry["legal"]]
        best = max(legal, key=lambda entry: entry["realised_gain"], default=None)
        taken = next(
            (entry for entry in played if entry["chip"] == chip),
            None,
        )
        taken_gain = 0 if taken is None else int(taken["realised_gain"])
        best_gain = 0 if best is None else int(best["realised_gain"])
        regret = max(0, best_gain - taken_gain)
        total += regret
        by_chip[chip] = {
            "played_gameweek": None if taken is None else taken["gameweek_number"],
            "realised_gain": taken_gain,
            "best_legal_gameweek": None if best is None else best["gameweek_number"],
            "best_legal_gain": best_gain,
            "regret": regret,
            "legal_gameweeks": [entry["gameweek_number"] for entry in legal],
        }
    return ChipRegretReport(
        backtest_run_id=backtest_run_id,
        season_code=str(replay["season_code"]),
        model_version=str(replay["model_version"]),
        gameweeks=tuple(int(week["gameweek_number"]) for week in weeks),
        played=tuple(played),
        by_chip=by_chip,
        total_regret=total,
        limitations=(
            "Only Bench Boost and Triple Captain are measured; Wildcard and "
            "Free Hit alter future state and cannot be valued this way.",
            "The counterfactual holds the squad the model actually owned that "
            "Gameweek, so it measures chip timing rather than squad quality.",
            "The best week is the best within the replayed window, not the "
            "season, so a short window understates the alternative.",
            "A chip never played is charged the full best available gain.",
        ),
    )


@dataclass(frozen=True)
class TransferRegretReport:
    """Two distinct measures of the same replay, kept apart deliberately.

    `same_state_mean_regret` is the gate metric: from the state that really
    existed each Gameweek, how much did the chosen action cost against the best
    action available from that identical state. It does not compound.

    `continuous_policy_points` is the season-representative one: what the
    carried squad actually scored, which is more meaningful to a manager but
    accumulates every earlier decision and forecast error.
    """

    backtest_run_id: int
    season_code: str
    model_version: str
    gameweeks: tuple[int, ...]
    decisions: int
    same_state_total_regret: int
    same_state_mean_regret: float
    continuous_policy_points: int
    continuous_hindsight_points: int
    total_hits: int
    weeks: tuple[dict[str, Any], ...]
    limitations: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "backtest_run_id": self.backtest_run_id,
            "season_code": self.season_code,
            "model_version": self.model_version,
            "gameweeks": list(self.gameweeks),
            "decisions": self.decisions,
            "same_state_total_regret": self.same_state_total_regret,
            "same_state_mean_regret": self.same_state_mean_regret,
            "continuous_policy_points": self.continuous_policy_points,
            "continuous_hindsight_points": self.continuous_hindsight_points,
            "total_hits": self.total_hits,
            "weeks": list(self.weeks),
            "limitations": list(self.limitations),
        }


def evaluate_transfer_regret(
    database: HistoricalDatabase,
    backtest_run_id: int,
    rules: SeasonRules,
    *,
    first_gameweek: int | None = None,
    last_gameweek: int | None = None,
    max_transfers_per_week: int = 1,
    candidate_pool_size: int = 1,
) -> TransferRegretReport:
    """Score each transfer action against the best one from the same state.

    Both branches start from an identical prior state — owned squad, purchase
    prices, bank, free transfers, chip availability and transfer limit — and
    differ only in that the hindsight branch knows the Gameweek's outcomes.
    """

    replay = replay_backtest_transfer_continuity(
        database,
        backtest_run_id,
        rules,
        first_gameweek=first_gameweek,
        last_gameweek=last_gameweek,
        max_transfers_per_week=max_transfers_per_week,
        candidate_pool_size=candidate_pool_size,
    )
    weeks = replay["weeks"]
    if not weeks:
        raise ValueError(
            f"Backtest run {backtest_run_id} produced no transfer decisions"
        )
    total_regret = sum(int(week["regret"]) for week in weeks)
    return TransferRegretReport(
        backtest_run_id=backtest_run_id,
        season_code=str(replay["season_code"]),
        model_version=str(replay["model_version"]),
        gameweeks=tuple(int(week["gameweek_number"]) for week in weeks),
        decisions=len(weeks),
        same_state_total_regret=total_regret,
        same_state_mean_regret=round(total_regret / len(weeks), 4),
        continuous_policy_points=int(replay["season_points"]),
        continuous_hindsight_points=int(
            replay["total_same_state_hindsight_points"]
        ),
        total_hits=int(replay["total_hits"]),
        weeks=tuple(weeks),
        limitations=tuple(replay["limitations"]),
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


def evaluate_squad_construction_policies(
    database: HistoricalDatabase,
    backtest_run_id: int,
    rules: SeasonRules,
    policies: tuple[SquadConstructionPolicy, ...],
    *,
    origin_gameweeks: tuple[int, ...] | None = None,
) -> SquadPolicyEvaluationReport:
    """Compare opening-squad construction policies without future leakage.

    Every policy sees the same persisted forecasts and price/team metadata that
    were available at the historical origin. Realised points are then scored
    with the selected Gameweek lineups, legal autosubs and captain fallback.
    The first policy is the paired baseline for wins, losses and point deltas.
    """

    if not policies:
        raise ValueError("At least one squad-construction policy is required")
    policy_names = tuple(policy.name for policy in policies)
    if len(set(policy_names)) != len(policy_names):
        raise ValueError("Squad-construction policy names must be unique")
    if origin_gameweeks is not None:
        if not origin_gameweeks:
            raise ValueError("At least one origin Gameweek is required")
        if len(set(origin_gameweeks)) != len(origin_gameweeks):
            raise ValueError("Origin Gameweeks must be unique")
        requested_origins = set(origin_gameweeks)
    else:
        requested_origins = None

    run, by_origin, player_history, position_history = _regret_run_context(
        database,
        backtest_run_id,
        rules,
    )
    origins = tuple(
        origin
        for origin in _origin_candidate_sets(
            database,
            run,
            by_origin,
            player_history,
            position_history,
            ("model",),
        )
        if requested_origins is None
        or origin.origin_gameweek in requested_origins
    )
    found_origins = {origin.origin_gameweek for origin in origins}
    if requested_origins is not None and found_origins != requested_origins:
        missing = sorted(requested_origins - found_origins)
        raise ValueError(f"Backtest run has no predictions for origins {missing}")
    if not origins:
        raise ValueError("Backtest run has no origins to evaluate")

    results: list[SquadPolicyOriginResult] = []
    for origin in origins:
        model_candidates = tuple(origin.candidates_by_method["model"])
        for policy in policies:
            eligible = tuple(
                player
                for player in model_candidates
                if _candidate_mean_appearance(player)
                >= policy.minimum_mean_appearance
            )
            try:
                recommendation = optimise_opening_squads(
                    eligible,
                    budget_tenths=rules.squad.budget_tenths,
                    rules=rules,
                    alternative_count=0,
                    candidate_pool_size=policy.candidate_pool_size,
                )
            except OptimisationError as error:
                results.append(
                    SquadPolicyOriginResult(
                        policy_name=policy.name,
                        origin_gameweek=origin.origin_gameweek,
                        target_gameweeks=origin.target_gameweeks,
                        eligible_players=len(eligible),
                        status="infeasible",
                        failure_reason=str(error),
                    )
                )
                continue

            selected = recommendation.primary
            selected_by_id = {
                player.source_player_id: player for player in selected.players
            }
            bench = tuple(
                selected_by_id[player_id]
                for player_id in selected.bench_player_ids
            )
            realised, realised_autosubs = _replayed_squad_score_breakdown(
                selected,
                origin.actual_lookup,
                origin.target_gameweeks,
                rules,
            )
            results.append(
                SquadPolicyOriginResult(
                    policy_name=policy.name,
                    origin_gameweek=origin.origin_gameweek,
                    target_gameweeks=origin.target_gameweeks,
                    eligible_players=len(eligible),
                    status="ok",
                    predicted_decision_value=selected.decision_value,
                    predicted_horizon_points=selected.horizon_expected_points,
                    realised_points=round(realised, 3),
                    realised_autosub_points=round(realised_autosubs, 3),
                    squad_cost_tenths=selected.total_cost_tenths,
                    bench_cost_tenths=sum(player.price_tenths for player in bench),
                    squad_mean_appearance=round(
                        sum(
                            _candidate_mean_appearance(player)
                            for player in selected.players
                        )
                        / len(selected.players),
                        4,
                    ),
                    bench_mean_appearance=round(
                        sum(_candidate_mean_appearance(player) for player in bench)
                        / len(bench),
                        4,
                    ),
                    bench_projected_points=round(
                        sum(player.expected_points for player in bench),
                        3,
                    ),
                    selected_player_ids=tuple(
                        sorted(player.source_player_id for player in selected.players)
                    ),
                    bench_player_ids=tuple(selected.bench_player_ids),
                )
            )

    return SquadPolicyEvaluationReport(
        backtest_run_id=backtest_run_id,
        season_code=str(run["season_code"]),
        model_version=str(run["model_version"]),
        baseline_policy=policies[0].name,
        policies=policies,
        origins=tuple(results),
        summaries=_summarise_squad_policies(tuple(results), policies),
        limitations=(
            "Each origin is a fresh opening-squad decision with no transfer cost; "
            "this isolates construction and bench policy rather than measuring a "
            "complete carried season.",
            "Inputs are the forecasts and metadata persisted at each origin. Legacy "
            "historical feeds cannot reconstruct every injury, press-conference or "
            "deadline snapshot that a live process would know.",
            "Appearance floors are applied to the mean forecast probability across "
            "the available horizon, not to realised appearances.",
            "Realised points include exact legal autosubs and captain fallback. The "
            "candidate-pool search compares distinct solver-optimal linear squads; "
            "it is not a proof of the global nonlinear autosub optimum.",
            "A policy that cannot form a legal squad is reported as infeasible and "
            "excluded from paired point comparisons, never silently discarded.",
        ),
    )


def compile_squad_policy_evaluations(
    database: HistoricalDatabase,
    backtest_run_ids: tuple[int, ...],
    rules_by_season: dict[str, SeasonRules],
    policies: tuple[SquadConstructionPolicy, ...],
    *,
    origin_gameweeks: tuple[int, ...] | None = None,
    bootstrap_samples: int = 2000,
    random_seed: int = 20260804,
) -> dict[str, object]:
    """Compile comparable squad-policy evidence across historical seasons."""

    if not backtest_run_ids:
        raise ValueError("At least one backtest run is required")
    if len(set(backtest_run_ids)) != len(backtest_run_ids):
        raise ValueError("Backtest run IDs must be unique")
    if bootstrap_samples < 1:
        raise ValueError("Bootstrap samples must be positive")
    reports = []
    for run_id in backtest_run_ids:
        row = database.connection.execute(
            """
            SELECT seasons.code
            FROM projection_backtest_runs runs
            JOIN seasons ON seasons.id = runs.season_id
            WHERE runs.id = ?
            """,
            (run_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Backtest run {run_id} is unavailable")
        season_code = str(row["code"])
        rules = rules_by_season.get(season_code)
        if rules is None:
            raise ValueError(f"Rules are missing for season {season_code}")
        reports.append(
            evaluate_squad_construction_policies(
                database,
                run_id,
                rules,
                policies,
                origin_gameweeks=origin_gameweeks,
            )
        )
    pooled, intervals = _pool_squad_policy_reports(
        tuple(reports),
        policies,
        bootstrap_samples=bootstrap_samples,
        random_seed=random_seed,
    )
    return {
        "backtest_run_ids": list(backtest_run_ids),
        "baseline_policy": policies[0].name,
        "policies": [asdict(policy) for policy in policies],
        "season_reports": [report.as_dict() for report in reports],
        "pooled_summaries": pooled,
        "season_cluster_bootstrap_delta_ci95": intervals,
        "bootstrap_samples": bootstrap_samples,
        "random_seed": random_seed,
        "limitations": [
            "The confidence interval resamples whole seasons, preserving the "
            "dependence among overlapping origins within a season.",
            "Five historical seasons are design evidence, not five hundred "
            "independent experiments; wide intervals should be expected.",
            "Use spaced origins at least one forecast horizon apart when the goal "
            "is a less-overlapping estimate rather than exhaustive diagnostics.",
            *reports[0].limitations,
        ],
    }


def _pool_squad_policy_reports(
    reports: tuple[SquadPolicyEvaluationReport, ...],
    policies: tuple[SquadConstructionPolicy, ...],
    *,
    bootstrap_samples: int,
    random_seed: int,
) -> tuple[list[dict[str, object]], dict[str, dict[str, object] | None]]:
    baseline = {
        (report.backtest_run_id, origin.origin_gameweek): origin
        for report in reports
        for origin in report.origins
        if origin.policy_name == policies[0].name
        and origin.status == "ok"
        and origin.realised_points is not None
    }
    pooled: list[dict[str, object]] = []
    intervals: dict[str, dict[str, object] | None] = {}
    random_generator = random.Random(random_seed)
    for policy in policies:
        policy_origins = [
            (report.backtest_run_id, origin)
            for report in reports
            for origin in report.origins
            if origin.policy_name == policy.name
        ]
        succeeded = [
            (run_id, origin)
            for run_id, origin in policy_origins
            if origin.status == "ok" and origin.realised_points is not None
        ]
        deltas_by_run: dict[int, list[float]] = {}
        for run_id, origin in succeeded:
            base = baseline.get((run_id, origin.origin_gameweek))
            if base is None:
                continue
            deltas_by_run.setdefault(run_id, []).append(
                float(origin.realised_points) - float(base.realised_points)
            )
        deltas = [delta for values in deltas_by_run.values() for delta in values]

        def mean_optional(
            field: str,
            source: list[tuple[int, SquadPolicyOriginResult]] = succeeded,
        ) -> float | None:
            values = [
                float(value)
                for _, origin in source
                if (value := getattr(origin, field)) is not None
            ]
            return None if not values else round(sum(values) / len(values), 4)

        realised = [float(origin.realised_points) for _, origin in succeeded]
        pooled.append(
            {
                "policy_name": policy.name,
                "seasons_succeeded": len(deltas_by_run),
                "origins_succeeded": len(succeeded),
                "origins_failed": len(policy_origins) - len(succeeded),
                "total_realised_points": (
                    None if not realised else round(sum(realised), 4)
                ),
                "mean_realised_points": mean_optional("realised_points"),
                "mean_delta_vs_baseline": (
                    None if not deltas else round(sum(deltas) / len(deltas), 4)
                ),
                "paired_wins": sum(delta > 1e-9 for delta in deltas),
                "paired_losses": sum(delta < -1e-9 for delta in deltas),
                "paired_ties": sum(abs(delta) <= 1e-9 for delta in deltas),
                "mean_bench_cost_tenths": mean_optional("bench_cost_tenths"),
                "mean_bench_appearance": mean_optional("bench_mean_appearance"),
                "mean_realised_autosub_points": mean_optional(
                    "realised_autosub_points"
                ),
            }
        )
        if len(deltas_by_run) < 2:
            intervals[policy.name] = None
            continue
        run_ids = tuple(sorted(deltas_by_run))
        draws = []
        for _ in range(bootstrap_samples):
            sampled = [
                random_generator.choice(run_ids) for _ in range(len(run_ids))
            ]
            sampled_deltas = [
                delta for run_id in sampled for delta in deltas_by_run[run_id]
            ]
            draws.append(sum(sampled_deltas) / len(sampled_deltas))
        draws.sort()
        lower = draws[max(0, int(0.025 * bootstrap_samples) - 1)]
        upper = draws[min(bootstrap_samples - 1, int(0.975 * bootstrap_samples))]
        intervals[policy.name] = {
            "low": round(lower, 4),
            "high": round(upper, 4),
            "season_clusters": len(run_ids),
        }
    return pooled, intervals


def _candidate_mean_appearance(player: CandidatePlayer) -> float:
    values = player.gameweek_values
    if not values:
        return player.appearance_probability
    return sum(value.appearance_probability for value in values) / len(values)


def _summarise_squad_policies(
    origins: tuple[SquadPolicyOriginResult, ...],
    policies: tuple[SquadConstructionPolicy, ...],
) -> tuple[SquadPolicySummary, ...]:
    baseline = {
        origin.origin_gameweek: origin
        for origin in origins
        if origin.policy_name == policies[0].name
        and origin.status == "ok"
        and origin.realised_points is not None
    }
    summaries = []
    for policy in policies:
        policy_origins = [
            origin for origin in origins if origin.policy_name == policy.name
        ]
        succeeded = [
            origin
            for origin in policy_origins
            if origin.status == "ok" and origin.realised_points is not None
        ]
        paired = [
            (origin, baseline[origin.origin_gameweek])
            for origin in succeeded
            if origin.origin_gameweek in baseline
        ]
        deltas = [
            float(origin.realised_points) - float(base.realised_points)
            for origin, base in paired
        ]

        def mean(
            field: str,
            source: list[SquadPolicyOriginResult] = succeeded,
        ) -> float | None:
            values = [
                float(value)
                for origin in source
                if (value := getattr(origin, field)) is not None
            ]
            return None if not values else round(sum(values) / len(values), 4)

        realised_values = [float(origin.realised_points) for origin in succeeded]
        summaries.append(
            SquadPolicySummary(
                policy_name=policy.name,
                origins_succeeded=len(succeeded),
                origins_failed=len(policy_origins) - len(succeeded),
                total_realised_points=(
                    None if not realised_values else round(sum(realised_values), 4)
                ),
                mean_realised_points=mean("realised_points"),
                mean_delta_vs_baseline=(
                    None if not deltas else round(sum(deltas) / len(deltas), 4)
                ),
                paired_wins=sum(delta > 1e-9 for delta in deltas),
                paired_losses=sum(delta < -1e-9 for delta in deltas),
                paired_ties=sum(abs(delta) <= 1e-9 for delta in deltas),
                mean_bench_cost_tenths=mean("bench_cost_tenths"),
                mean_bench_appearance=mean("bench_mean_appearance"),
                mean_realised_autosub_points=mean("realised_autosub_points"),
            )
        )
    return tuple(summaries)


def replay_backtest_transfer_continuity(
    database: HistoricalDatabase,
    backtest_run_id: int,
    rules: SeasonRules,
    *,
    first_gameweek: int | None = None,
    last_gameweek: int | None = None,
    max_transfers_per_week: int = 2,
    chip_policy: ScoringChipPolicy | None = None,
    candidate_pool_size: int = 1,
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
    # The whole horizon, not only the scored Gameweek. A chip policy that can
    # only see this week will always spend the chip now, whatever is coming.
    rows = database.connection.execute(
        """
        SELECT origin_gameweek, target_gameweek, player_season_id,
               expected_points, appearance_probability, sixty_probability,
               expected_minutes, actual_points, actual_minutes
        FROM projection_backtest_predictions
        WHERE backtest_run_id = ?
        ORDER BY origin_gameweek, target_gameweek, player_season_id
        """,
        (backtest_run_id,),
    ).fetchall()
    horizon: dict[int, dict[int, list[Any]]] = {}
    horizon_targets: dict[int, tuple[int, ...]] = {}
    for row in rows:
        horizon.setdefault(int(row["origin_gameweek"]), {}).setdefault(
            int(row["player_season_id"]), []
        ).append(row)
    for origin, players in horizon.items():
        horizon_targets[origin] = tuple(
            sorted(
                {
                    int(value["target_gameweek"])
                    for rows_for_player in players.values()
                    for value in rows_for_player
                }
            )
        )
    rows = [row for row in rows if row["origin_gameweek"] == row["target_gameweek"]]
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
            # Every projected Gameweek from this origin, so a later double
            # Gameweek is visible to a chip or lineup decision taken now. A
            # player with no row for a Gameweek has no fixture in it, so the
            # blank is filled with zero rather than left ragged — the optimiser
            # requires every candidate to cover the same Gameweeks.
            by_target = {
                int(value["target_gameweek"]): value
                for value in horizon.get(gameweek, {}).get(player_season_id, [])
            }
            values = tuple(
                GameweekPlayerValue(
                    gameweek_number=target,
                    expected_points=(
                        0.0
                        if target not in by_target
                        else float(by_target[target]["expected_points"])
                    ),
                    appearance_probability=(
                        0.0
                        if target not in by_target
                        else float(by_target[target]["appearance_probability"])
                    ),
                    sixty_probability=(
                        0.0
                        if target not in by_target
                        else float(by_target[target]["sixty_probability"] or 0.0)
                    ),
                )
                for target in horizon_targets[gameweek]
            )
            candidates.append(
                CandidatePlayer(
                    source_player_id=source_player_id,
                    web_name=str(player["web_name"]),
                    team_id=str(player["team_id"]),
                    team_short_name=str(player["team_short_name"]),
                    position=Position(str(player["position"])),
                    price_tenths=int(player["price_tenths"]),
                    expected_points=(
                        sum(value.expected_points for value in values)
                        if values
                        else expected
                    ),
                    gameweek_expected_points=expected,
                    appearance_probability=appearance,
                    gameweek_values=values,
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

    opening = optimise_opening_squads(
        weeks[0].forecast_candidates,
        budget_tenths=rules.squad.budget_tenths,
        rules=rules,
        alternative_count=0,
        candidate_pool_size=candidate_pool_size,
    ).primary
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
        available_chips=tuple(rules.chips.names),
    )
    # The opening squad is the week-one selection itself, so replaying from it
    # would let the model transfer twice into the same Gameweek.
    report = replay_transfer_continuity(
        weeks[1:],
        initial,
        rules=rules,
        max_transfers_per_week=max_transfers_per_week,
        chip_policy=chip_policy,
        candidate_pool_size=candidate_pool_size,
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
    chip_limitation = (
        "Chips are not played; every Gameweek uses the base scoring rules."
        if chip_policy is None or not chip_policy.plays_anything
        else "Bench Boost and Triple Captain follow the explicitly supplied "
        f"policy: {chip_policy.as_dict()}. Wildcard and Free Hit are not replayed."
    )
    result["limitations"] = [
        *report.limitations,
        "The opening squad is selected at the first replayed Gameweek and is "
        "scored without a transfer decision.",
        chip_limitation,
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


def compile_transfer_policy_evaluation(
    database: HistoricalDatabase,
    backtest_run_ids: tuple[int, ...],
    rules_by_season: dict[str, SeasonRules],
    *,
    first_gameweek: int | None = None,
    last_gameweek: int | None = None,
    max_transfers_per_week: int = 2,
    candidate_pool_size: int = 1,
    minimum_samples: int = 50,
    maximum_cap_share: float = 0.75,
) -> dict[str, Any]:
    """Replay several seasons and estimate the option value of saved transfers."""

    if not backtest_run_ids:
        raise ValueError("At least one backtest run is required")
    if len(set(backtest_run_ids)) != len(backtest_run_ids):
        raise ValueError("Backtest run IDs must be unique")
    if minimum_samples < 1:
        raise ValueError("Minimum transfer-policy samples must be positive")
    if not 0.0 < maximum_cap_share < 1.0:
        raise ValueError("Maximum transfer-cap share must be between zero and one")
    season_summaries = []
    need_counts: list[int] = []
    for run_id in backtest_run_ids:
        row = database.connection.execute(
            """
            SELECT seasons.code
            FROM projection_backtest_runs runs
            JOIN seasons ON seasons.id = runs.season_id
            WHERE runs.id = ?
            """,
            (run_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Backtest run {run_id} is unavailable")
        season_code = str(row["code"])
        rules = rules_by_season.get(season_code)
        if rules is None:
            raise ValueError(f"Rules are missing for season {season_code}")
        replay = replay_backtest_transfer_continuity(
            database,
            run_id,
            rules,
            first_gameweek=first_gameweek,
            last_gameweek=last_gameweek,
            max_transfers_per_week=max_transfers_per_week,
            candidate_pool_size=candidate_pool_size,
        )
        season_needs = [
            int(week["reference_one_ft_hindsight_transfers"])
            for week in replay["weeks"]
        ]
        need_counts.extend(season_needs)
        season_summaries.append(
            {
                "backtest_run_id": run_id,
                "season_code": season_code,
                "model_version": replay["model_version"],
                "gameweeks": replay["gameweeks"],
                "season_points": replay["season_points"],
                "total_hits": replay["total_hits"],
                "total_regret": replay["total_regret"],
                "final_free_transfers": replay["final_free_transfers"],
                "reference_one_ft_hindsight_transfer_need_counts": {
                    str(count): season_needs.count(count)
                    for count in sorted(set(season_needs))
                },
            }
        )
    if not need_counts:
        raise ValueError("The transfer-policy replays produced no decisions")
    diagnostic_distribution = {
        str(count): round(need_counts.count(count) / len(need_counts), 8)
        for count in sorted(set(need_counts))
    }
    cap_share = need_counts.count(max_transfers_per_week) / len(need_counts)
    qualification_failures = []
    if len(need_counts) < minimum_samples:
        qualification_failures.append(
            f"Only {len(need_counts)} samples are available; {minimum_samples} are required."
        )
    if cap_share > maximum_cap_share:
        qualification_failures.append(
            f"The transfer cap was selected in {cap_share:.1%} of samples; "
            f"the maximum qualified share is {maximum_cap_share:.1%}."
        )
    if len(diagnostic_distribution) < 2:
        qualification_failures.append(
            "The estimated need distribution has no variation."
        )
    qualified = not qualification_failures
    return {
        "backtest_run_ids": list(backtest_run_ids),
        "first_gameweek": first_gameweek,
        "last_gameweek": last_gameweek,
        "max_transfers_per_week": max_transfers_per_week,
        "candidate_pool_size": candidate_pool_size,
        "samples": len(need_counts),
        "qualified": qualified,
        "qualification_failures": qualification_failures,
        "minimum_samples": minimum_samples,
        "maximum_cap_share": maximum_cap_share,
        "diagnostic_transfer_need_distribution": diagnostic_distribution,
        "future_transfer_need_distribution": (
            diagnostic_distribution if qualified else None
        ),
        "season_summaries": season_summaries,
        "limitations": [
            "Transfer need is the same-state one-Gameweek hindsight optimum after "
            "every solved route is re-priced from a reference state with one free "
            "transfer; it is not observed manager behaviour.",
            "It prices the option to avoid future hits; it does not price every "
            "benefit of bank, team structure or waiting for information.",
            "Historical metadata fixes the player universe at the opening origin, "
            "so mid-window arrivals cannot become transfer targets.",
            "Use this empirical distribution only with the same transfer cap and "
            "season-rule family recorded in the artifact.",
            "A distribution concentrated at the searched transfer cap fails closed "
            "because it measures hindsight search appetite, not usable option value.",
        ],
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

    realised, _ = _replayed_squad_score_breakdown(
        result,
        actual_lookup,
        target_gameweeks,
        rules,
    )
    return realised


def _replayed_squad_score_breakdown(
    result: FullSquadResult,
    actual_lookup: dict[tuple[str, int], GameweekPlayerValue],
    target_gameweeks: tuple[int, ...],
    rules: SeasonRules,
) -> tuple[float, float]:
    """Return realised total and the part supplied by automatic substitutes."""

    realised = 0
    autosub_points = 0
    for gameweek in target_gameweeks:
        # Only the played/blanked distinction drives autosubs; the backtest
        # row's own minutes are not carried on these values.
        outcomes = _realised_outcomes(result, actual_lookup, gameweek)
        points, autosubs, _ = score_squad_gameweek(
            result,
            outcomes,
            rules,
            gameweek,
        )
        realised += points
        autosub_points += autosubs
    return float(realised), float(autosub_points)


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
