"""Human-readable verification exports for a collected FPL snapshot."""

from __future__ import annotations

import csv
import html
import io
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fpl_engine.history.database import HistoricalDatabase, _observation_mode_filter

STATUS_LABELS = {
    "a": "Available",
    "d": "Doubtful",
    "i": "Injured",
    "n": "Unavailable",
    "s": "Suspended",
    "u": "Unavailable",
}


@dataclass(frozen=True)
class VerificationCheck:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class VerificationReport:
    directory: Path
    index_path: Path
    players_csv_path: Path
    fixtures_csv_path: Path
    latest_index_path: Path


def write_verification_report(
    database: HistoricalDatabase,
    *,
    report_root: str | Path,
    season_code: str,
    gameweek_number: int,
    captured_at: datetime,
    ingestion_run_id: int,
    archive_directory: Path,
    observation_mode: str = "latest_available",
) -> VerificationReport:
    """Export the normalised database rows as CSV and a browser report."""

    players = _players(database, season_code, gameweek_number, observation_mode)
    fixtures = _fixtures(database, season_code)
    summary = database.season_summary(season_code)
    gameweek = _one(
        database,
        """
        SELECT gameweeks.deadline_time
        FROM gameweeks
        JOIN seasons ON seasons.id = gameweeks.season_id
        WHERE seasons.code = ? AND gameweeks.number = ?
        """,
        (season_code, gameweek_number),
        "Gameweek",
    )
    ingestion = _one(
        database,
        """
        SELECT id, source_name, source_url, retrieved_at, content_sha256,
               identifier_namespace, source_revision, adapter_version,
               status, row_count
        FROM ingestion_runs WHERE id = ?
        """,
        (ingestion_run_id,),
        "Ingestion run",
    )
    checks = _checks(
        database,
        players,
        fixtures,
        summary,
        ingestion,
        gameweek,
        captured_at,
        observation_mode,
    )

    stamp = captured_at.astimezone(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    season_root = Path(report_root) / season_code
    directory = season_root / stamp
    latest = season_root / "latest"
    directory.mkdir(parents=True, exist_ok=True)
    latest.mkdir(parents=True, exist_ok=True)

    files = {
        "players.csv": _players_csv(players),
        "fixtures.csv": _fixtures_csv(fixtures),
        "index.html": _html_report(
            season_code=season_code,
            gameweek_number=gameweek_number,
            captured_at=captured_at,
            archive_directory=archive_directory,
            summary=summary,
            gameweek=gameweek,
            ingestion=ingestion,
            checks=checks,
            players=players,
            fixtures=fixtures,
        ).encode(),
    }
    for target in (directory, latest):
        for name, content in files.items():
            _atomic_write(target / name, content)

    return VerificationReport(
        directory=directory,
        index_path=directory / "index.html",
        players_csv_path=directory / "players.csv",
        fixtures_csv_path=directory / "fixtures.csv",
        latest_index_path=latest / "index.html",
    )


def _players(
    database: HistoricalDatabase,
    season_code: str,
    gameweek_number: int,
    observation_mode: str = "latest_available",
) -> list[dict[str, Any]]:
    observation_filter = _observation_mode_filter(observation_mode)
    rows = database.connection.execute(
        f"""
        WITH ranked_observations AS (
            SELECT observations.*,
                   ROW_NUMBER() OVER (
                       PARTITION BY observations.player_season_id,
                                    observations.gameweek_id
                       ORDER BY CASE observations.timing_quality
                                    WHEN 'exact' THEN 0
                                    WHEN 'date_only' THEN 1
                                    ELSE 2
                                END,
                                observations.observed_at DESC,
                                observations.observed_on DESC,
                                ingestion_runs.retrieved_at DESC,
                                observations.id DESC
                   ) AS observation_rank
            FROM player_gameweek_observations observations
            JOIN ingestion_runs ON ingestion_runs.id = observations.provenance_run_id
            JOIN gameweeks ON gameweeks.id = observations.gameweek_id
            JOIN seasons ON seasons.id = gameweeks.season_id
            WHERE seasons.code = ? AND gameweeks.number = ?
              AND {observation_filter}
        ),
        ranked_season_stats AS (
            SELECT season_stats.*,
                   ROW_NUMBER() OVER (
                       PARTITION BY season_stats.player_season_id
                       ORDER BY season_stats.observed_at DESC,
                                season_stats.id DESC
                   ) AS stats_rank
            FROM player_season_stats_observations season_stats
            JOIN player_seasons stats_ps
              ON stats_ps.id = season_stats.player_season_id
            JOIN seasons stats_seasons ON stats_seasons.id = stats_ps.season_id
            WHERE stats_seasons.code = ?
        )
        SELECT ps.source_player_id AS player_id, players.web_name,
               players.first_name, players.second_name,
               COALESCE(snapshot_teams.name, teams.name) AS team,
               COALESCE(snapshot_teams.short_name, teams.short_name) AS team_short_name,
               ps.position,
               snapshots.price_tenths, snapshots.selected_count,
               snapshots.selected_by_percent, snapshots.transfers_in,
               snapshots.transfers_out, snapshots.status,
               snapshots.chance_of_playing_next_round, snapshots.news,
               snapshots.observed_at AS captured_at, snapshots.observed_on,
               snapshots.timing_quality, snapshots.observation_kind,
               season_stats.minutes, season_stats.starts,
               season_stats.goals, season_stats.assists,
               season_stats.clean_sheets, season_stats.bonus,
               season_stats.bps, season_stats.defensive_contributions,
               season_stats.expected_goals, season_stats.expected_assists,
               season_stats.expected_goal_involvements,
               season_stats.total_points
        FROM ranked_observations snapshots
        JOIN player_seasons ps ON ps.id = snapshots.player_season_id
        JOIN seasons ON seasons.id = ps.season_id
        JOIN players ON players.id = ps.player_id
        JOIN teams ON teams.id = ps.team_id
        LEFT JOIN teams snapshot_teams ON snapshot_teams.id = snapshots.team_id
        LEFT JOIN ranked_season_stats season_stats
          ON season_stats.player_season_id = ps.id
         AND season_stats.stats_rank = 1
        JOIN gameweeks ON gameweeks.id = snapshots.gameweek_id
        WHERE snapshots.observation_rank = 1
          AND seasons.code = ? AND gameweeks.number = ?
          AND ps.identifier_namespace = 'official-fpl'
        ORDER BY players.web_name COLLATE NOCASE, ps.source_player_id
        """,
        (
            season_code,
            gameweek_number,
            season_code,
            season_code,
            gameweek_number,
        ),
    ).fetchall()
    return [dict(row) for row in rows]


def _fixtures(database: HistoricalDatabase, season_code: str) -> list[dict[str, Any]]:
    rows = database.connection.execute(
        """
        SELECT fixtures.source_fixture_id AS fixture_id,
               gameweeks.number AS gameweek, fixtures.kickoff_time,
               home.name AS home_team, away.name AS away_team,
               fixtures.home_score, fixtures.away_score, fixtures.finished
        FROM fixtures
        JOIN seasons ON seasons.id = fixtures.season_id
        JOIN teams home ON home.id = fixtures.home_team_id
        JOIN teams away ON away.id = fixtures.away_team_id
        LEFT JOIN gameweeks ON gameweeks.id = fixtures.gameweek_id
        WHERE seasons.code = ? AND fixtures.identifier_namespace = 'official-fpl'
        ORDER BY COALESCE(gameweeks.number, 999), fixtures.kickoff_time,
                 fixtures.source_fixture_id
        """,
        (season_code,),
    ).fetchall()
    return [dict(row) for row in rows]


def _one(
    database: HistoricalDatabase,
    query: str,
    values: tuple[object, ...],
    entity: str,
) -> dict[str, Any]:
    row = database.connection.execute(query, values).fetchone()
    if row is None:
        raise ValueError(f"{entity} was not found")
    return dict(row)


def _checks(
    database: HistoricalDatabase,
    players: list[dict[str, Any]],
    fixtures: list[dict[str, Any]],
    summary: dict[str, int],
    ingestion: dict[str, Any],
    gameweek: dict[str, Any],
    captured_at: datetime,
    observation_mode: str,
) -> list[VerificationCheck]:
    player_ids = [row["player_id"] for row in players]
    fixture_ids = [row["fixture_id"] for row in fixtures]
    current_observations = database.connection.execute(
        """
        SELECT
            (SELECT COUNT(*) FROM player_gameweek_observations
             WHERE provenance_run_id = ?)
          + (SELECT COUNT(*) FROM player_season_stats_observations
             WHERE provenance_run_id = ?)
          + (SELECT COUNT(*) FROM player_fixture_stats
             WHERE provenance_run_id = ?)
        """,
        (ingestion["id"], ingestion["id"], ingestion["id"]),
    ).fetchone()[0]
    expected_rows = (
        summary["teams"]
        + (summary["players"] * 2)
        + summary["gameweeks"]
        + summary["fixtures"]
        + current_observations
    )
    foreign_keys = database.connection.execute("PRAGMA foreign_key_check").fetchall()
    fixture_history_count = database.connection.execute(
        """
        SELECT COUNT(*)
        FROM fixture_observations
        WHERE provenance_run_id = ?
        """,
        (ingestion["id"],),
    ).fetchone()[0]
    deadline_value = gameweek["deadline_time"]
    timing_passed = False
    timing_detail = "Gameweek deadline is missing"
    if deadline_value:
        try:
            deadline = datetime.fromisoformat(str(deadline_value).replace("Z", "+00:00"))
            captured_utc = captured_at.astimezone(UTC)
            deadline_utc = deadline.astimezone(UTC)
            if observation_mode == "latest_pre_deadline":
                timing_passed = captured_utc < deadline_utc
                timing_detail = (
                    f"Captured {deadline_utc - captured_utc} before deadline"
                )
            elif observation_mode == "latest_post_gameweek":
                timing_passed = captured_utc >= deadline_utc
                timing_detail = (
                    f"Captured {captured_utc - deadline_utc} after deadline"
                )
            else:
                timing_passed = True
                timing_detail = f"Observation mode {observation_mode!r} is not time-specific"
        except (TypeError, ValueError):
            timing_detail = f"Invalid deadline {deadline_value!r}"
    return [
        VerificationCheck(
            "Ingestion completed",
            ingestion["status"] == "completed",
            f"Run {ingestion['id']} status: {ingestion['status']}",
        ),
        VerificationCheck(
            "Player snapshot is complete",
            len(players) == summary["players"],
            f"{len(players)} snapshots for {summary['players']} players",
        ),
        VerificationCheck(
            "Player IDs are unique",
            len(player_ids) == len(set(player_ids)),
            f"{len(set(player_ids))} unique IDs",
        ),
        VerificationCheck(
            "Fixture export is complete",
            len(fixtures) == summary["fixtures"],
            f"{len(fixtures)} exports for {summary['fixtures']} fixtures",
        ),
        VerificationCheck(
            "Fixture IDs are unique",
            len(fixture_ids) == len(set(fixture_ids)),
            f"{len(set(fixture_ids))} unique IDs",
        ),
        VerificationCheck(
            "Fixture history is captured",
            fixture_history_count == summary["fixtures"],
            (
                f"{fixture_history_count} fixture observations for "
                f"{summary['fixtures']} fixtures in this run"
            ),
        ),
        VerificationCheck(
            "Capture timing matches observation kind",
            timing_passed,
            timing_detail,
        ),
        VerificationCheck(
            "Database relationships are valid",
            not foreign_keys,
            f"{len(foreign_keys)} foreign-key issue(s)",
        ),
        VerificationCheck(
            "Ingestion row count matches",
            ingestion["row_count"] == expected_rows,
            f"{ingestion['row_count']} rows processed; expected {expected_rows}",
        ),
        VerificationCheck(
            "Source checksum recorded",
            bool(ingestion["content_sha256"]),
            str(ingestion["content_sha256"] or "Missing checksum"),
        ),
    ]


def _players_csv(players: list[dict[str, Any]]) -> bytes:
    headers = [
        "player_id",
        "web_name",
        "first_name",
        "second_name",
        "team",
        "team_short_name",
        "position",
        "price_millions",
        "selected_by_percent",
        "selected_count",
        "transfers_in",
        "transfers_out",
        "status",
        "chance_of_playing_next_round",
        "news",
        "captured_at",
        "observed_on",
        "timing_quality",
        "observation_kind",
        "minutes",
        "starts",
        "goals",
        "assists",
        "clean_sheets",
        "bonus",
        "bps",
        "defensive_contributions",
        "expected_goals",
        "expected_assists",
        "expected_goal_involvements",
        "total_points",
    ]
    rows = []
    for player in players:
        row = dict(player)
        price = row.pop("price_tenths")
        row["price_millions"] = "" if price is None else f"{price / 10:.1f}"
        rows.append(row)
    return _csv_bytes(headers, rows)


def _fixtures_csv(fixtures: list[dict[str, Any]]) -> bytes:
    headers = [
        "fixture_id",
        "gameweek",
        "kickoff_time",
        "home_team",
        "away_team",
        "home_score",
        "away_score",
        "finished",
    ]
    return _csv_bytes(headers, fixtures)


def _csv_bytes(headers: list[str], rows: list[dict[str, Any]]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=headers, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("utf-8-sig")


def _html_report(
    *,
    season_code: str,
    gameweek_number: int,
    captured_at: datetime,
    archive_directory: Path,
    summary: dict[str, int],
    gameweek: dict[str, Any],
    ingestion: dict[str, Any],
    checks: list[VerificationCheck],
    players: list[dict[str, Any]],
    fixtures: list[dict[str, Any]],
) -> str:
    passed = sum(check.passed for check in checks)
    all_passed = passed == len(checks)
    top_price = sorted(players, key=lambda row: row["price_tenths"] or 0, reverse=True)[:10]
    top_owned = sorted(
        players,
        key=lambda row: row["selected_by_percent"] or 0,
        reverse=True,
    )[:10]
    cards = [
        ("Overall", "All checks passed" if all_passed else "Review failed checks"),
        ("Teams", summary["teams"]),
        ("Players", summary["players"]),
        ("Fixtures", summary["fixtures"]),
        ("Snapshot", f"GW{gameweek_number}"),
        ("Deadline", gameweek["deadline_time"] or "Not supplied"),
    ]
    check_rows = [
        ("PASS" if check.passed else "FAIL", check.name, check.detail)
        for check in checks
    ]
    player_rows = [
        (
            row["player_id"],
            row["web_name"],
            row["team"],
            row["position"],
            _price(row["price_tenths"]),
            _percent(row["selected_by_percent"]),
            row["selected_count"] or "",
            STATUS_LABELS.get(row["status"], row["status"] or "Unknown"),
            _chance(row["chance_of_playing_next_round"]),
            row["news"] or "",
        )
        for row in players
    ]
    fixture_rows = [
        (
            row["fixture_id"],
            row["gameweek"] or "",
            row["kickoff_time"] or "",
            row["home_team"],
            row["away_team"],
            _score(row),
            "Yes" if row["finished"] else "No",
        )
        for row in fixtures
    ]
    ranking_headers = ("Player", "Team", "Position", "Value")
    price_rows = [
        (
            row["web_name"],
            row["team_short_name"],
            row["position"],
            _price(row["price_tenths"]),
        )
        for row in top_price
    ]
    owned_rows = [
        (
            row["web_name"],
            row["team_short_name"],
            row["position"],
            _percent(row["selected_by_percent"]),
        )
        for row in top_owned
    ]
    player_headers = (
        "ID",
        "Player",
        "Team",
        "Pos",
        "Price",
        "Owned",
        "Selected",
        "Status",
        "Chance",
        "News",
    )
    fixture_headers = ("ID", "GW", "Kick-off", "Home", "Away", "Score", "Finished")
    provenance = [
        ("Ingestion run", ingestion["id"]),
        ("Source", ingestion["source_name"]),
        ("Identifier namespace", ingestion["identifier_namespace"]),
        ("Source URL", ingestion["source_url"] or ""),
        ("Retrieved", ingestion["retrieved_at"]),
        ("SHA-256", ingestion["content_sha256"] or ""),
        ("Source revision", ingestion["source_revision"] or ""),
        ("Adapter version", ingestion["adapter_version"] or ""),
        ("Raw archive", archive_directory),
    ]
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FPL verification — {_e(season_code)}</title>
<style>
body{{font-family:system-ui;margin:0;background:#0f172a;color:#e2e8f0}}
main{{max-width:1500px;margin:auto;padding:24px}}a{{color:#7dd3fc}}
.cards,.two{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}}
.card,section{{background:#1e293b;border:1px solid #334155;border-radius:10px;padding:14px}}
section{{margin-top:14px}}.value{{font-size:1.4rem;font-weight:700}}
.wrap{{overflow:auto;max-height:600px}}
table{{width:100%;border-collapse:collapse;font-size:.9rem}}
th,td{{padding:8px;border-bottom:1px solid #334155;text-align:left;white-space:nowrap}}
th{{position:sticky;top:0;background:#334155}}
input{{padding:9px;width:min(420px,90%);margin:8px 0;background:#0f172a;color:inherit;
border:1px solid #64748b}}
</style></head><body><main>
<h1>FPL data verification</h1>
<p>Season {_e(season_code)} · Captured {_e(captured_at.isoformat())}</p>
<div class="cards">{''.join(_card(*card) for card in cards)}</div>
<section><h2>Integrity checks</h2><p>{passed} of {len(checks)} passed.</p>
{_table(("Result", "Check", "Detail"), check_rows)}</section>
<div class="two"><section><h2>Most expensive</h2>
{_table(ranking_headers, price_rows)}</section>
<section><h2>Highest ownership</h2>{_table(ranking_headers, owned_rows)}</section></div>
<section><h2>Players</h2><p><a href="players.csv">Open players.csv in Excel</a></p>
<input id="pf" placeholder="Search players" oninput="filterRows('pf','players')">
<div class="wrap">{_table(player_headers, player_rows, "players")}</div></section>
<section><h2>Fixtures</h2><p><a href="fixtures.csv">Open fixtures.csv in Excel</a></p>
<input id="ff" placeholder="Search fixtures" oninput="filterRows('ff','fixtures')">
<div class="wrap">{_table(fixture_headers, fixture_rows, "fixtures")}</div></section>
<section><h2>Provenance</h2>{_table(("Field", "Value"), provenance)}</section>
<script>
function filterRows(i,t){{
const q=document.getElementById(i).value.toLowerCase();
document.querySelectorAll(`#${{t}} tbody tr`).forEach(
r=>r.hidden=!r.innerText.toLowerCase().includes(q));
}}
</script>
</main></body></html>"""


def _table(
    headers: tuple[str, ...], rows: list[tuple[object, ...]], table_id: str = ""
) -> str:
    head = "".join(f"<th>{_e(value)}</th>" for value in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{_e(value)}</td>" for value in row) + "</tr>"
        for row in rows
    )
    identifier = f' id="{table_id}"' if table_id else ""
    return f"<table{identifier}><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _card(label: object, value: object) -> str:
    return f'<div class="card"><div>{_e(label)}</div><div class="value">{_e(value)}</div></div>'


def _price(value: int | None) -> str:
    return "" if value is None else f"£{value / 10:.1f}m"


def _percent(value: float | None) -> str:
    return "" if value is None else f"{value:.1f}%"


def _chance(value: int | None) -> str:
    return "" if value is None else f"{value}%"


def _score(row: dict[str, Any]) -> str:
    if row["home_score"] is None or row["away_score"] is None:
        return ""
    return f"{row['home_score']}–{row['away_score']}"


def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


def _atomic_write(path: Path, content: bytes) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(content)
    temporary.replace(path)
