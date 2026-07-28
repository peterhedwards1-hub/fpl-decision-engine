"""Command-line interface for the historical database."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from .csv_bundle import load_csv_bundle
from .database import HistoricalDatabase
from .records import IngestionSource, SeasonRecord


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

    summary_parser = subparsers.add_parser("summary", help="Show season row counts")
    summary_parser.add_argument("season_code")

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
