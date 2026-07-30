"""Command-line interface for the historical database."""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from ..assumption_audit import run_assumption_audit
from ..backtest import ProjectionBacktester, load_backtest_report
from ..config import load_season_rules
from ..evaluation import (
    build_evaluation_suite,
    compare_backtest_to_baselines,
    evaluate_legal_squad_regret,
    write_json_report,
)
from ..learned_challenger import train_and_evaluate_learned_challenger
from ..projections import DEFAULT_MODEL_CONFIG, MODEL_VERSION
from ..tuning import tune_projection_model, tune_projection_model_rolling
from .csv_bundle import load_csv_bundle
from .database import HistoricalDatabase
from .records import IngestionSource, SeasonRecord
from .vaastav import VaastavAdapter, VaastavClient


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage historical FPL data")
    parser.add_argument("--database", default="data/fpl_history.sqlite3")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="Create or upgrade the database schema")

    import_parser = subparsers.add_parser(
        "import-csv", help="Import a normalised CSV bundle"
    )
    import_parser.add_argument("directory")
    import_parser.add_argument("--season-code", required=True)
    import_parser.add_argument("--season-name", required=True)
    import_parser.add_argument("--source-name", required=True)
    import_parser.add_argument(
        "--identifier-namespace", default="official-fpl"
    )
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
        default=(
            "https://raw.githubusercontent.com/"
            "vaastav/Fantasy-Premier-League"
        ),
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
        "--player-prior-minutes",
        type=float,
        default=DEFAULT_MODEL_CONFIG.player_rate_prior_minutes,
    )
    backtest_parser.add_argument(
        "--minutes-prior-matches",
        type=float,
        default=DEFAULT_MODEL_CONFIG.minutes_prior_matches,
    )
    backtest_parser.add_argument(
        "--team-prior-matches",
        type=float,
        default=DEFAULT_MODEL_CONFIG.team_prior_matches,
    )
    backtest_parser.add_argument(
        "--home-attack-multiplier",
        type=float,
        default=DEFAULT_MODEL_CONFIG.home_attack_multiplier,
    )
    backtest_parser.add_argument(
        "--away-attack-multiplier",
        type=float,
        default=DEFAULT_MODEL_CONFIG.away_attack_multiplier,
    )
    backtest_parser.add_argument(
        "--minutes-model",
        choices=("legacy", "two_stage"),
        default="two_stage",
    )
    backtest_parser.add_argument(
        "--recent-gameweeks",
        type=int,
        default=DEFAULT_MODEL_CONFIG.recent_gameweeks,
    )
    backtest_parser.add_argument(
        "--recent-evidence-weight",
        type=float,
        default=DEFAULT_MODEL_CONFIG.recent_evidence_weight,
    )
    backtest_parser.add_argument(
        "--scoring-event-source",
        choices=("actual", "expected_with_actual_fallback"),
        default=DEFAULT_MODEL_CONFIG.scoring_event_source,
    )
    backtest_parser.add_argument(
        "--appearance-prior-matches",
        type=float,
        default=DEFAULT_MODEL_CONFIG.appearance_prior_matches,
    )
    backtest_parser.add_argument(
        "--appearance-prior-probability",
        type=float,
        default=DEFAULT_MODEL_CONFIG.appearance_prior_probability,
    )
    backtest_parser.add_argument(
        "--conditional-minutes-prior-appearances",
        type=float,
        default=DEFAULT_MODEL_CONFIG.conditional_minutes_prior_appearances,
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
    tune_parser.add_argument(
        "--study-name", default="fpl-rates-two-stage-v2"
    )
    tune_parser.add_argument(
        "--study-storage",
        default="sqlite:///data/fpl_tuning.sqlite3",
    )
    tune_parser.add_argument("--seed", type=int, default=20260729)

    rolling_parser = subparsers.add_parser(
        "tune-projections-rolling",
        help=(
            "Tune across development seasons and evaluate one locked "
            "validation season"
        ),
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

    challenger_parser = subparsers.add_parser(
        "train-boosted-challenger",
        help=(
            "Train on earlier completed backtests and evaluate one later "
            "validation run"
        ),
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

    audit_parser = subparsers.add_parser(
        "audit-projection-assumptions",
        help=(
            "Compare predeclared football assumptions on development "
            "seasons only"
        ),
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
            rules = load_season_rules(
                Path(
                    args.rules
                    or f"config/seasons/{season['code']}.json"
                )
            )
            regret = evaluate_legal_squad_regret(
                database,
                args.run_id,
                rules,
                methods=tuple(args.methods),
            )
            print(json.dumps(regret.as_dict(), indent=2))
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

        if args.command == "backtest-projections":
            rules_path = Path(
                args.rules or f"config/seasons/{args.season_code}.json"
            )
            rules = load_season_rules(rules_path)
            if rules.season != args.season_code:
                raise ValueError(
                    f"Rules season {rules.season!r} does not match "
                    f"backtest season {args.season_code!r}"
                )
            config = replace(
                DEFAULT_MODEL_CONFIG,
                player_rate_prior_minutes=args.player_prior_minutes,
                minutes_prior_matches=args.minutes_prior_matches,
                team_prior_matches=args.team_prior_matches,
                home_attack_multiplier=args.home_attack_multiplier,
                away_attack_multiplier=args.away_attack_multiplier,
                minutes_model=args.minutes_model,
                recent_gameweeks=args.recent_gameweeks,
                recent_evidence_weight=args.recent_evidence_weight,
                scoring_event_source=args.scoring_event_source,
                appearance_prior_matches=args.appearance_prior_matches,
                appearance_prior_probability=(
                    args.appearance_prior_probability
                ),
                conditional_minutes_prior_appearances=(
                    args.conditional_minutes_prior_appearances
                ),
                enforce_team_minutes=(
                    not args.no_team_minute_constraint
                ),
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
            rules_path = Path(
                args.rules or f"config/seasons/{args.season_code}.json"
            )
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
                season_code: load_season_rules(
                    rules_directory / f"{season_code}.json"
                )
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

        if args.command == "audit-projection-assumptions":
            rules_directory = Path(args.rules_directory)
            rules_by_season = {
                season_code: load_season_rules(
                    rules_directory / f"{season_code}.json"
                )
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
            adapter = VaastavAdapter(
                VaastavClient(base_url=args.source_base_url)
            )
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
                            "gameweek_observations": (
                                result.quality.gameweek_observations
                            ),
                            "skipped_rescheduled_rows": (
                                result.quality.skipped_rescheduled_rows
                            ),
                            "skipped_non_player_rows": (
                                result.quality.skipped_non_player_rows
                            ),
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
