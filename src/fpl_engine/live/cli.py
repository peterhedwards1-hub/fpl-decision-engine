"""Command-line entry point for collecting an official FPL snapshot."""

from __future__ import annotations

import argparse
import webbrowser
from pathlib import Path

from fpl_engine.history.database import HistoricalDatabase

from .collector import LiveSnapshotCollector
from .mirror import (
    DEFAULT_MIRROR_BASE_URL,
    MIRROR_PROVENANCE,
    MirrorSnapshotClient,
    official_api_reachable,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect the current official FPL snapshot")
    parser.add_argument("--database", type=Path, default=Path("data/fpl.sqlite3"))
    parser.add_argument("--archive-root", type=Path, default=Path("data/raw/fpl"))
    parser.add_argument("--report-root", type=Path, default=Path("data/reports/fpl"))
    parser.add_argument("--season-code", required=True, help="For example 2026-27")
    parser.add_argument("--season-name", help="Human-readable name; defaults to season code")
    parser.add_argument(
        "--rebuild-from-archives",
        action="store_true",
        help="Replay immutable archives before fetching the new snapshot",
    )
    parser.add_argument(
        "--open-report",
        action="store_true",
        help="Open the latest verification report in the default browser",
    )
    parser.add_argument(
        "--require-pre-deadline",
        action="store_true",
        help=(
            "Fail before archiving or ingestion unless the capture precedes "
            "the next deadline"
        ),
    )
    parser.add_argument(
        "--mirror-source-ref",
        help=(
            "Collect from the pinned public mirror at this immutable commit "
            "SHA instead of the official API. Use only when the official host "
            "is unreachable; the capture is recorded under the mirror's own "
            "ingestion source name, never the official one."
        ),
    )
    parser.add_argument("--mirror-base-url", default=DEFAULT_MIRROR_BASE_URL)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.database.parent.mkdir(parents=True, exist_ok=True)
    client = None
    provenance = None
    if args.mirror_source_ref:
        reachable, reason = official_api_reachable()
        if reachable:
            raise SystemExit(
                "The official FPL API is reachable; collect from it rather "
                "than from a mirror."
            )
        print(f"Official FPL API unreachable ({reason}); using the pinned mirror.")
        client = MirrorSnapshotClient(
            season_code=args.season_code,
            source_ref=args.mirror_source_ref,
            base_url=args.mirror_base_url,
        )
        provenance = MIRROR_PROVENANCE
    with HistoricalDatabase(args.database) as database:
        database.initialise()
        collector = LiveSnapshotCollector(
            database,
            archive_root=args.archive_root,
            report_root=args.report_root,
            **({"client": client, "provenance": provenance} if client else {}),
        )
        if args.rebuild_from_archives:
            replayed = collector.replay_archives(
                season_code=args.season_code,
                season_name=args.season_name,
            )
            print(f"Replayed {len(replayed)} archived captures.")
        result = collector.collect(
            season_code=args.season_code,
            season_name=args.season_name,
            require_pre_deadline=args.require_pre_deadline,
        )
    print(
        f"Collected {result.players} players, {result.teams} teams and "
        f"{result.fixtures} fixtures for GW{result.gameweek_number}."
    )
    print(f"Ingestion run: {result.ingestion_run_id}")
    print(
        f"Observation: {result.observation_kind} "
        f"(deadline {result.deadline_time})"
    )
    print(f"Raw archive: {result.archive_directory}")
    print(f"Verification report: {result.latest_report_index.resolve()}")
    print(f"Excel exports: {result.report_directory.resolve()}")
    if client is not None:
        print("Mirror notes:")
        for key, value in client.notes.as_dict().items():
            print(f"  {key}: {value}")
    if args.open_report:
        webbrowser.open(result.latest_report_index.resolve().as_uri())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
