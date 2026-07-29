"""Command-line interface for the historical database."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from ..backtest import ProjectionBacktester, load_backtest_report
from ..config import load_season_rules
from ..projections import MODEL_VERSION, ProjectionModelConfig
from ..tuning import tune_projection_model
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
        "--player-prior-minutes", type=float, default=900.0
    )
    backtest_parser.add_argument(
        "--minutes-prior-matches", type=float, default=6.0
    )
    backtest_parser.add_argument(
        "--team-prior-matches", type=float, default=6.0
    )
    backtest_parser.add_argument(
        "--home-attack-multiplier", type=float, default=1.08
    )
    backtest_parser.add_argument(
        "--away-attack-multiplier", type=float, default=0.92
    )
    backtest_parser.add_argument(
        "--minutes-model",
        choices=("legacy", "two_stage"),
        default="two_stage",
    )
    backtest_parser.add_argument("--recent-gameweeks", type=int, default=3)
    backtest_parser.add_argument(
        "--recent-evidence-weight", type=float, default=4.0
    )
    backtest_parser.add_argument(
        "--appearance-prior-matches", type=float, default=1.0
    )
    backtest_parser.add_argument(
        "--appearance-prior-probability", type=float, default=0.40
    )
    backtest_parser.add_argument(
        "--conditional-minutes-prior-appearances",
        type=float,
        default=2.0,
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

    report_parser = subparsers.add_parser(
        "backtest-report", help="Show a completed persisted backtest scorecard"
    )
    report_parser.add_argument("run_id", type=int)

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
            config = ProjectionModelConfig(
                player_rate_prior_minutes=args.player_prior_minutes,
                minutes_prior_matches=args.minutes_prior_matches,
                team_prior_matches=args.team_prior_matches,
                home_attack_multiplier=args.home_attack_multiplier,
                away_attack_multiplier=args.away_attack_multiplier,
                minutes_model=args.minutes_model,
                recent_gameweeks=args.recent_gameweeks,
                recent_evidence_weight=args.recent_evidence_weight,
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
