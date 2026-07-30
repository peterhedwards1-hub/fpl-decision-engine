"""Chronological hurdle models for appearance, starts and minutes.

This module is deliberately separate from the production rates model.  It trains
challengers from strictly earlier player-Gameweek rows, calibrates probability
outputs on chronological out-of-fold predictions, and writes an auditable
artifact.  Promotion into live projections is a later, forward-data decision.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from .history.database import HistoricalDatabase

HurdleFamily = Literal["logistic", "histogram_gradient_boosting"]

FEATURE_NAMES = (
    "gameweek_fraction",
    "price_tenths",
    "is_gk",
    "is_def",
    "is_mid",
    "is_fwd",
    "player_appearance_rate",
    "player_start_rate",
    "player_sixty_given_appearance",
    "player_conditional_minutes",
    "position_appearance_rate",
    "team_appearance_rate",
    "previous_minutes",
    "previous_started",
    "absence_streak",
    "substitute_run_length",
    "new_club",
    "position_change",
)


class PlayingTimeDependencyError(RuntimeError):
    """Raised when the optional modelling dependencies are unavailable."""


@dataclass(frozen=True)
class HurdleMetrics:
    samples: int
    appearance_brier: float
    appearance_log_loss: float
    start_brier: float
    start_log_loss: float
    sixty_brier: float
    sixty_log_loss: float
    expected_minutes_rmse: float
    expected_minutes_bias: float
    conditional_minutes_rmse: float


@dataclass(frozen=True)
class DownstreamPointsMetrics:
    samples: int
    baseline_rmse: float
    hurdle_rmse: float
    rmse_change: float
    baseline_bias: float
    hurdle_bias: float
    global_top_one_regret_change: float
    unconstrained_top_15_regret_change: float
    method: str


@dataclass(frozen=True)
class HurdleTrainingReport:
    artifact_path: str
    metadata_path: str
    family: str
    training_seasons: tuple[str, ...]
    validation_season: str
    feature_names: tuple[str, ...]
    chronological_oof_samples: int
    calibration_method: str
    baseline: HurdleMetrics
    challenger: HurdleMetrics
    downstream_points: DownstreamPointsMetrics | None
    limitations: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["training_seasons"] = list(self.training_seasons)
        result["feature_names"] = list(self.feature_names)
        result["limitations"] = list(self.limitations)
        return result


@dataclass(frozen=True)
class _HurdleRow:
    season_code: str
    gameweek: int
    player_season_id: int
    features: tuple[float, ...]
    baseline_appearance: float
    baseline_start: float
    baseline_sixty: float
    baseline_conditional_minutes: float
    appeared: int
    started: int
    reached_sixty: int
    minutes: int


def train_and_evaluate_hurdle_model(
    database: HistoricalDatabase,
    *,
    training_seasons: tuple[str, ...],
    validation_season: str,
    artifact_path: str | Path,
    family: HurdleFamily = "logistic",
    seed: int = 20260730,
) -> HurdleTrainingReport:
    """Train a four-part playing-time challenger and score a later season."""

    if not training_seasons:
        raise ValueError("At least one training season is required")
    ordered_training = tuple(sorted(training_seasons))
    if ordered_training != training_seasons:
        raise ValueError("Training seasons must be supplied chronologically")
    if len(set(training_seasons)) != len(training_seasons):
        raise ValueError("Training seasons must be unique")
    if max(training_seasons) >= validation_season:
        raise ValueError("Every training season must precede validation")
    if family not in {"logistic", "histogram_gradient_boosting"}:
        raise ValueError(f"Unsupported hurdle family {family!r}")

    try:
        import joblib
        from sklearn.isotonic import IsotonicRegression
    except ImportError as error:
        raise PlayingTimeDependencyError(
            "Playing-time challengers require the 'modeling' dependency"
        ) from error

    rows = _build_hurdle_rows(
        database,
        (*training_seasons, validation_season),
    )
    training = [row for row in rows if row.season_code in training_seasons]
    validation = [row for row in rows if row.season_code == validation_season]
    if not training or not validation:
        raise ValueError("Training and validation seasons must contain fixture rows")

    models: dict[str, Any] = {}
    calibrators: dict[str, Any | None] = {}
    oof_counts: list[int] = []
    target_specs = (
        ("appearance", lambda row: row.appeared, False),
        ("start", lambda row: row.started, False),
        ("sixty", lambda row: row.reached_sixty, True),
    )
    for name, target, appeared_only in target_specs:
        eligible = [row for row in training if not appeared_only or row.appeared]
        base_model = _classifier(family, seed)
        oof_predictions, oof_actual = _chronological_oof(
            eligible,
            family=family,
            seed=seed,
            target=target,
        )
        calibrator = None
        if (
            len(oof_predictions) >= 200
            and len(set(oof_actual)) == 2
            and len(set(round(value, 8) for value in oof_predictions)) > 1
        ):
            calibrator = IsotonicRegression(
                out_of_bounds="clip",
                y_min=0.001,
                y_max=0.999,
            )
            calibrator.fit(oof_predictions, oof_actual)
        base_model.fit(
            [row.features for row in eligible],
            [target(row) for row in eligible],
        )
        models[name] = base_model
        calibrators[name] = calibrator
        oof_counts.append(len(oof_predictions))

    minutes_training = [row for row in training if row.appeared]
    minutes_model = _regressor(family, seed)
    minutes_model.fit(
        [row.features for row in minutes_training],
        [row.minutes for row in minutes_training],
    )
    models["conditional_minutes"] = minutes_model

    predictions = _predict_hurdles(validation, models, calibrators)
    baseline = _hurdle_metrics(
        validation,
        [
            (
                row.baseline_appearance,
                row.baseline_start,
                row.baseline_sixty,
                row.baseline_conditional_minutes,
            )
            for row in validation
        ],
    )
    challenger = _hurdle_metrics(validation, predictions)
    downstream = _downstream_points_metrics(
        database,
        validation_season,
        validation,
        predictions,
    )

    artifact = Path(artifact_path)
    artifact.parent.mkdir(parents=True, exist_ok=True)
    metadata_path = artifact.with_suffix(".json")
    payload = {
        "schema_version": 1,
        "family": family,
        "feature_names": FEATURE_NAMES,
        "training_seasons": training_seasons,
        "validation_season": validation_season,
        "models": models,
        "calibrators": calibrators,
        "separate_signal_half_lives_gameweeks": {
            "appearance": 3.0,
            "starts": 3.0,
            "conditional_minutes": 5.0,
            "scoring_skill": 12.0,
            "team_strength": 8.0,
            "news": 1.0,
        },
    }
    joblib.dump(payload, artifact)
    report = HurdleTrainingReport(
        artifact_path=str(artifact),
        metadata_path=str(metadata_path),
        family=family,
        training_seasons=training_seasons,
        validation_season=validation_season,
        feature_names=FEATURE_NAMES,
        chronological_oof_samples=min(oof_counts),
        calibration_method=(
            "isotonic_on_strictly_chronological_oof"
            if any(value is not None for value in calibrators.values())
            else "identity_insufficient_chronological_oof"
        ),
        baseline=baseline,
        challenger=challenger,
        downstream_points=downstream,
        limitations=(
            "Historical availability snapshots have unknown timing and are excluded.",
            "Manager-change data is not yet present, so no manager-change feature is fitted.",
            "Starts are predicted unconditionally; the 60-minute model is "
            "conditional on appearance.",
            "Historical results are design evidence only; promotion requires forward 2026/27 rows.",
        ),
    )
    metadata_path.write_text(
        json.dumps(report.as_dict(), indent=2),
        encoding="utf-8",
    )
    return report


def load_hurdle_artifact(path: str | Path) -> dict[str, Any]:
    """Load a previously trained hurdle model with its feature contract."""

    try:
        import joblib
    except ImportError as error:
        raise PlayingTimeDependencyError(
            "Playing-time challengers require the 'modeling' dependency"
        ) from error
    artifact = joblib.load(path)
    if artifact.get("schema_version") != 1:
        raise ValueError("Unsupported playing-time artifact schema")
    if tuple(artifact.get("feature_names", ())) != FEATURE_NAMES:
        raise ValueError("Playing-time artifact feature contract does not match")
    return artifact


def predict_live_hurdles(
    database: HistoricalDatabase,
    artifact_path: str | Path,
    *,
    season_code: str,
    start_gameweek: int,
    players: list[dict[str, Any]],
) -> dict[int, tuple[float, float, float, float]]:
    """Apply a trained hurdle artifact using only evidence before the origin."""

    artifact = load_hurdle_artifact(artifact_path)
    features = _live_feature_rows(
        database,
        season_code=season_code,
        start_gameweek=start_gameweek,
        players=players,
    )
    ordered_ids = [int(player["player_season_id"]) for player in players]
    synthetic = [
        _HurdleRow(
            season_code=season_code,
            gameweek=start_gameweek,
            player_season_id=player_id,
            features=features[player_id],
            baseline_appearance=0.0,
            baseline_start=0.0,
            baseline_sixty=0.0,
            baseline_conditional_minutes=0.0,
            appeared=0,
            started=0,
            reached_sixty=0,
            minutes=0,
        )
        for player_id in ordered_ids
    ]
    predicted = _predict_hurdles(
        synthetic,
        artifact["models"],
        artifact["calibrators"],
    )
    return dict(zip(ordered_ids, predicted, strict=True))


def _live_feature_rows(
    database: HistoricalDatabase,
    *,
    season_code: str,
    start_gameweek: int,
    players: list[dict[str, Any]],
) -> dict[int, tuple[float, ...]]:
    player_season_ids = tuple(
        int(player["player_season_id"]) for player in players
    )
    if not player_season_ids:
        return {}
    placeholders = ",".join("?" for _ in player_season_ids)
    histories = database.connection.execute(
        f"""
        WITH current_players AS (
            SELECT id AS current_player_season_id, player_id
            FROM player_seasons
            WHERE id IN ({placeholders})
        )
        SELECT current_players.current_player_season_id,
               seasons.code AS season_code,
               gameweeks.number AS gameweek,
               history.team_id, history.position,
               SUM(stats.minutes) AS minutes,
               MAX(stats.starts) AS started
        FROM current_players
        JOIN player_seasons history
          ON history.player_id = current_players.player_id
        JOIN seasons ON seasons.id = history.season_id
        JOIN player_fixture_stats stats
          ON stats.player_season_id = history.id
        JOIN fixtures ON fixtures.id = stats.fixture_id
        JOIN gameweeks ON gameweeks.id = fixtures.gameweek_id
        WHERE seasons.code < ?
           OR (seasons.code = ? AND gameweeks.number < ?)
        GROUP BY current_players.current_player_season_id,
                 seasons.code, gameweeks.number, history.id
        ORDER BY current_players.current_player_season_id,
                 seasons.code, gameweeks.number
        """,
        (*player_season_ids, season_code, season_code, start_gameweek),
    ).fetchall()
    states: dict[int, dict[str, float | int | str | None]] = {}
    fast_decay = math.exp(math.log(0.5) / 3.0)
    minutes_decay = math.exp(math.log(0.5) / 5.0)
    for raw in histories:
        player_id = int(raw["current_player_season_id"])
        state = states.setdefault(player_id, _empty_player_state())
        minutes = int(raw["minutes"])
        appeared = int(minutes > 0)
        started = int(raw["started"])
        state["matches"] = float(state["matches"]) * fast_decay + 1.0
        state["appearances"] = (
            float(state["appearances"]) * fast_decay + appeared
        )
        state["starts"] = float(state["starts"]) * fast_decay + started
        state["sixty"] = (
            float(state["sixty"]) * fast_decay + int(minutes >= 60)
        )
        state["minutes"] = (
            float(state["minutes"]) * minutes_decay + minutes
        )
        state["previous_minutes"] = minutes
        state["previous_started"] = started
        state["absence_streak"] = (
            0 if appeared else int(state["absence_streak"]) + 1
        )
        state["substitute_run"] = (
            int(state["substitute_run"]) + 1
            if appeared and not started
            else 0
        )
        state["team_id"] = int(raw["team_id"])
        state["position"] = str(raw["position"])

    priors = database.connection.execute(
        """
        SELECT player_seasons.position, player_seasons.team_id,
               COUNT(stats.id) AS matches,
               SUM(stats.minutes > 0) AS appearances
        FROM player_fixture_stats stats
        JOIN player_seasons
          ON player_seasons.id = stats.player_season_id
        JOIN seasons ON seasons.id = player_seasons.season_id
        JOIN fixtures ON fixtures.id = stats.fixture_id
        JOIN gameweeks ON gameweeks.id = fixtures.gameweek_id
        WHERE seasons.code = ? AND gameweeks.number < ?
        GROUP BY player_seasons.position, player_seasons.team_id
        """,
        (season_code, start_gameweek),
    ).fetchall()
    position_values: dict[str, list[float]] = {}
    team_values: dict[int, list[float]] = {}
    for raw in priors:
        position = position_values.setdefault(
            str(raw["position"]), [0.0, 0.0]
        )
        team = team_values.setdefault(int(raw["team_id"]), [0.0, 0.0])
        for values in (position, team):
            values[0] += float(raw["matches"])
            values[1] += float(raw["appearances"])

    result = {}
    for player in players:
        player_id = int(player["player_season_id"])
        state = states.setdefault(player_id, _empty_player_state())
        position = str(player["position"])
        team_id = int(player["team_id"])
        position_counts = position_values.get(position, [0.0, 0.0])
        position_rate = _shrunk_rate(
            position_counts[1],
            position_counts[0],
            0.72,
            20.0,
        )
        team_counts = team_values.get(team_id, [0.0, 0.0])
        team_rate = _shrunk_rate(
            team_counts[1],
            team_counts[0],
            position_rate,
            8.0,
        )
        player_appearance = _shrunk_rate(
            float(state["appearances"]),
            float(state["matches"]),
            position_rate,
            3.0,
        )
        player_start = _shrunk_rate(
            float(state["starts"]),
            float(state["matches"]),
            0.62,
            3.0,
        )
        player_sixty = _shrunk_rate(
            float(state["sixty"]),
            float(state["appearances"]),
            0.72,
            2.0,
        )
        conditional_minutes = _shrunk_rate(
            float(state["minutes"]),
            float(state["appearances"]),
            72.0,
            2.0,
        )
        result[player_id] = (
            start_gameweek / 38.0,
            float(player["price_tenths"]),
            float(position == "GK"),
            float(position == "DEF"),
            float(position == "MID"),
            float(position == "FWD"),
            player_appearance,
            player_start,
            player_sixty,
            conditional_minutes,
            position_rate,
            team_rate,
            float(state["previous_minutes"]),
            float(state["previous_started"]),
            float(state["absence_streak"]),
            float(state["substitute_run"]),
            float(
                state["team_id"] is not None
                and state["team_id"] != team_id
            ),
            float(
                state["position"] is not None
                and state["position"] != position
            ),
        )
    return result


def _empty_player_state() -> dict[str, float | int | str | None]:
    return {
        "matches": 0.0,
        "appearances": 0.0,
        "starts": 0.0,
        "sixty": 0.0,
        "minutes": 0.0,
        "previous_minutes": 0,
        "previous_started": 0,
        "absence_streak": 0,
        "substitute_run": 0,
        "team_id": None,
        "position": None,
    }


def _classifier(family: HurdleFamily, seed: int) -> Any:
    if family == "logistic":
        from sklearn.linear_model import LogisticRegression
        from sklearn.pipeline import make_pipeline
        from sklearn.preprocessing import StandardScaler

        return make_pipeline(
            StandardScaler(),
            LogisticRegression(
                C=0.5,
                max_iter=1_000,
                random_state=seed,
            ),
        )
    from sklearn.ensemble import HistGradientBoostingClassifier

    return HistGradientBoostingClassifier(
        learning_rate=0.05,
        max_iter=150,
        max_leaf_nodes=15,
        l2_regularization=2.0,
        random_state=seed,
    )


def _regressor(family: HurdleFamily, seed: int) -> Any:
    if family == "logistic":
        from sklearn.linear_model import Ridge
        from sklearn.pipeline import make_pipeline
        from sklearn.preprocessing import StandardScaler

        return make_pipeline(StandardScaler(), Ridge(alpha=5.0))
    from sklearn.ensemble import HistGradientBoostingRegressor

    return HistGradientBoostingRegressor(
        loss="squared_error",
        learning_rate=0.05,
        max_iter=150,
        max_leaf_nodes=15,
        l2_regularization=2.0,
        random_state=seed,
    )


def _chronological_oof(
    rows: list[_HurdleRow],
    *,
    family: HurdleFamily,
    seed: int,
    target: Any,
) -> tuple[list[float], list[int]]:
    periods = sorted({(row.season_code, row.gameweek) for row in rows})
    if len(periods) < 4:
        return [], []
    boundaries = sorted(
        {
            max(1, int(len(periods) * fraction))
            for fraction in (0.5, 0.67, 0.83)
            if int(len(periods) * fraction) < len(periods)
        }
    )
    predictions: list[float] = []
    actual: list[int] = []
    previous = 0
    for boundary in boundaries:
        next_boundary = min(
            len(periods),
            boundary + max(1, len(periods) // 6),
        )
        train_periods = set(periods[:boundary])
        test_periods = set(periods[max(previous, boundary) : next_boundary])
        previous = next_boundary
        train_rows = [row for row in rows if (row.season_code, row.gameweek) in train_periods]
        test_rows = [row for row in rows if (row.season_code, row.gameweek) in test_periods]
        train_actual = [target(row) for row in train_rows]
        if not test_rows or len(set(train_actual)) < 2:
            continue
        model = _classifier(family, seed + boundary)
        model.fit([row.features for row in train_rows], train_actual)
        predictions.extend(
            float(value) for value in model.predict_proba([row.features for row in test_rows])[:, 1]
        )
        actual.extend(target(row) for row in test_rows)
    return predictions, actual


def _predict_hurdles(
    rows: list[_HurdleRow],
    models: dict[str, Any],
    calibrators: dict[str, Any | None],
) -> list[tuple[float, float, float, float]]:
    features = [row.features for row in rows]
    probabilities: dict[str, list[float]] = {}
    for name in ("appearance", "start", "sixty"):
        raw = [float(value) for value in models[name].predict_proba(features)[:, 1]]
        calibrator = calibrators[name]
        probabilities[name] = (
            raw if calibrator is None else [float(value) for value in calibrator.predict(raw)]
        )
    conditional_minutes = [
        min(90.0, max(1.0, float(value)))
        for value in models["conditional_minutes"].predict(features)
    ]
    result = []
    for index in range(len(rows)):
        appearance = _clamp_probability(probabilities["appearance"][index])
        start = min(
            appearance,
            _clamp_probability(probabilities["start"][index]),
        )
        sixty = appearance * _clamp_probability(probabilities["sixty"][index])
        result.append((appearance, start, sixty, conditional_minutes[index]))
    return result


def _build_hurdle_rows(
    database: HistoricalDatabase,
    seasons: tuple[str, ...],
) -> list[_HurdleRow]:
    placeholders = ",".join("?" for _ in seasons)
    source = database.connection.execute(
        f"""
        WITH latest_observations AS (
            SELECT player_season_id, gameweek_id, MAX(id) AS observation_id
            FROM player_gameweek_observations
            GROUP BY player_season_id, gameweek_id
        )
        SELECT seasons.code AS season_code,
               gameweeks.number AS gameweek,
               player_seasons.id AS player_season_id,
               player_seasons.player_id,
               player_seasons.team_id,
               player_seasons.position,
               COALESCE(observations.price_tenths,
                        player_seasons.start_price_tenths, 50) AS price_tenths,
               SUM(stats.minutes) AS minutes,
               MAX(stats.starts) AS started
        FROM player_fixture_stats stats
        JOIN player_seasons
          ON player_seasons.id = stats.player_season_id
        JOIN fixtures ON fixtures.id = stats.fixture_id
        JOIN gameweeks ON gameweeks.id = fixtures.gameweek_id
        JOIN seasons ON seasons.id = fixtures.season_id
        LEFT JOIN latest_observations latest
          ON latest.player_season_id = player_seasons.id
         AND latest.gameweek_id = gameweeks.id
        LEFT JOIN player_gameweek_observations observations
          ON observations.id = latest.observation_id
        WHERE seasons.code IN ({placeholders})
        GROUP BY seasons.code, gameweeks.number, player_seasons.id
        ORDER BY seasons.code, gameweeks.number, player_seasons.player_id
        """,
        seasons,
    ).fetchall()

    player_state: dict[int, dict[str, float | int | str | None]] = {}
    position_state: dict[str, list[float]] = {}
    team_state: dict[int, list[float]] = {}
    rows: list[_HurdleRow] = []
    fast_decay = math.exp(math.log(0.5) / 3.0)
    minutes_decay = math.exp(math.log(0.5) / 5.0)
    current_period: tuple[str, int] | None = None
    pending: list[Any] = []

    def process_period(period_rows: list[Any]) -> None:
        for raw in period_rows:
            player_id = int(raw["player_id"])
            team_id = int(raw["team_id"])
            position = str(raw["position"])
            state = player_state.setdefault(
                player_id,
                {
                    "matches": 0.0,
                    "appearances": 0.0,
                    "starts": 0.0,
                    "sixty": 0.0,
                    "minutes": 0.0,
                    "previous_minutes": 0,
                    "previous_started": 0,
                    "absence_streak": 0,
                    "substitute_run": 0,
                    "team_id": None,
                    "position": None,
                },
            )
            position_values = position_state.setdefault(position, [0.0, 0.0, 0.0])
            team_values = team_state.setdefault(team_id, [0.0, 0.0])
            position_rate = _shrunk_rate(position_values[1], position_values[0], 0.72, 20.0)
            team_rate = _shrunk_rate(team_values[1], team_values[0], position_rate, 8.0)
            player_appearance = _shrunk_rate(
                float(state["appearances"]),
                float(state["matches"]),
                position_rate,
                3.0,
            )
            player_start = _shrunk_rate(
                float(state["starts"]),
                float(state["matches"]),
                0.62,
                3.0,
            )
            player_sixty = _shrunk_rate(
                float(state["sixty"]),
                float(state["appearances"]),
                0.72,
                2.0,
            )
            conditional_minutes = _shrunk_rate(
                float(state["minutes"]),
                float(state["appearances"]),
                72.0,
                2.0,
            )
            features = (
                int(raw["gameweek"]) / 38.0,
                float(raw["price_tenths"]),
                float(position == "GK"),
                float(position == "DEF"),
                float(position == "MID"),
                float(position == "FWD"),
                player_appearance,
                player_start,
                player_sixty,
                conditional_minutes,
                position_rate,
                team_rate,
                float(state["previous_minutes"]),
                float(state["previous_started"]),
                float(state["absence_streak"]),
                float(state["substitute_run"]),
                float(state["team_id"] is not None and state["team_id"] != team_id),
                float(state["position"] is not None and state["position"] != position),
            )
            minutes = int(raw["minutes"])
            appeared = int(minutes > 0)
            started = int(raw["started"])
            rows.append(
                _HurdleRow(
                    season_code=str(raw["season_code"]),
                    gameweek=int(raw["gameweek"]),
                    player_season_id=int(raw["player_season_id"]),
                    features=features,
                    baseline_appearance=player_appearance,
                    baseline_start=min(player_appearance, player_start),
                    baseline_sixty=player_appearance * player_sixty,
                    baseline_conditional_minutes=conditional_minutes,
                    appeared=appeared,
                    started=started,
                    reached_sixty=int(minutes >= 60),
                    minutes=minutes,
                )
            )

        for raw in period_rows:
            player_id = int(raw["player_id"])
            team_id = int(raw["team_id"])
            position = str(raw["position"])
            minutes = int(raw["minutes"])
            appeared = int(minutes > 0)
            started = int(raw["started"])
            state = player_state[player_id]
            state["matches"] = float(state["matches"]) * fast_decay + 1.0
            state["appearances"] = float(state["appearances"]) * fast_decay + appeared
            state["starts"] = float(state["starts"]) * fast_decay + started
            state["sixty"] = float(state["sixty"]) * fast_decay + int(minutes >= 60)
            state["minutes"] = float(state["minutes"]) * minutes_decay + minutes
            state["previous_minutes"] = minutes
            state["previous_started"] = started
            state["absence_streak"] = 0 if appeared else int(state["absence_streak"]) + 1
            state["substitute_run"] = (
                int(state["substitute_run"]) + 1 if appeared and not started else 0
            )
            state["team_id"] = team_id
            state["position"] = position
            position_values = position_state.setdefault(position, [0.0, 0.0, 0.0])
            position_values[0] += 1.0
            position_values[1] += appeared
            position_values[2] += started
            team_values = team_state.setdefault(team_id, [0.0, 0.0])
            team_values[0] += 1.0
            team_values[1] += appeared

    for raw in source:
        period = (str(raw["season_code"]), int(raw["gameweek"]))
        if current_period is not None and period != current_period:
            process_period(pending)
            pending = []
        current_period = period
        pending.append(raw)
    if pending:
        process_period(pending)
    return rows


def _hurdle_metrics(
    rows: list[_HurdleRow],
    predictions: list[tuple[float, float, float, float]],
) -> HurdleMetrics:
    appearance_prob = [value[0] for value in predictions]
    start_prob = [value[1] for value in predictions]
    sixty_prob = [value[2] for value in predictions]
    conditional_minutes = [value[3] for value in predictions]
    expected_minutes = [prediction[0] * prediction[3] for prediction in predictions]
    appeared_rows = [
        (row, conditional_minutes[index]) for index, row in enumerate(rows) if row.appeared
    ]
    return HurdleMetrics(
        samples=len(rows),
        appearance_brier=_brier(appearance_prob, [row.appeared for row in rows]),
        appearance_log_loss=_log_loss(appearance_prob, [row.appeared for row in rows]),
        start_brier=_brier(start_prob, [row.started for row in rows]),
        start_log_loss=_log_loss(start_prob, [row.started for row in rows]),
        sixty_brier=_brier(sixty_prob, [row.reached_sixty for row in rows]),
        sixty_log_loss=_log_loss(sixty_prob, [row.reached_sixty for row in rows]),
        expected_minutes_rmse=_rmse(expected_minutes, [row.minutes for row in rows]),
        expected_minutes_bias=_bias(expected_minutes, [row.minutes for row in rows]),
        conditional_minutes_rmse=_rmse(
            [value for _, value in appeared_rows],
            [row.minutes for row, _ in appeared_rows],
        ),
    )


def _downstream_points_metrics(
    database: HistoricalDatabase,
    season_code: str,
    hurdle_rows: list[_HurdleRow],
    predictions: list[tuple[float, float, float, float]],
) -> DownstreamPointsMetrics | None:
    by_key = {
        (row.player_season_id, row.gameweek): prediction
        for row, prediction in zip(hurdle_rows, predictions, strict=True)
    }
    run = database.connection.execute(
        """
        SELECT runs.id
        FROM projection_backtest_runs runs
        JOIN seasons ON seasons.id = runs.season_id
        WHERE seasons.code = ? AND runs.status = 'completed'
          AND runs.horizon_gameweeks = 1
        ORDER BY runs.id DESC
        LIMIT 1
        """,
        (season_code,),
    ).fetchone()
    if run is None:
        return None
    source = database.connection.execute(
        """
        SELECT origin_gameweek, target_gameweek, player_season_id,
               expected_minutes, appearance_probability,
               sixty_probability, expected_points, actual_points,
               component_points_json
        FROM projection_backtest_predictions
        WHERE backtest_run_id = ? AND fixture_count = 1
          AND component_points_json IS NOT NULL
        """,
        (int(run["id"]),),
    ).fetchall()
    scored: list[dict[str, float | int]] = []
    for raw in source:
        prediction = by_key.get((int(raw["player_season_id"]), int(raw["target_gameweek"])))
        if prediction is None:
            continue
        components = json.loads(raw["component_points_json"])
        old_minutes = float(raw["expected_minutes"])
        old_sixty = float(raw["sixty_probability"])
        appearance, _, sixty, conditional_minutes = prediction
        new_minutes = appearance * conditional_minutes
        appearance_points = max(0.0, appearance - sixty) * float(
            components["_appearance_under_60_rule"]
        ) + sixty * float(components["_appearance_60_or_more_rule"])
        linear_names = (
            "goal",
            "assist",
            "save",
            "bonus",
            "deduction",
        )
        linear = sum(float(components[name]) for name in linear_names)
        linear *= new_minutes / old_minutes if old_minutes > 0 else 0.0
        gated = float(components["clean_sheet"])
        gated *= sixty / old_sixty if old_sixty > 0 else 0.0
        defensive = float(components["defensive_contribution"])
        defensive *= appearance / max(float(raw["appearance_probability"]), 1e-9)
        scored.append(
            {
                "origin": int(raw["origin_gameweek"]),
                "player": int(raw["player_season_id"]),
                "actual": float(raw["actual_points"]),
                "baseline": float(raw["expected_points"]),
                "hurdle": appearance_points + linear + gated + defensive,
            }
        )
    if not scored:
        return None
    baseline = [float(row["baseline"]) for row in scored]
    hurdle = [float(row["hurdle"]) for row in scored]
    actual = [float(row["actual"]) for row in scored]
    baseline_top_one, baseline_top_15 = _ranking_regret(scored, "baseline")
    hurdle_top_one, hurdle_top_15 = _ranking_regret(scored, "hurdle")
    baseline_rmse = _rmse(baseline, actual)
    hurdle_rmse = _rmse(hurdle, actual)
    return DownstreamPointsMetrics(
        samples=len(scored),
        baseline_rmse=baseline_rmse,
        hurdle_rmse=hurdle_rmse,
        rmse_change=round(hurdle_rmse - baseline_rmse, 6),
        baseline_bias=_bias(baseline, actual),
        hurdle_bias=_bias(hurdle, actual),
        global_top_one_regret_change=round(hurdle_top_one - baseline_top_one, 6),
        unconstrained_top_15_regret_change=round(hurdle_top_15 - baseline_top_15, 6),
        method=(
            "Component-backed sensitivity: replace appearance scoring, "
            "scale linear components by expected minutes and clean sheets by "
            "60-minute probability."
        ),
    )


def _ranking_regret(
    rows: list[dict[str, float | int]],
    forecast_name: str,
) -> tuple[float, float]:
    groups: dict[int, list[dict[str, float | int]]] = {}
    for row in rows:
        groups.setdefault(int(row["origin"]), []).append(row)
    top_one = []
    top_15 = []
    for group in groups.values():
        predicted = sorted(
            group,
            key=lambda row: (
                -float(row[forecast_name]),
                int(row["player"]),
            ),
        )
        actual = sorted(
            group,
            key=lambda row: (
                -float(row["actual"]),
                int(row["player"]),
            ),
        )
        top_one.append(float(actual[0]["actual"]) - float(predicted[0]["actual"]))
        top_15.append(
            sum(float(row["actual"]) for row in actual[:15])
            - sum(float(row["actual"]) for row in predicted[:15])
        )
    return sum(top_one) / len(top_one), sum(top_15) / len(top_15)


def _shrunk_rate(
    successes: float,
    trials: float,
    prior: float,
    prior_weight: float,
) -> float:
    return (successes + prior * prior_weight) / (trials + prior_weight)


def _clamp_probability(value: float) -> float:
    return min(0.999, max(0.001, value))


def _brier(predicted: list[float], actual: list[int]) -> float:
    return round(
        sum(
            (prediction - outcome) ** 2
            for prediction, outcome in zip(predicted, actual, strict=True)
        )
        / len(actual),
        6,
    )


def _log_loss(predicted: list[float], actual: list[int]) -> float:
    return round(
        -sum(
            outcome * math.log(_clamp_probability(prediction))
            + (1 - outcome) * math.log(1 - _clamp_probability(prediction))
            for prediction, outcome in zip(predicted, actual, strict=True)
        )
        / len(actual),
        6,
    )


def _rmse(predicted: list[float], actual: list[float | int]) -> float:
    return round(
        math.sqrt(
            sum(
                (outcome - prediction) ** 2
                for prediction, outcome in zip(predicted, actual, strict=True)
            )
            / len(actual)
        ),
        6,
    )


def _bias(predicted: list[float], actual: list[float | int]) -> float:
    return round(
        sum(outcome - prediction for prediction, outcome in zip(predicted, actual, strict=True))
        / len(actual),
        6,
    )
