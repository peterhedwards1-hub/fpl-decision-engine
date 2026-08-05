"""Command-line interface for the historical database."""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path

from ..allocation_variants import evaluate_allocation_variants
from ..assumption_audit import run_assumption_audit
from ..backtest import ProjectionBacktester, load_backtest_report
from ..capture import capture_gameweek_forecasts
from ..championship import (
    DEFAULT_CHAMPIONSHIP_DATA_PATH,
    import_championship_document,
    load_championship_document,
)
from ..chip_state import LookaheadChipPolicy, ScoringChipPolicy
from ..config import load_season_rules
from ..declaration import ModelDeclaration
from ..diagnostics import (
    SUPPORTED_BASELINES,
    build_stage_one_diagnostics,
    write_stage_one_diagnostics,
)
from ..evaluation import (
    SquadConstructionPolicy,
    build_evaluation_suite,
    compare_backtest_to_baselines,
    compile_squad_policy_evaluations,
    compile_transfer_policy_evaluation,
    evaluate_chip_regret,
    evaluate_legal_squad_regret,
    evaluate_owned_captain_regret,
    evaluate_squad_construction_policies,
    evaluate_transfer_regret,
    replay_backtest_transfer_continuity,
    write_json_report,
)
from ..frontier_validation import (
    validate_opening_squad_search,
    write_search_validation_artifacts,
)
from ..learned_challenger import train_and_evaluate_learned_challenger
from ..news import ingest_structured_news
from ..optimisation import DEFAULT_OPENING_MINIMUM_MEAN_APPEARANCE
from ..playing_time import train_and_evaluate_hurdle_model
from ..preseason_final import (
    DEFAULT_FRONTIER_SIZE,
    finalise_preseason_squad,
    load_final_squad,
    write_artifacts,
)
from ..preseason_fit import fit_preseason_priors, profile_preseason_prior
from ..preseason_strength import (
    squad_comparison_artifact,
    validate_preseason_strength,
    write_preseason_validation_markdown,
)
from ..projections import (
    DEFAULT_MODEL_CONFIG,
    MODEL_VERSION,
    PRESEASON_V5_MODEL_CONFIG,
    ProjectionModelConfig,
    RatesProjectionModel,
)
from ..promoted_roles import import_role_document, load_role_document
from ..promotion import (
    DecisionGateEvidence,
    PromotionGatePolicy,
    build_decision_gate_evidence,
    evaluate_forward_candidate,
    load_forward_candidate,
    register_forward_candidate,
    run_forward_candidate_pair,
)
from ..prospective import build_prospective_capture_status
from ..readiness import build_preseason_readiness_report
from ..research_decision import (
    compare_opening_squad_decision,
    compare_transfer_decision,
    compare_weekly_xi_decision,
    generate_revised_projection,
)
from ..reviewed_modifiers import ReviewedProjectionModifier
from ..squad_comparison import compare_opening_squads
from ..team_news_v3 import generate_team_news_research_package
from ..team_strength import ContextualAdjustment
from ..team_strength_report import (
    build_team_strength_report,
    evaluate_team_strength_models,
)
from ..transfers import CurrentSquad
from ..tuning import tune_projection_model, tune_projection_model_rolling
from .csv_bundle import load_csv_bundle
from .database import HistoricalDatabase
from .records import IngestionSource, SeasonRecord
from .vaastav import VaastavAdapter, VaastavClient


def _load_declaration(path: str) -> ModelDeclaration:
    """Read a candidate file in either the declaration or the legacy shape."""

    return ModelDeclaration.from_dict(
        json.loads(Path(path).read_text(encoding="utf-8"))
    )


def _load_config(path: str) -> ProjectionModelConfig:
    """The projection config from a candidate file, for callers needing only it."""

    return _load_declaration(path).model_config


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage historical FPL data")
    parser.add_argument("--database", default="data/fpl_history.sqlite3")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="Create or upgrade the database schema")

    import_parser = subparsers.add_parser("import-csv", help="Import a normalised CSV bundle")
    import_parser.add_argument("directory")
    import_parser.add_argument("--season-code", required=True)
    import_parser.add_argument("--season-name", required=True)
    import_parser.add_argument("--source-name", required=True)
    import_parser.add_argument("--identifier-namespace", default="official-fpl")
    import_parser.add_argument("--source-url")
    import_parser.add_argument("--source-revision")
    import_parser.add_argument("--adapter-version")
    import_parser.add_argument("--starts-on")
    import_parser.add_argument("--ends-on")

    vaastav_parser = subparsers.add_parser(
        "import-vaastav",
        help="Download and import historical seasons from an immutable Vaastav revision",
    )
    vaastav_parser.add_argument("--source-ref", required=True)
    vaastav_parser.add_argument("--seasons", nargs="+", required=True)
    vaastav_parser.add_argument(
        "--source-base-url",
        default=("https://raw.githubusercontent.com/vaastav/Fantasy-Premier-League"),
    )

    summary_parser = subparsers.add_parser("summary", help="Show season row counts")
    summary_parser.add_argument("season_code")

    backtest_parser = subparsers.add_parser(
        "backtest-projections",
        help="Walk historical Gameweeks forward and score projection accuracy",
    )
    backtest_parser.add_argument("season_code")
    backtest_parser.add_argument(
        "--rules",
        help="Season rules JSON (defaults to config/seasons/<season>.json)",
    )
    backtest_parser.add_argument("--origin-start", type=int, default=2)
    backtest_parser.add_argument("--origin-end", type=int, default=38)
    backtest_parser.add_argument("--horizon", type=int, default=1)
    backtest_parser.add_argument(
        "--evidence-policy",
        choices=("performance_only", "pre_deadline_only"),
        default="performance_only",
    )
    backtest_parser.add_argument("--model-version", default=MODEL_VERSION)
    backtest_parser.add_argument(
        "--model-config",
        help=(
            "Full projection configuration JSON to run instead of the incumbent "
            "defaults. Individual flags below still override single fields."
        ),
    )
    backtest_parser.add_argument("--player-prior-minutes", type=float)
    backtest_parser.add_argument("--minutes-prior-matches", type=float)
    backtest_parser.add_argument("--team-prior-matches", type=float)
    backtest_parser.add_argument("--home-attack-multiplier", type=float)
    backtest_parser.add_argument("--away-attack-multiplier", type=float)
    backtest_parser.add_argument(
        "--minutes-model",
        choices=("legacy", "two_stage", "learned_hurdle"),
    )
    backtest_parser.add_argument("--playing-time-artifact")
    backtest_parser.add_argument("--recent-gameweeks", type=int)
    backtest_parser.add_argument("--recent-evidence-weight", type=float)
    backtest_parser.add_argument(
        "--scoring-event-source",
        choices=(
            "actual",
            "expected_with_actual_fallback",
            "team_share_expected",
        ),
    )
    backtest_parser.add_argument(
        "--defensive-contribution-model",
        choices=(
            "threshold_poisson",
            "empirical_2025_minutes_band",
        ),
    )
    backtest_parser.add_argument("--appearance-prior-matches", type=float)
    backtest_parser.add_argument("--appearance-prior-probability", type=float)
    backtest_parser.add_argument(
        "--conditional-minutes-prior-appearances",
        type=float,
    )
    backtest_parser.add_argument(
        "--no-team-minute-constraint",
        action="store_true",
    )

    tune_parser = subparsers.add_parser(
        "tune-projections",
        help="Tune the two-stage projection model on a development window",
    )
    tune_parser.add_argument("season_code")
    tune_parser.add_argument(
        "--rules",
        help="Season rules JSON (defaults to config/seasons/<season>.json)",
    )
    tune_parser.add_argument("--development-start", type=int, default=2)
    tune_parser.add_argument("--development-end", type=int, default=25)
    tune_parser.add_argument("--validation-start", type=int, default=26)
    tune_parser.add_argument("--validation-end", type=int, default=38)
    tune_parser.add_argument("--horizon", type=int, default=1)
    tune_parser.add_argument("--trials", type=int, default=30)
    tune_parser.add_argument("--study-name", default="fpl-rates-two-stage-v2")
    tune_parser.add_argument(
        "--study-storage",
        default="sqlite:///data/fpl_tuning.sqlite3",
    )
    tune_parser.add_argument("--seed", type=int, default=20260729)

    rolling_parser = subparsers.add_parser(
        "tune-projections-rolling",
        help=("Tune across development seasons and evaluate one locked validation season"),
    )
    rolling_parser.add_argument(
        "--development-seasons",
        nargs="+",
        required=True,
    )
    rolling_parser.add_argument("--validation-season", required=True)
    rolling_parser.add_argument(
        "--rules-directory",
        default="config/seasons",
    )
    rolling_parser.add_argument("--origin-start", type=int, default=2)
    rolling_parser.add_argument("--origin-end", type=int, default=38)
    rolling_parser.add_argument("--horizon", type=int, default=1)
    rolling_parser.add_argument(
        "--trials",
        type=int,
        default=50,
        help="Target number of completed trials; safe to reuse after interruption",
    )
    rolling_parser.add_argument(
        "--study-name",
        default="fpl-rates-rolling-v3",
    )
    rolling_parser.add_argument(
        "--study-storage",
        default="sqlite:///data/fpl_tuning.sqlite3",
    )
    rolling_parser.add_argument("--seed", type=int, default=20260729)

    preseason_parser = subparsers.add_parser(
        "fit-preseason-priors",
        help="Estimate the Stage 3a carry-forward and cold-start parameters",
    )
    preseason_parser.add_argument(
        "--target-seasons",
        nargs="+",
        required=True,
        help="Development seasons to fit on; each needs an earlier season present",
    )
    preseason_parser.add_argument("--origin-start", type=int, default=1)
    preseason_parser.add_argument("--origin-end", type=int, default=8)
    preseason_parser.add_argument("--horizon", type=int, default=1)
    preseason_parser.add_argument(
        "--design-size",
        type=int,
        default=48,
        help="Fixed Halton design points, generated before any outcome is seen",
    )
    preseason_parser.add_argument(
        "--confirmation-horizons",
        nargs="*",
        type=int,
        default=[8],
        help="Horizons to re-score the fitted configuration on",
    )
    preseason_parser.add_argument(
        "--config-output",
        help="Write the fitted configuration for register-forward-candidate",
    )
    preseason_parser.add_argument("--output")
    profile_parser = subparsers.add_parser(
        "profile-preseason-prior",
        help="Sweep one Stage 3a parameter with the others held fixed",
    )
    profile_parser.add_argument("parameter")
    profile_parser.add_argument("--target-seasons", nargs="+", required=True)
    profile_parser.add_argument("--low", type=float, required=True)
    profile_parser.add_argument("--high", type=float, required=True)
    profile_parser.add_argument("--steps", type=int, default=7)
    profile_parser.add_argument("--origin-start", type=int, default=1)
    profile_parser.add_argument("--origin-end", type=int, default=8)
    profile_parser.add_argument("--horizon", type=int, default=1)
    profile_parser.add_argument(
        "--base-config",
        help="Configuration to hold the other parameters at (defaults to v1)",
    )
    profile_parser.add_argument("--output")
    squads_parser = subparsers.add_parser(
        "compare-opening-squads",
        help="Build and cross-value the squads two configurations pick",
    )
    squads_parser.add_argument("season_code")
    squads_parser.add_argument("--first-label", default="v1")
    squads_parser.add_argument("--first-config", required=True)
    squads_parser.add_argument("--second-label", default="v2")
    squads_parser.add_argument("--second-config", required=True)
    squads_parser.add_argument("--gameweek", type=int, default=1)
    squads_parser.add_argument("--horizon", type=int, default=8)
    squads_parser.add_argument(
        "--sensitivity-parameters",
        nargs="*",
        default=[],
        help="Parameters to perturb up and down when testing squad stability",
    )
    squads_parser.add_argument(
        "--sensitivity-fraction",
        type=float,
        default=0.25,
    )
    squads_parser.add_argument("--rules")
    squads_parser.add_argument("--output")
    challenger_parser = subparsers.add_parser(
        "train-boosted-challenger",
        help=("Train on earlier completed backtests and evaluate one later validation run"),
    )
    challenger_parser.add_argument(
        "--training-run-ids",
        nargs="+",
        type=int,
        required=True,
    )
    challenger_parser.add_argument(
        "--validation-run-id",
        type=int,
        required=True,
    )
    challenger_parser.add_argument(
        "--artifact",
        default="data/models/boosted-points-v1.joblib",
    )
    challenger_parser.add_argument(
        "--loss",
        choices=("absolute_error", "squared_error", "poisson"),
        default="absolute_error",
    )
    challenger_parser.add_argument("--seed", type=int, default=20260729)
    hurdle_parser = subparsers.add_parser(
        "train-playing-time-hurdle",
        help=(
            "Train and chronologically calibrate appearance, start, "
            "60-minute and conditional-minutes challengers"
        ),
    )
    hurdle_parser.add_argument(
        "--training-seasons",
        nargs="+",
        required=True,
    )
    hurdle_parser.add_argument("--validation-season", required=True)
    hurdle_parser.add_argument(
        "--family",
        choices=("logistic", "histogram_gradient_boosting"),
        default="logistic",
    )
    hurdle_parser.add_argument(
        "--artifact",
        default="data/models/playing-time-hurdle-v1.joblib",
    )
    hurdle_parser.add_argument("--seed", type=int, default=20260730)

    audit_parser = subparsers.add_parser(
        "audit-projection-assumptions",
        help=("Compare predeclared football assumptions on development seasons only"),
    )
    audit_parser.add_argument(
        "--development-seasons",
        nargs="+",
        required=True,
    )
    audit_parser.add_argument(
        "--rules-directory",
        default="config/seasons",
    )
    audit_parser.add_argument("--origin-start", type=int, default=2)
    audit_parser.add_argument("--origin-end", type=int, default=38)
    audit_parser.add_argument("--horizon", type=int, default=1)
    audit_parser.add_argument(
        "--output",
        default="data/models/assumption-audit-v1.json",
    )
    audit_parser.add_argument(
        "--artifact-directory",
        default="data/models/assumption-audit",
    )
    audit_parser.add_argument("--seed", type=int, default=20260729)

    report_parser = subparsers.add_parser(
        "backtest-report", help="Show a completed persisted backtest scorecard"
    )
    report_parser.add_argument("run_id", type=int)
    readiness_parser = subparsers.add_parser(
        "preseason-readiness",
        help="Produce one qualified projection and robust opening-squad report",
    )
    readiness_parser.add_argument("season_code")
    readiness_parser.add_argument("--gameweek", type=int, default=1)
    readiness_parser.add_argument("--horizon", type=int, default=8)
    readiness_parser.add_argument("--candidate-pool-size", type=int, default=8)
    readiness_parser.add_argument("--appearance-floor", type=float, default=0.6)
    readiness_parser.add_argument("--rules")
    readiness_parser.add_argument("--output")
    baseline_parser = subparsers.add_parser(
        "compare-backtest-baselines",
        help="Compare one completed backtest with leakage-controlled simple baselines",
    )
    baseline_parser.add_argument("run_id", type=int)
    baseline_parser.add_argument(
        "--output",
        help="Optional JSON output path",
    )
    regret_parser = subparsers.add_parser(
        "evaluate-squad-regret",
        help="Replay a backtest as legal £100m multi-Gameweek squad decisions",
    )
    regret_parser.add_argument("run_id", type=int)
    regret_parser.add_argument(
        "--rules",
        help="Season rules JSON (defaults to the backtest season)",
    )
    regret_parser.add_argument(
        "--methods",
        nargs="+",
        choices=(
            "model",
            "season_points_per_fixture",
            "recent_4_points_per_fixture",
            "season_points_per_90_model_minutes",
            "position_points_per_fixture",
        ),
        default=(
            "model",
            "season_points_per_fixture",
            "recent_4_points_per_fixture",
            "season_points_per_90_model_minutes",
            "position_points_per_fixture",
        ),
    )
    squad_policy_parser = subparsers.add_parser(
        "evaluate-squad-policies",
        help=(
            "Compare unrestricted and appearance-qualified opening-squad policies"
        ),
    )
    squad_policy_parser.add_argument("run_id", type=int)
    squad_policy_parser.add_argument(
        "--rules",
        help="Season rules JSON (defaults to the backtest season)",
    )
    squad_policy_parser.add_argument(
        "--origins",
        nargs="+",
        type=int,
        help="Optional subset of historical origin Gameweeks",
    )
    squad_policy_parser.add_argument(
        "--candidate-pool-size",
        type=int,
        default=4,
        help="Distinct solver candidates exactly rescored for each frontier policy",
    )
    squad_policy_parser.add_argument(
        "--appearance-floors",
        nargs="+",
        type=float,
        default=(0.6,),
        help="Mean projected appearance floors to compare with the baseline",
    )
    squad_policy_parser.add_argument("--output")
    squad_policy_suite_parser = subparsers.add_parser(
        "compile-squad-policy-evaluation",
        help="Pool opening-squad policy evidence across historical seasons",
    )
    squad_policy_suite_parser.add_argument("run_ids", nargs="+", type=int)
    squad_policy_suite_parser.add_argument(
        "--origins",
        nargs="+",
        type=int,
        help="Optional common subset of origin Gameweeks",
    )
    squad_policy_suite_parser.add_argument(
        "--candidate-pool-size",
        type=int,
        default=4,
    )
    squad_policy_suite_parser.add_argument(
        "--appearance-floors",
        nargs="+",
        type=float,
        default=(0.6,),
    )
    squad_policy_suite_parser.add_argument("--bootstrap-samples", type=int, default=2000)
    squad_policy_suite_parser.add_argument("--seed", type=int, default=20260804)
    squad_policy_suite_parser.add_argument("--output")
    continuity_parser = subparsers.add_parser(
        "replay-transfer-continuity",
        help="Replay a backtest as one persistent squad with hits and autosubs",
    )
    continuity_parser.add_argument("run_id", type=int)
    continuity_parser.add_argument(
        "--rules",
        help="Season rules JSON (defaults to the backtest season)",
    )
    continuity_parser.add_argument("--first-gameweek", type=int)
    continuity_parser.add_argument("--last-gameweek", type=int)
    continuity_parser.add_argument("--max-transfers-per-week", type=int, default=2)
    continuity_parser.add_argument("--candidate-pool-size", type=int, default=1)
    continuity_parser.add_argument("--output")
    transfer_policy_parser = subparsers.add_parser(
        "compile-transfer-policy-evaluation",
        help="Replay seasons and estimate saved-transfer option value",
    )
    transfer_policy_parser.add_argument("run_ids", nargs="+", type=int)
    transfer_policy_parser.add_argument("--first-gameweek", type=int)
    transfer_policy_parser.add_argument("--last-gameweek", type=int)
    transfer_policy_parser.add_argument("--max-transfers-per-week", type=int, default=2)
    transfer_policy_parser.add_argument("--candidate-pool-size", type=int, default=1)
    transfer_policy_parser.add_argument("--output")
    captain_parser = subparsers.add_parser(
        "evaluate-captain-regret",
        help="Score captaincy against the best armband in the model's own squad",
    )
    captain_parser.add_argument("run_id", type=int)
    captain_parser.add_argument("--rules")
    captain_parser.add_argument("--method", default="model")
    captain_parser.add_argument("--output")
    transfer_regret_parser = subparsers.add_parser(
        "evaluate-transfer-regret",
        help="Score each transfer action against the best from the same state",
    )
    transfer_regret_parser.add_argument("run_id", type=int)
    transfer_regret_parser.add_argument("--rules")
    transfer_regret_parser.add_argument("--first-gameweek", type=int)
    transfer_regret_parser.add_argument("--last-gameweek", type=int)
    transfer_regret_parser.add_argument(
        "--max-transfers-per-week",
        type=int,
        default=1,
    )
    transfer_regret_parser.add_argument("--candidate-pool-size", type=int, default=1)
    transfer_regret_parser.add_argument("--output")
    chip_regret_parser = subparsers.add_parser(
        "evaluate-chip-regret",
        help="Score Bench Boost and Triple Captain timing against the best week",
    )
    chip_regret_parser.add_argument("run_id", type=int)
    chip_regret_parser.add_argument("--rules")
    chip_regret_parser.add_argument("--first-gameweek", type=int)
    chip_regret_parser.add_argument("--last-gameweek", type=int)
    chip_regret_parser.add_argument("--candidate-pool-size", type=int, default=1)
    chip_regret_parser.add_argument(
        "--max-transfers-per-week",
        type=int,
        default=1,
    )
    chip_regret_parser.add_argument(
        "--bench-boost-threshold",
        type=float,
        help="Expected points at which the replay plays Bench Boost",
    )
    chip_regret_parser.add_argument(
        "--triple-captain-threshold",
        type=float,
        help="Expected points at which the replay plays Triple Captain",
    )
    chip_regret_parser.add_argument(
        "--lookahead",
        action="store_true",
        help=(
            "Hold a chip while a better Gameweek remains before the set "
            "expires, instead of playing the first week clearing a threshold"
        ),
    )
    chip_regret_parser.add_argument(
        "--lookahead-minimum-gain",
        type=float,
        default=0.0,
        help="Refuse to play a chip worth less than this",
    )
    chip_regret_parser.add_argument(
        "--lookahead-margin",
        type=float,
        default=0.0,
        help="Points by which this week must beat the best later week",
    )
    chip_regret_parser.add_argument("--output")
    suite_parser = subparsers.add_parser(
        "compile-model-evaluation",
        help="Compile horizon, baseline and challenger gates from backtest runs",
    )
    suite_parser.add_argument(
        "--incumbent-runs",
        nargs="+",
        type=int,
        required=True,
    )
    suite_parser.add_argument(
        "--challenger-runs",
        nargs="*",
        type=int,
        default=(),
    )
    suite_parser.add_argument("--output", required=True)
    diagnostics_parser = subparsers.add_parser(
        "stage-one-diagnostics",
        help=(
            "Build paired bootstrap, calibration, residual-slice and oracle "
            "diagnostics from completed backtests"
        ),
    )
    diagnostics_parser.add_argument(
        "--run-ids",
        nargs="+",
        type=int,
        required=True,
    )
    diagnostics_parser.add_argument(
        "--baseline",
        choices=SUPPORTED_BASELINES,
        default="season_points_per_fixture",
    )
    diagnostics_parser.add_argument(
        "--bootstrap-samples",
        type=int,
        default=2_000,
    )
    diagnostics_parser.add_argument(
        "--moving-block-gameweeks",
        type=int,
        default=3,
    )
    diagnostics_parser.add_argument(
        "--minimum-slice-samples",
        type=int,
        default=100,
    )
    diagnostics_parser.add_argument("--seed", type=int, default=20260730)
    diagnostics_parser.add_argument("--output", required=True)
    capture_parser = subparsers.add_parser(
        "prospective-capture-status",
        help="Report missing live evidence for each prospective Gameweek",
    )
    capture_parser.add_argument("season_code")
    capture_parser.add_argument("--output")
    forecast_capture_parser = subparsers.add_parser(
        "capture-gameweek-forecasts",
        help="Persist incumbent and every declared candidate before a deadline",
    )
    forecast_capture_parser.add_argument("season_code")
    forecast_capture_parser.add_argument("--gameweek", type=int, required=True)
    forecast_capture_parser.add_argument("--horizon", type=int, default=8)
    forecast_capture_parser.add_argument(
        "--incumbent-config",
        help="Incumbent configuration JSON (defaults to the production model)",
    )
    forecast_capture_parser.add_argument(
        "--incumbent-model-version",
        default=MODEL_VERSION,
    )
    forecast_capture_parser.add_argument("--rules")
    forecast_capture_parser.add_argument("--output")
    register_parser = subparsers.add_parser(
        "register-forward-candidate",
        help="Immutably declare a model configuration and promotion policy",
    )
    register_parser.add_argument("candidate_key")
    register_parser.add_argument("--season-code", required=True)
    register_parser.add_argument("--model-version", required=True)
    register_parser.add_argument("--model-config", required=True)
    register_parser.add_argument("--gate-policy")
    qualify_parser = subparsers.add_parser(
        "evaluate-forward-candidate",
        help="Apply two-tier gates to matched post-registration forward runs",
    )
    qualify_parser.add_argument("candidate_key")
    qualify_parser.add_argument(
        "--incumbent-runs",
        nargs="+",
        type=int,
        required=True,
    )
    qualify_parser.add_argument(
        "--challenger-runs",
        nargs="+",
        type=int,
        required=True,
    )
    qualify_parser.add_argument("--decision-evidence", required=True)
    qualify_parser.add_argument("--seed", type=int, default=20260730)
    qualify_parser.add_argument("--finalize-failure", action="store_true")
    qualify_parser.add_argument("--output")
    project_candidate_parser = subparsers.add_parser(
        "project-forward-candidate",
        help="Generate a pre-deadline run from an immutable candidate declaration",
    )
    project_candidate_parser.add_argument("candidate_key")
    project_candidate_parser.add_argument(
        "--start-gameweek",
        type=int,
        required=True,
    )
    project_candidate_parser.add_argument("--horizon", type=int, default=8)
    project_candidate_parser.add_argument("--rules")
    candidate_backtest_parser = subparsers.add_parser(
        "backtest-forward-candidate",
        help="Score a declared candidate and its control over one matched scope",
    )
    candidate_backtest_parser.add_argument("candidate_key")
    candidate_backtest_parser.add_argument(
        "--incumbent-config",
        required=True,
        help="Declared control configuration JSON the candidate is measured against",
    )
    candidate_backtest_parser.add_argument(
        "--incumbent-model-version",
        default=MODEL_VERSION,
    )
    candidate_backtest_parser.add_argument("--origin-start", type=int, default=1)
    candidate_backtest_parser.add_argument("--origin-end", type=int, default=8)
    candidate_backtest_parser.add_argument("--horizon", type=int, default=1)
    candidate_backtest_parser.add_argument("--rules")
    candidate_backtest_parser.add_argument("--output")
    evidence_parser = subparsers.add_parser(
        "build-decision-evidence",
        help="Measure the legal-squad decision gate from a matched run pair",
    )
    evidence_parser.add_argument("--incumbent-run", type=int, required=True)
    evidence_parser.add_argument("--challenger-run", type=int, required=True)
    evidence_parser.add_argument("--method", default="model")
    evidence_parser.add_argument(
        "--max-transfers-per-week",
        type=int,
        default=1,
    )
    evidence_parser.add_argument("--rules")
    evidence_parser.add_argument("--output")
    strength_parser = subparsers.add_parser(
        "team-strength-report",
        help="Explain every club's opponent-adjusted rating at one origin",
    )
    strength_parser.add_argument("season_code")
    strength_parser.add_argument("--gameweek", type=int, default=1)
    strength_parser.add_argument(
        "--adjustments",
        help=(
            "JSON file holding a list of reviewed contextual adjustments. "
            "Each needs a source_team_id, category and rationale; nothing is "
            "applied without one."
        ),
    )
    strength_parser.add_argument("--rules")
    strength_parser.add_argument("--output")
    strength_evaluation_parser = subparsers.add_parser(
        "evaluate-team-strength",
        help="Score team-goal accuracy and clean-sheet calibration by origin",
    )
    strength_evaluation_parser.add_argument("season_code")
    strength_evaluation_parser.add_argument("--origin-start", type=int, default=1)
    strength_evaluation_parser.add_argument("--origin-end", type=int, default=38)
    strength_evaluation_parser.add_argument("--rules")
    strength_evaluation_parser.add_argument("--output")
    preseason_strength_parser = subparsers.add_parser(
        "validate-preseason-strength",
        help=(
            "Compare the flat preseason team-strength model against a "
            "regressed previous-season carry-forward, apply the decision "
            "gate, and regenerate the opening squad from the winner"
        ),
    )
    preseason_strength_parser.add_argument("season_code")
    preseason_strength_parser.add_argument("--horizon", type=int, default=8)
    preseason_strength_parser.add_argument("--gameweek", type=int, default=1)
    preseason_strength_parser.add_argument(
        "--candidate-pool-size", type=int, default=8
    )
    preseason_strength_parser.add_argument(
        "--appearance-floor",
        type=float,
        default=DEFAULT_OPENING_MINIMUM_MEAN_APPEARANCE,
    )
    preseason_strength_parser.add_argument(
        "--skip-reference-model",
        action="store_true",
        help=(
            "Omit the opponent-adjusted secondary benchmark. It is never "
            "promoted from here; skipping it only saves runtime."
        ),
    )
    preseason_strength_parser.add_argument(
        "--skip-robustness",
        action="store_true",
        help="Omit the six declared stress tests",
    )
    preseason_strength_parser.add_argument(
        "--no-live-projection",
        action="store_true",
        help="Evaluate and gate without persisting a live projection run",
    )
    preseason_strength_parser.add_argument("--rules")
    preseason_strength_parser.add_argument("--output")
    preseason_strength_parser.add_argument(
        "--comparison-output",
        help="Where to write the cross-model squad comparison document",
    )
    preseason_strength_parser.add_argument(
        "--markdown-output", help="Where to write the readable summary"
    )

    finalise_parser = subparsers.add_parser(
        "finalise-preseason-squad",
        help=(
            "Validate the differentiated promoted-club prior and the "
            "promoted-player role evidence, generate a fresh projection under "
            "the validated model, search a broad exact squad frontier with "
            "goalkeeper pairs, bank levels and forced-inclusion "
            "counterfactuals, and write the provisional opening squad"
        ),
    )
    finalise_parser.add_argument("season_code")
    finalise_parser.add_argument("--horizon", type=int, default=8)
    finalise_parser.add_argument("--gameweek", type=int, default=1)
    finalise_parser.add_argument(
        "--frontier-size",
        type=int,
        default=DEFAULT_FRONTIER_SIZE,
        help="How many distinct complete legal squads to enumerate and rescore",
    )
    finalise_parser.add_argument(
        "--alternatives",
        type=int,
        default=3,
        help="How many runners-up to report in full",
    )
    finalise_parser.add_argument(
        "--appearance-floor",
        type=float,
        default=DEFAULT_OPENING_MINIMUM_MEAN_APPEARANCE,
    )
    finalise_parser.add_argument(
        "--decision-evidence",
        action="store_true",
        help=(
            "Also replay a historical opening squad per transition per prior. "
            "Secondary evidence only, and it dominates runtime."
        ),
    )
    finalise_parser.add_argument(
        "--no-modifiers",
        action="store_true",
        help="Do not apply accepted reviewed research modifiers to the live run",
    )
    finalise_parser.add_argument("--rules")
    finalise_parser.add_argument(
        "--output",
        default=None,
        help=(
            "Validation artifact path; defaults to "
            "data/models/preseason-final-validation-<season>.json"
        ),
    )
    finalise_parser.add_argument("--squad-output")
    finalise_parser.add_argument("--markdown-output")
    finalise_parser.add_argument(
        "--quiet",
        action="store_true",
        help="Print a short summary instead of the whole artifact",
    )

    search_parser = subparsers.add_parser(
        "validate-opening-squad-search",
        help=(
            "Compare the eight-candidate frontier, the forty-candidate "
            "frontier and the mixed candidate search on the same players, and "
            "report diversity, convergence and whether a forced-player "
            "diagnostic can still escape the pool"
        ),
    )
    search_parser.add_argument("season_code")
    search_parser.add_argument("--horizon", type=int, default=8)
    search_parser.add_argument("--gameweek", type=int, default=1)
    search_parser.add_argument(
        "--mixed-scale",
        type=float,
        default=1.0,
        help="Generation scale for the live season's mixed pool",
    )
    search_parser.add_argument(
        "--historical-scale",
        type=float,
        default=0.1,
        help="Generation scale for each historical season's mixed pool",
    )
    search_parser.add_argument(
        "--skip-historical",
        action="store_true",
        help="Compare on the live season only",
    )
    search_parser.add_argument(
        "--appearance-floor",
        type=float,
        default=DEFAULT_OPENING_MINIMUM_MEAN_APPEARANCE,
    )
    search_parser.add_argument("--rules")
    search_parser.add_argument("--output")
    search_parser.add_argument("--markdown-output")
    search_parser.add_argument("--quiet", action="store_true")

    import_championship_parser = subparsers.add_parser(
        "import-championship",
        help=(
            "Import the documented Championship season goal file used to vary "
            "the promoted-club prior"
        ),
    )
    import_championship_parser.add_argument(
        "--path", default=str(DEFAULT_CHAMPIONSHIP_DATA_PATH)
    )
    import_championship_parser.add_argument(
        "--roles-path",
        help=(
            "Optional Championship player role file. Absent by default: no "
            "public source of Championship player minutes is available to this "
            "project."
        ),
    )

    variants_parser = subparsers.add_parser(
        "evaluate-allocation-variants",
        help=(
            "Separate the team-strength change from the allocation change "
            "across four controlled variants"
        ),
    )
    variants_parser.add_argument("season_code")
    variants_parser.add_argument("--origin-start", type=int, default=2)
    variants_parser.add_argument("--origin-end", type=int, default=38)
    variants_parser.add_argument("--horizon", type=int, default=1)
    variants_parser.add_argument("--max-transfers-per-week", type=int, default=1)
    variants_parser.add_argument(
        "--skip-transfer-regret",
        action="store_true",
        help="Transfer regret solves a MILP per Gameweek and dominates runtime",
    )
    variants_parser.add_argument("--rules")
    variants_parser.add_argument("--output")

    package_parser = subparsers.add_parser(
        "export-team-news-research-package",
        help="Export a bounded decision-focused team-news package for a projection run",
    )
    package_parser.add_argument("season_code")
    package_parser.add_argument("--gameweek", type=int, required=True)
    package_parser.add_argument("--projection-run", type=int, required=True)
    package_parser.add_argument("--recommendation-run")
    package_parser.add_argument("--recommendation-json")
    package_parser.add_argument(
        "--research-mode", choices=("preseason", "provisional", "final"), required=True
    )
    package_parser.add_argument("--research-window-start", required=True)
    package_parser.add_argument("--research-timestamp")
    package_parser.add_argument("--alternatives", type=int, default=15)
    package_parser.add_argument("--output", required=True)

    result_parser = subparsers.add_parser(
        "import-team-news-research-result",
        help="Import strict schema-v3 team-news JSON into the human review queue",
    )
    result_parser.add_argument("--season-code", required=True)
    result_parser.add_argument("--gameweek", type=int, required=True)
    result_parser.add_argument("--input", required=True)

    modifier_parser = subparsers.add_parser(
        "review-team-news-modifier",
        help="Accept one evidence item with an explicit reviewed projection modifier",
    )
    modifier_parser.add_argument("--season-code", required=True)
    modifier_parser.add_argument("--gameweek", type=int, required=True)
    modifier_parser.add_argument("--evidence-id", type=int, required=True)
    modifier_parser.add_argument("--player-id", required=True)
    modifier_parser.add_argument("--modifier-type", required=True)
    modifier_parser.add_argument("--operation", required=True)
    modifier_parser.add_argument("--value", type=float, required=True)
    modifier_parser.add_argument("--start-gameweek", type=int, required=True)
    modifier_parser.add_argument("--end-gameweek", type=int, required=True)
    modifier_parser.add_argument("--rationale", required=True)
    modifier_parser.add_argument("--reviewed-by", default="user")
    modifier_parser.add_argument("--research-run-id")
    modifier_parser.add_argument("--input-package-id")

    apply_parser = subparsers.add_parser(
        "apply-team-news-research",
        help="Rerun a projection and decision using accepted reviewed modifiers",
    )
    apply_parser.add_argument("--baseline-projection-run", type=int, required=True)
    apply_parser.add_argument(
        "--decision-type",
        choices=("opening_squad", "transfers", "weekly_xi"),
        default="opening_squad",
    )
    apply_parser.add_argument("--rules")
    apply_parser.add_argument("--output")

    compare_research_parser = subparsers.add_parser(
        "compare-research-decision",
        help="Compare baseline and revised opening-squad decisions",
    )
    compare_research_parser.add_argument("--baseline-projection-run", type=int, required=True)
    compare_research_parser.add_argument("--revised-projection-run", type=int, required=True)
    compare_research_parser.add_argument(
        "--decision-type",
        choices=("opening_squad", "transfers", "weekly_xi"),
        default="opening_squad",
    )
    compare_research_parser.add_argument("--current-squad-json")
    compare_research_parser.add_argument("--rules")
    compare_research_parser.add_argument("--output")

    args = parser.parse_args()
    database_path = Path(args.database)
    database_path.parent.mkdir(parents=True, exist_ok=True)

    with HistoricalDatabase(database_path) as database:
        database.initialise()
        if args.command == "init":
            print(f"Initialised {database_path} at schema version {database.schema_version}")
            return

        if args.command == "summary":
            print(json.dumps(database.season_summary(args.season_code), indent=2))
            return

        if args.command == "export-team-news-research-package":
            recommendation = None
            if args.recommendation_json:
                recommendation = json.loads(
                    Path(args.recommendation_json).read_text(encoding="utf-8")
                )
            package = generate_team_news_research_package(
                database,
                season_code=args.season_code,
                gameweek_number=args.gameweek,
                projection_run_id=args.projection_run,
                recommendation_run_id=args.recommendation_run,
                recommendation=recommendation,
                research_mode=args.research_mode,
                research_window_start=args.research_window_start,
                research_timestamp=args.research_timestamp,
                alternatives_limit=args.alternatives,
                output_path=args.output,
            )
            print(
                json.dumps(
                    {
                        "input_package_id": package["input_package_id"],
                        "input_package_hash": package["input_package_hash"],
                        "output": str(args.output),
                        "selected_players": len(package["selected_squad"]),
                        "alternatives": len(package["alternatives"]),
                    },
                    indent=2,
                )
            )
            return

        if args.command == "import-team-news-research-result":
            from ..workflow import WeeklyWorkflowRepository

            payload = Path(args.input).read_text(encoding="utf-8")
            evidence_ids = ingest_structured_news(
                WeeklyWorkflowRepository(database),
                season_code=args.season_code,
                gameweek_number=args.gameweek,
                payload=payload,
            )
            print(
                json.dumps(
                    {
                        "schema_version": 3,
                        "evidence_ids": list(evidence_ids),
                        "evidence_count": len(evidence_ids),
                        "coverage_count": database.connection.execute(
                            "SELECT COUNT(*) FROM team_news_coverage "
                            "WHERE research_result_id = (SELECT MAX(id) "
                            "FROM team_news_research_runs)"
                        ).fetchone()[0],
                    },
                    indent=2,
                )
            )
            return

        if args.command == "review-team-news-modifier":
            from ..workflow import WeeklyWorkflowRepository

            modifier = ReviewedProjectionModifier(
                source_player_id=args.player_id,
                modifier_type=args.modifier_type,
                operation=args.operation,
                value=args.value,
                start_gameweek=args.start_gameweek,
                end_gameweek=args.end_gameweek,
                evidence_ids=(args.evidence_id,),
                rationale=args.rationale,
                reviewed_by=args.reviewed_by,
                reviewed_at=datetime.now(UTC),
                research_run_id=args.research_run_id,
                input_package_id=args.input_package_id,
            )
            WeeklyWorkflowRepository(database).review_evidence(
                args.evidence_id,
                status="accepted",
                rationale=args.rationale,
                modifier=modifier,
            )
            print(
                json.dumps(
                    {"evidence_id": args.evidence_id, "status": "accepted", "modifier": True}
                )
            )
            return

        if args.command == "apply-team-news-research":
            baseline = database.connection.execute(
                """
                SELECT seasons.code
                FROM projection_runs
                JOIN seasons ON seasons.id = projection_runs.season_id
                WHERE projection_runs.id = ?
                """,
                (args.baseline_projection_run,),
            ).fetchone()
            if baseline is None:
                raise ValueError("Baseline projection run is unavailable")
            rules = load_season_rules(Path(args.rules or f"config/seasons/{baseline['code']}.json"))
            rerun = generate_revised_projection(
                database,
                rules,
                baseline_projection_run_id=args.baseline_projection_run,
                decision_type=args.decision_type,
            )
            result = {
                "revised_projection_run_id": rerun.revised_projection_run_id,
                "baseline_projection_run_id": rerun.baseline_projection_run_id,
                "modifier_ids": list(rerun.modifier_ids),
            }
            if args.decision_type == "opening_squad":
                comparison = compare_opening_squad_decision(
                    database, rules,
                    baseline_projection_run_id=rerun.baseline_projection_run_id,
                    revised_projection_run_id=rerun.revised_projection_run_id,
                    modifier_ids=rerun.modifier_ids,
                )
                result["comparison"] = comparison.__dict__
            elif args.decision_type == "weekly_xi":
                comparison = compare_weekly_xi_decision(
                    database, rules,
                    baseline_projection_run_id=rerun.baseline_projection_run_id,
                    revised_projection_run_id=rerun.revised_projection_run_id,
                    modifier_ids=rerun.modifier_ids,
                )
                result["comparison"] = comparison.__dict__
            elif args.current_squad_json:
                current_payload = json.loads(
                    Path(args.current_squad_json).read_text(encoding="utf-8")
                )
                current = CurrentSquad(
                    player_ids=frozenset(str(value) for value in current_payload["player_ids"]),
                    selling_prices_tenths={
                        str(key): int(value)
                        for key, value in current_payload["selling_prices_tenths"].items()
                    },
                    bank_tenths=int(current_payload["bank_tenths"]),
                    free_transfers=int(current_payload["free_transfers"]),
                    available_chips=tuple(current_payload.get("available_chips", ())),
                )
                result["comparison"] = compare_transfer_decision(
                    database, rules,
                    baseline_projection_run_id=rerun.baseline_projection_run_id,
                    revised_projection_run_id=rerun.revised_projection_run_id,
                    current_squad=current,
                    modifier_ids=rerun.modifier_ids,
                ).__dict__
            if args.output:
                Path(args.output).write_text(
                    json.dumps(result, indent=2, default=str), encoding="utf-8"
                )
            print(json.dumps(result, indent=2, default=str))
            return

        if args.command == "compare-research-decision":
            baseline = database.connection.execute(
                """
                SELECT seasons.code
                FROM projection_runs
                JOIN seasons ON seasons.id = projection_runs.season_id
                WHERE projection_runs.id = ?
                """,
                (args.baseline_projection_run,),
            ).fetchone()
            if baseline is None:
                raise ValueError("Baseline projection run is unavailable")
            rules = load_season_rules(Path(args.rules or f"config/seasons/{baseline['code']}.json"))
            if args.decision_type == "opening_squad":
                comparison = compare_opening_squad_decision(
                    database, rules,
                    baseline_projection_run_id=args.baseline_projection_run,
                    revised_projection_run_id=args.revised_projection_run,
                )
            elif args.decision_type == "weekly_xi":
                comparison = compare_weekly_xi_decision(
                    database, rules,
                    baseline_projection_run_id=args.baseline_projection_run,
                    revised_projection_run_id=args.revised_projection_run,
                )
            else:
                if not args.current_squad_json:
                    raise ValueError("Transfer comparisons require --current-squad-json")
                current_payload = json.loads(
                    Path(args.current_squad_json).read_text(encoding="utf-8")
                )
                current = CurrentSquad(
                    player_ids=frozenset(str(value) for value in current_payload["player_ids"]),
                    selling_prices_tenths={
                        str(key): int(value)
                        for key, value in current_payload["selling_prices_tenths"].items()
                    },
                    bank_tenths=int(current_payload["bank_tenths"]),
                    free_transfers=int(current_payload["free_transfers"]),
                    available_chips=tuple(current_payload.get("available_chips", ())),
                )
                comparison = compare_transfer_decision(
                    database, rules,
                    baseline_projection_run_id=args.baseline_projection_run,
                    revised_projection_run_id=args.revised_projection_run,
                    current_squad=current,
                )
            result = comparison.__dict__
            if args.output:
                Path(args.output).write_text(
                    json.dumps(result, indent=2, default=str), encoding="utf-8"
                )
            print(json.dumps(result, indent=2, default=str))
            return

        if args.command == "backtest-report":
            print(
                json.dumps(
                    load_backtest_report(database, args.run_id).as_dict(),
                    indent=2,
                )
            )
            return

        if args.command == "compare-backtest-baselines":
            comparison = compare_backtest_to_baselines(
                database,
                args.run_id,
            )
            if args.output:
                comparison.write_json(args.output)
            print(json.dumps(comparison.as_dict(), indent=2))
            return

        if args.command == "evaluate-squad-regret":
            season = database.connection.execute(
                """
                SELECT seasons.code
                FROM projection_backtest_runs
                JOIN seasons ON seasons.id = projection_backtest_runs.season_id
                WHERE projection_backtest_runs.id = ?
                """,
                (args.run_id,),
            ).fetchone()
            if season is None:
                raise ValueError(f"Backtest run {args.run_id} is unavailable")
            rules = load_season_rules(Path(args.rules or f"config/seasons/{season['code']}.json"))
            regret = evaluate_legal_squad_regret(
                database,
                args.run_id,
                rules,
                methods=tuple(args.methods),
            )
            print(json.dumps(regret.as_dict(), indent=2))
            return

        if args.command == "preseason-readiness":
            rules = load_season_rules(
                Path(args.rules or f"config/seasons/{args.season_code}.json")
            )
            readiness = build_preseason_readiness_report(
                database,
                rules,
                season_code=args.season_code,
                gameweek_number=args.gameweek,
                horizon_gameweeks=args.horizon,
                candidate_pool_size=args.candidate_pool_size,
                minimum_mean_appearance=args.appearance_floor,
            )
            if args.output:
                write_json_report(readiness, args.output)
            print(json.dumps(readiness, indent=2))
            return

        if args.command == "evaluate-squad-policies":
            season = database.connection.execute(
                """
                SELECT seasons.code
                FROM projection_backtest_runs
                JOIN seasons ON seasons.id = projection_backtest_runs.season_id
                WHERE projection_backtest_runs.id = ?
                """,
                (args.run_id,),
            ).fetchone()
            if season is None:
                raise ValueError(f"Backtest run {args.run_id} is unavailable")
            rules = load_season_rules(
                Path(args.rules or f"config/seasons/{season['code']}.json")
            )
            policies = (
                SquadConstructionPolicy("unrestricted-single"),
                SquadConstructionPolicy(
                    "unrestricted-frontier",
                    candidate_pool_size=args.candidate_pool_size,
                ),
                *(
                    SquadConstructionPolicy(
                        f"appearance-{floor:.2f}-frontier",
                        minimum_mean_appearance=floor,
                        candidate_pool_size=args.candidate_pool_size,
                    )
                    for floor in args.appearance_floors
                ),
            )
            report = evaluate_squad_construction_policies(
                database,
                args.run_id,
                rules,
                policies,
                origin_gameweeks=(
                    None if args.origins is None else tuple(args.origins)
                ),
            )
            if args.output:
                write_json_report(report.as_dict(), args.output)
            print(json.dumps(report.as_dict(), indent=2))
            return

        if args.command == "compile-squad-policy-evaluation":
            season_rows = database.connection.execute(
                f"""
                SELECT DISTINCT seasons.code
                FROM projection_backtest_runs
                JOIN seasons ON seasons.id = projection_backtest_runs.season_id
                WHERE projection_backtest_runs.id IN (
                    {','.join('?' for _ in args.run_ids)}
                )
                """,
                tuple(args.run_ids),
            ).fetchall()
            rules_by_season = {
                str(row["code"]): load_season_rules(
                    Path(f"config/seasons/{row['code']}.json")
                )
                for row in season_rows
            }
            policies = (
                SquadConstructionPolicy("unrestricted-single"),
                SquadConstructionPolicy(
                    "unrestricted-frontier",
                    candidate_pool_size=args.candidate_pool_size,
                ),
                *(
                    SquadConstructionPolicy(
                        f"appearance-{floor:.2f}-frontier",
                        minimum_mean_appearance=floor,
                        candidate_pool_size=args.candidate_pool_size,
                    )
                    for floor in args.appearance_floors
                ),
            )
            report = compile_squad_policy_evaluations(
                database,
                tuple(args.run_ids),
                rules_by_season,
                policies,
                origin_gameweeks=(
                    None if args.origins is None else tuple(args.origins)
                ),
                bootstrap_samples=args.bootstrap_samples,
                random_seed=args.seed,
            )
            if args.output:
                write_json_report(report, args.output)
            print(json.dumps(report, indent=2))
            return

        if args.command == "fit-preseason-priors":
            rules_by_season = {
                season_code: load_season_rules(Path(f"config/seasons/{season_code}.json"))
                for season_code in args.target_seasons
            }
            fit = fit_preseason_priors(
                database,
                rules_by_season,
                target_seasons=tuple(args.target_seasons),
                origin_gameweek_start=args.origin_start,
                origin_gameweek_end=args.origin_end,
                horizon_gameweeks=args.horizon,
                design_size=args.design_size,
                confirmation_horizons=tuple(args.confirmation_horizons),
            )
            if args.config_output:
                Path(args.config_output).write_text(
                    json.dumps(asdict(fit.best_config), indent=2) + "\n",
                    encoding="utf-8",
                )
            if args.output:
                write_json_report(fit.as_dict(), args.output)
            print(json.dumps(fit.as_dict(), indent=2))
            return

        if args.command == "profile-preseason-prior":
            rules_by_season = {
                season_code: load_season_rules(Path(f"config/seasons/{season_code}.json"))
                for season_code in args.target_seasons
            }
            base_config = (
                PRESEASON_V5_MODEL_CONFIG
                if args.base_config is None
                else _load_config(args.base_config)
            )
            profile = profile_preseason_prior(
                database,
                rules_by_season,
                parameter=args.parameter,
                target_seasons=tuple(args.target_seasons),
                low=args.low,
                high=args.high,
                steps=args.steps,
                origin_gameweek_start=args.origin_start,
                origin_gameweek_end=args.origin_end,
                horizon_gameweeks=args.horizon,
                base_config=base_config,
            )
            if args.output:
                write_json_report(profile, args.output)
            print(json.dumps(profile, indent=2))
            return

        if args.command == "team-strength-report":
            rules = load_season_rules(
                Path(args.rules or f"config/seasons/{args.season_code}.json")
            )
            adjustments: tuple[ContextualAdjustment, ...] = ()
            if args.adjustments:
                adjustments = tuple(
                    ContextualAdjustment(**entry)
                    for entry in json.loads(
                        Path(args.adjustments).read_text(encoding="utf-8")
                    )
                )
            report = build_team_strength_report(
                database,
                rules,
                season_code=args.season_code,
                gameweek_number=args.gameweek,
                adjustments=adjustments,
            )
            if args.output:
                write_json_report(report, args.output)
            print(json.dumps(report, indent=2))
            return

        if args.command == "evaluate-team-strength":
            rules = load_season_rules(
                Path(args.rules or f"config/seasons/{args.season_code}.json")
            )
            evaluation = evaluate_team_strength_models(
                database,
                rules,
                season_code=args.season_code,
                origin_gameweek_start=args.origin_start,
                origin_gameweek_end=args.origin_end,
            )
            if args.output:
                write_json_report(evaluation, args.output)
            print(json.dumps(evaluation, indent=2))
            return

        if args.command == "validate-preseason-strength":
            rules = load_season_rules(
                Path(args.rules or f"config/seasons/{args.season_code}.json")
            )
            result = validate_preseason_strength(
                database,
                rules,
                season_code=args.season_code,
                horizon_gameweeks=args.horizon,
                gameweek_number=args.gameweek,
                candidate_pool_size=args.candidate_pool_size,
                minimum_mean_appearance=args.appearance_floor,
                include_reference_model=not args.skip_reference_model,
                include_robustness=not args.skip_robustness,
                generate_live_projection=not args.no_live_projection,
            )
            if args.output:
                write_json_report(result, args.output)
            if args.comparison_output:
                write_json_report(
                    squad_comparison_artifact(result), args.comparison_output
                )
            if args.markdown_output:
                write_preseason_validation_markdown(result, args.markdown_output)
            print(json.dumps(result, indent=2))
            return

        if args.command == "validate-opening-squad-search":
            rules = load_season_rules(
                Path(args.rules or f"config/seasons/{args.season_code}.json")
            )
            previous = load_final_squad(args.season_code)
            incumbent = frozenset(
                player["source_player_id"]
                for player in ((previous or {}).get("final_squad") or {}).get(
                    "players", []
                )
            )
            result = validate_opening_squad_search(
                database,
                rules,
                season_code=args.season_code,
                horizon_gameweeks=args.horizon,
                gameweek_number=args.gameweek,
                mixed_scale=args.mixed_scale,
                historical_scale=args.historical_scale,
                include_historical=not args.skip_historical,
                minimum_mean_appearance=args.appearance_floor,
                incumbent_winner=incumbent,
                # Bank the live comparison before the historical phase starts.
                checkpoint_path=(
                    args.output
                    or Path("data/models")
                    / f"opening-squad-search-validation-{args.season_code}.json"
                ),
            )
            base = Path("data/models")
            paths = write_search_validation_artifacts(
                result,
                json_path=(
                    args.output
                    or base
                    / f"opening-squad-search-validation-{args.season_code}.json"
                ),
                markdown_path=(
                    args.markdown_output
                    or base
                    / f"opening-squad-search-validation-{args.season_code}.md"
                ),
            )
            if args.quiet:
                acceptance = result["acceptance"]
                print(
                    f"Acceptance: {'PASS' if acceptance['passed'] else 'FAIL'}; "
                    f"live gain over the forty-candidate frontier "
                    f"{result['live']['exact_value_gained_over_frontier_40']}; "
                    f"converged {acceptance['live_search_converged']}."
                )
                for path in paths:
                    print(f"Wrote {path}")
            else:
                print(json.dumps(result, indent=2, default=str))
            return

        if args.command == "import-championship":
            document = load_championship_document(args.path)
            summary = import_championship_document(database, document)
            if args.roles_path:
                roles = load_role_document(args.roles_path)
                summary["roles"] = import_role_document(database, roles)
            print(json.dumps(summary, indent=2, default=str))
            return

        if args.command == "finalise-preseason-squad":
            rules = load_season_rules(
                Path(args.rules or f"config/seasons/{args.season_code}.json")
            )
            result = finalise_preseason_squad(
                database,
                rules,
                season_code=args.season_code,
                horizon_gameweeks=args.horizon,
                gameweek_number=args.gameweek,
                frontier_size=args.frontier_size,
                minimum_mean_appearance=args.appearance_floor,
                include_decision_evidence=args.decision_evidence,
                alternative_count=args.alternatives,
                apply_modifiers=not args.no_modifiers,
            )
            base = Path("data/models")
            paths = write_artifacts(
                result,
                validation_path=(
                    args.output
                    or base / f"preseason-final-validation-{args.season_code}.json"
                ),
                squad_path=(
                    args.squad_output
                    or base / f"preseason-final-squad-{args.season_code}.json"
                ),
                markdown_path=(
                    args.markdown_output
                    or base / f"preseason-final-validation-{args.season_code}.md"
                ),
            )
            if args.quiet:
                squad = result["final_squad"]
                print(
                    f"Projection run {squad.get('projection_run_id')} "
                    f"({squad.get('model_version')}); cost "
                    f"{squad['total_cost_tenths'] / 10:.1f}m, bank "
                    f"{squad['bank_tenths'] / 10:.1f}m, exact GW"
                    f"{args.gameweek}-{args.gameweek + args.horizon - 1} value "
                    f"{squad['decision_value']}."
                )
                for path in paths:
                    print(f"Wrote {path}")
            else:
                print(json.dumps(result, indent=2, default=str))
            return

        if args.command == "evaluate-allocation-variants":
            rules = load_season_rules(
                Path(args.rules or f"config/seasons/{args.season_code}.json")
            )
            variants = evaluate_allocation_variants(
                database,
                rules,
                season_code=args.season_code,
                origin_gameweek_start=args.origin_start,
                origin_gameweek_end=args.origin_end,
                horizon_gameweeks=args.horizon,
                max_transfers_per_week=args.max_transfers_per_week,
                include_transfer_regret=not args.skip_transfer_regret,
            )
            if args.output:
                write_json_report(variants, args.output)
            print(json.dumps(variants, indent=2))
            return

        if args.command == "compare-opening-squads":
            rules = load_season_rules(Path(args.rules or f"config/seasons/{args.season_code}.json"))
            comparison = compare_opening_squads(
                database,
                rules,
                {
                    args.first_label: _load_config(args.first_config),
                    args.second_label: _load_config(args.second_config),
                },
                season_code=args.season_code,
                gameweek=args.gameweek,
                horizon_gameweeks=args.horizon,
                sensitivity_parameters=tuple(args.sensitivity_parameters),
                sensitivity_fraction=args.sensitivity_fraction,
            )
            if args.output:
                write_json_report(comparison, args.output)
            print(json.dumps(comparison, indent=2))
            return

        if args.command == "evaluate-captain-regret":
            season = database.connection.execute(
                """
                SELECT seasons.code
                FROM projection_backtest_runs
                JOIN seasons ON seasons.id = projection_backtest_runs.season_id
                WHERE projection_backtest_runs.id = ?
                """,
                (args.run_id,),
            ).fetchone()
            if season is None:
                raise ValueError(f"Backtest run {args.run_id} is unavailable")
            rules = load_season_rules(Path(args.rules or f"config/seasons/{season['code']}.json"))
            captain = evaluate_owned_captain_regret(
                database,
                args.run_id,
                rules,
                method=args.method,
            )
            if args.output:
                write_json_report(captain.as_dict(), args.output)
            print(json.dumps(captain.as_dict(), indent=2))
            return

        if args.command == "evaluate-transfer-regret":
            season = database.connection.execute(
                """
                SELECT seasons.code
                FROM projection_backtest_runs
                JOIN seasons ON seasons.id = projection_backtest_runs.season_id
                WHERE projection_backtest_runs.id = ?
                """,
                (args.run_id,),
            ).fetchone()
            if season is None:
                raise ValueError(f"Backtest run {args.run_id} is unavailable")
            rules = load_season_rules(Path(args.rules or f"config/seasons/{season['code']}.json"))
            transfer = evaluate_transfer_regret(
                database,
                args.run_id,
                rules,
                first_gameweek=args.first_gameweek,
                last_gameweek=args.last_gameweek,
                max_transfers_per_week=args.max_transfers_per_week,
                candidate_pool_size=args.candidate_pool_size,
            )
            if args.output:
                write_json_report(transfer.as_dict(), args.output)
            print(json.dumps(transfer.as_dict(), indent=2))
            return

        if args.command == "evaluate-chip-regret":
            season = database.connection.execute(
                """
                SELECT seasons.code
                FROM projection_backtest_runs
                JOIN seasons ON seasons.id = projection_backtest_runs.season_id
                WHERE projection_backtest_runs.id = ?
                """,
                (args.run_id,),
            ).fetchone()
            if season is None:
                raise ValueError(f"Backtest run {args.run_id} is unavailable")
            rules = load_season_rules(Path(args.rules or f"config/seasons/{season['code']}.json"))
            if args.lookahead:
                policy = LookaheadChipPolicy(
                    enabled=True,
                    minimum_gain=args.lookahead_minimum_gain,
                    margin=args.lookahead_margin,
                )
            else:
                policy = ScoringChipPolicy(
                    bench_boost_threshold=(
                        float("inf")
                        if args.bench_boost_threshold is None
                        else args.bench_boost_threshold
                    ),
                    triple_captain_threshold=(
                        float("inf")
                        if args.triple_captain_threshold is None
                        else args.triple_captain_threshold
                    ),
                )
            chip_report = evaluate_chip_regret(
                database,
                args.run_id,
                rules,
                first_gameweek=args.first_gameweek,
                last_gameweek=args.last_gameweek,
                max_transfers_per_week=args.max_transfers_per_week,
                chip_policy=policy,
                candidate_pool_size=args.candidate_pool_size,
            )
            if args.output:
                write_json_report(chip_report.as_dict(), args.output)
            print(json.dumps(chip_report.as_dict(), indent=2))
            return

        if args.command == "replay-transfer-continuity":
            season = database.connection.execute(
                """
                SELECT seasons.code
                FROM projection_backtest_runs
                JOIN seasons ON seasons.id = projection_backtest_runs.season_id
                WHERE projection_backtest_runs.id = ?
                """,
                (args.run_id,),
            ).fetchone()
            if season is None:
                raise ValueError(f"Backtest run {args.run_id} is unavailable")
            rules = load_season_rules(Path(args.rules or f"config/seasons/{season['code']}.json"))
            continuity = replay_backtest_transfer_continuity(
                database,
                args.run_id,
                rules,
                first_gameweek=args.first_gameweek,
                last_gameweek=args.last_gameweek,
                max_transfers_per_week=args.max_transfers_per_week,
                candidate_pool_size=args.candidate_pool_size,
            )
            if args.output:
                write_json_report(continuity, args.output)
            print(json.dumps(continuity, indent=2))
            return

        if args.command == "compile-transfer-policy-evaluation":
            season_rows = database.connection.execute(
                f"""
                SELECT DISTINCT seasons.code
                FROM projection_backtest_runs
                JOIN seasons ON seasons.id = projection_backtest_runs.season_id
                WHERE projection_backtest_runs.id IN (
                    {','.join('?' for _ in args.run_ids)}
                )
                """,
                tuple(args.run_ids),
            ).fetchall()
            rules_by_season = {
                str(row["code"]): load_season_rules(
                    Path(f"config/seasons/{row['code']}.json")
                )
                for row in season_rows
            }
            evaluation = compile_transfer_policy_evaluation(
                database,
                tuple(args.run_ids),
                rules_by_season,
                first_gameweek=args.first_gameweek,
                last_gameweek=args.last_gameweek,
                max_transfers_per_week=args.max_transfers_per_week,
                candidate_pool_size=args.candidate_pool_size,
            )
            if args.output:
                write_json_report(evaluation, args.output)
            print(json.dumps(evaluation, indent=2))
            return

        if args.command == "compile-model-evaluation":
            suite = build_evaluation_suite(
                database,
                tuple(args.incumbent_runs),
                challenger_run_ids=tuple(args.challenger_runs),
            )
            write_json_report(suite, args.output)
            print(json.dumps(suite, indent=2))
            return

        if args.command == "stage-one-diagnostics":
            diagnostics = build_stage_one_diagnostics(
                database,
                tuple(args.run_ids),
                baseline_method=args.baseline,
                bootstrap_samples=args.bootstrap_samples,
                moving_block_gameweeks=args.moving_block_gameweeks,
                minimum_slice_samples=args.minimum_slice_samples,
                seed=args.seed,
            )
            write_stage_one_diagnostics(diagnostics, args.output)
            print(json.dumps(diagnostics, indent=2))
            return

        if args.command == "capture-gameweek-forecasts":
            rules = load_season_rules(Path(args.rules or f"config/seasons/{args.season_code}.json"))
            incumbent_config = (
                DEFAULT_MODEL_CONFIG
                if args.incumbent_config is None
                else _load_config(args.incumbent_config)
            )
            capture = capture_gameweek_forecasts(
                database,
                rules,
                season_code=args.season_code,
                gameweek=args.gameweek,
                horizon_gameweeks=args.horizon,
                incumbent_config=incumbent_config,
                incumbent_model_version=args.incumbent_model_version,
            )
            if args.output:
                write_json_report(capture, args.output)
            print(json.dumps(capture, indent=2))
            return

        if args.command == "prospective-capture-status":
            capture_status = build_prospective_capture_status(
                database,
                args.season_code,
            )
            if args.output:
                write_json_report(capture_status, args.output)
            print(json.dumps(capture_status, indent=2))
            return

        if args.command == "register-forward-candidate":
            model_config = json.loads(Path(args.model_config).read_text(encoding="utf-8"))
            gate_policy = (
                PromotionGatePolicy()
                if args.gate_policy is None
                else PromotionGatePolicy(
                    **json.loads(Path(args.gate_policy).read_text(encoding="utf-8"))
                )
            )
            registration = register_forward_candidate(
                database,
                candidate_key=args.candidate_key,
                season_code=args.season_code,
                model_version=args.model_version,
                model_config=model_config,
                gate_policy=gate_policy,
            )
            print(json.dumps(registration, indent=2))
            return

        if args.command == "evaluate-forward-candidate":
            evidence = DecisionGateEvidence(
                **json.loads(Path(args.decision_evidence).read_text(encoding="utf-8"))
            )
            qualification = evaluate_forward_candidate(
                database,
                candidate_key=args.candidate_key,
                incumbent_run_ids=tuple(args.incumbent_runs),
                challenger_run_ids=tuple(args.challenger_runs),
                decision_evidence=evidence,
                seed=args.seed,
                finalize_failure=args.finalize_failure,
            )
            if args.output:
                write_json_report(qualification, args.output)
            print(json.dumps(qualification, indent=2))
            return

        if args.command == "project-forward-candidate":
            registration = database.connection.execute(
                """
                SELECT registrations.*, seasons.code AS season_code
                FROM model_candidate_registrations registrations
                JOIN seasons ON seasons.id = registrations.season_id
                WHERE registrations.candidate_key = ?
                """,
                (args.candidate_key,),
            ).fetchone()
            if registration is None:
                raise ValueError(f"Candidate {args.candidate_key!r} is not registered")
            if registration["status"] != "declared":
                raise ValueError(f"Candidate is already {registration['status']}")
            deadline = database.connection.execute(
                """
                SELECT deadline_time FROM gameweeks
                WHERE season_id = ? AND number = ?
                """,
                (
                    int(registration["season_id"]),
                    args.start_gameweek,
                ),
            ).fetchone()
            if deadline is None or deadline["deadline_time"] is None:
                raise ValueError("Candidate projection requires a deadline")
            deadline_time = datetime.fromisoformat(
                str(deadline["deadline_time"]).replace("Z", "+00:00")
            )
            generated_at = datetime.now(UTC)
            if generated_at >= deadline_time:
                raise ValueError("Forward candidate runs must be generated before deadline")
            season_code = str(registration["season_code"])
            rules = load_season_rules(
                Path(
                    args.rules
                    or f"config/seasons/{season_code}.json"
                )
            )
            declared = ModelDeclaration.from_dict(
                json.loads(registration["model_config_json"])
            )
            result = RatesProjectionModel(
                database,
                rules,
                config=declared.model_config,
                model_version=str(registration["model_version"]),
                team_strength_settings=declared.team_strength_settings,
                team_strength_adjustments=declared.contextual_adjustments,
            ).project(
                season_code=season_code,
                start_gameweek=args.start_gameweek,
                horizon_gameweeks=args.horizon,
                generated_at=generated_at,
                observation_mode="latest_pre_deadline",
                use_availability=True,
                fixture_as_of=generated_at,
                persist=True,
            )
            print(
                json.dumps(
                    {
                        "candidate_key": args.candidate_key,
                        "projection_run_id": result.projection_run_id,
                        "model_version": result.model_version,
                        "generated_at": result.generated_at.isoformat(),
                        "start_gameweek": result.start_gameweek,
                        "horizon_gameweeks": result.horizon_gameweeks,
                        "projection_rows": len(result.projections),
                    },
                    indent=2,
                )
            )
            return

        if args.command == "backtest-forward-candidate":
            declaration = load_forward_candidate(database, args.candidate_key)
            season_code = declaration["season_code"]
            rules = load_season_rules(
                Path(args.rules or f"config/seasons/{season_code}.json")
            )
            incumbent_config = _load_config(args.incumbent_config)
            pair = run_forward_candidate_pair(
                database,
                rules,
                candidate_key=args.candidate_key,
                incumbent_config=incumbent_config,
                incumbent_model_version=args.incumbent_model_version,
                origin_gameweek_start=args.origin_start,
                origin_gameweek_end=args.origin_end,
                horizon_gameweeks=args.horizon,
            )
            if args.output:
                write_json_report(pair.as_dict(), args.output)
            print(json.dumps(pair.as_dict(), indent=2))
            return

        if args.command == "build-decision-evidence":
            season = database.connection.execute(
                """
                SELECT seasons.code
                FROM projection_backtest_runs
                JOIN seasons ON seasons.id = projection_backtest_runs.season_id
                WHERE projection_backtest_runs.id = ?
                """,
                (args.challenger_run,),
            ).fetchone()
            if season is None:
                raise ValueError(f"Backtest run {args.challenger_run} is unavailable")
            rules = load_season_rules(Path(args.rules or f"config/seasons/{season['code']}.json"))
            evidence = build_decision_gate_evidence(
                database,
                rules,
                incumbent_run_id=args.incumbent_run,
                challenger_run_id=args.challenger_run,
                method=args.method,
                max_transfers_per_week=args.max_transfers_per_week,
            )
            if args.output:
                write_json_report(asdict(evidence), args.output)
            print(json.dumps(asdict(evidence), indent=2))
            return

        if args.command == "backtest-projections":
            rules_path = Path(args.rules or f"config/seasons/{args.season_code}.json")
            rules = load_season_rules(rules_path)
            if rules.season != args.season_code:
                raise ValueError(
                    f"Rules season {rules.season!r} does not match "
                    f"backtest season {args.season_code!r}"
                )
            base = (
                DEFAULT_MODEL_CONFIG
                if args.model_config is None
                else ProjectionModelConfig(
                    **json.loads(Path(args.model_config).read_text(encoding="utf-8"))
                )
            )
            overrides = {
                "player_rate_prior_minutes": args.player_prior_minutes,
                "minutes_prior_matches": args.minutes_prior_matches,
                "team_prior_matches": args.team_prior_matches,
                "home_attack_multiplier": args.home_attack_multiplier,
                "away_attack_multiplier": args.away_attack_multiplier,
                "minutes_model": args.minutes_model,
                "playing_time_artifact": args.playing_time_artifact,
                "recent_gameweeks": args.recent_gameweeks,
                "recent_evidence_weight": args.recent_evidence_weight,
                "scoring_event_source": args.scoring_event_source,
                "defensive_contribution_model": args.defensive_contribution_model,
                "appearance_prior_matches": args.appearance_prior_matches,
                "appearance_prior_probability": args.appearance_prior_probability,
                "conditional_minutes_prior_appearances": (
                    args.conditional_minutes_prior_appearances
                ),
            }
            if args.no_team_minute_constraint:
                overrides["enforce_team_minutes"] = False
            config = replace(
                base,
                **{name: value for name, value in overrides.items() if value is not None},
            )
            report = ProjectionBacktester(
                database,
                rules,
                config=config,
                model_version=args.model_version,
            ).run(
                season_code=args.season_code,
                origin_gameweek_start=args.origin_start,
                origin_gameweek_end=args.origin_end,
                horizon_gameweeks=args.horizon,
                evidence_policy=args.evidence_policy,
            )
            print(json.dumps(report.as_dict(), indent=2))
            return

        if args.command == "tune-projections":
            rules_path = Path(args.rules or f"config/seasons/{args.season_code}.json")
            rules = load_season_rules(rules_path)
            if rules.season != args.season_code:
                raise ValueError(
                    f"Rules season {rules.season!r} does not match "
                    f"tuning season {args.season_code!r}"
                )
            Path("data").mkdir(parents=True, exist_ok=True)
            result = tune_projection_model(
                database,
                rules,
                season_code=args.season_code,
                development_start=args.development_start,
                development_end=args.development_end,
                validation_start=args.validation_start,
                validation_end=args.validation_end,
                horizon_gameweeks=args.horizon,
                trials=args.trials,
                study_name=args.study_name,
                storage_url=args.study_storage,
                seed=args.seed,
            )
            print(json.dumps(result.as_dict(), indent=2))
            return

        if args.command == "tune-projections-rolling":
            requested_seasons = (
                *args.development_seasons,
                args.validation_season,
            )
            rules_directory = Path(args.rules_directory)
            rules_by_season = {
                season_code: load_season_rules(rules_directory / f"{season_code}.json")
                for season_code in requested_seasons
            }
            for season_code, rules in rules_by_season.items():
                if rules.season != season_code:
                    raise ValueError(
                        f"Rules season {rules.season!r} does not match "
                        f"requested season {season_code!r}"
                    )
            result = tune_projection_model_rolling(
                database,
                rules_by_season,
                development_seasons=tuple(args.development_seasons),
                validation_season=args.validation_season,
                origin_gameweek_start=args.origin_start,
                origin_gameweek_end=args.origin_end,
                horizon_gameweeks=args.horizon,
                trials=args.trials,
                study_name=args.study_name,
                storage_url=args.study_storage,
                seed=args.seed,
            )
            print(json.dumps(result.as_dict(), indent=2))
            return

        if args.command == "train-boosted-challenger":
            result = train_and_evaluate_learned_challenger(
                database,
                training_run_ids=tuple(args.training_run_ids),
                validation_run_id=args.validation_run_id,
                artifact_path=args.artifact,
                seed=args.seed,
                loss=args.loss,
            )
            print(json.dumps(result.as_dict(), indent=2))
            return

        if args.command == "train-playing-time-hurdle":
            result = train_and_evaluate_hurdle_model(
                database,
                training_seasons=tuple(args.training_seasons),
                validation_season=args.validation_season,
                artifact_path=args.artifact,
                family=args.family,
                seed=args.seed,
            )
            print(json.dumps(result.as_dict(), indent=2))
            return

        if args.command == "audit-projection-assumptions":
            rules_directory = Path(args.rules_directory)
            rules_by_season = {
                season_code: load_season_rules(rules_directory / f"{season_code}.json")
                for season_code in args.development_seasons
            }
            result = run_assumption_audit(
                database,
                rules_by_season,
                development_seasons=tuple(args.development_seasons),
                origin_gameweek_start=args.origin_start,
                origin_gameweek_end=args.origin_end,
                horizon_gameweeks=args.horizon,
                output_path=args.output,
                artifact_directory=args.artifact_directory,
                seed=args.seed,
            )
            print(json.dumps(result.as_dict(), indent=2))
            return

        if args.command == "import-vaastav":
            adapter = VaastavAdapter(VaastavClient(base_url=args.source_base_url))
            for season_code in args.seasons:
                result = adapter.load_season(
                    source_ref=args.source_ref,
                    season_code=season_code,
                )
                source = IngestionSource(
                    name="vaastav-fpl-dataset",
                    url=(
                        "https://github.com/vaastav/"
                        "Fantasy-Premier-League/tree/"
                        f"{args.source_ref}/data/{season_code}"
                    ),
                    retrieved_at=datetime.now(UTC),
                    content_sha256=result.content_sha256,
                    identifier_namespace="official-fpl",
                    source_revision=args.source_ref,
                    adapter_version="vaastav-v1",
                )
                run_id = database.ingest_bundle(source, result.bundle)
                print(
                    f"Completed Vaastav ingestion run {run_id} "
                    f"for {season_code} from {len(result.source_files)} files"
                )
                print(
                    json.dumps(
                        {
                            "teams": result.quality.teams,
                            "players": result.quality.players,
                            "gameweeks": result.quality.gameweeks,
                            "fixtures": result.quality.fixtures,
                            "fixture_stats": result.quality.fixture_stats,
                            "gameweek_observations": (result.quality.gameweek_observations),
                            "skipped_rescheduled_rows": (result.quality.skipped_rescheduled_rows),
                            "skipped_non_player_rows": (result.quality.skipped_non_player_rows),
                            "players_without_gameweek_rows": (
                                result.quality.players_without_gameweek_rows
                            ),
                        },
                        indent=2,
                    )
                )
            return

        directory = Path(args.directory)
        source = IngestionSource(
            name=args.source_name,
            url=args.source_url,
            retrieved_at=datetime.now(UTC),
            content_sha256=_directory_digest(directory),
            identifier_namespace=args.identifier_namespace,
            source_revision=args.source_revision,
            adapter_version=args.adapter_version,
        )
        season = SeasonRecord(
            code=args.season_code,
            name=args.season_name,
            starts_on=args.starts_on,
            ends_on=args.ends_on,
        )
        run_id = database.ingest_bundle(source, load_csv_bundle(directory, season))
        print(f"Completed ingestion run {run_id}")


def _directory_digest(directory: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(directory.glob("*.csv")):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


if __name__ == "__main__":
    main()
