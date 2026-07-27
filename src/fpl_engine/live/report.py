"""Human-readable verification exports for a collected FPL snapshot."""

from __future__ import annotations

import csv
import html
import io
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from fpl_engine.history.database import HistoricalDatabase

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
) -> VerificationReport:
    """Export the normalised database rows as CSV and a browser report."""

    players = _players(database, season_code, gameweek_number)
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
               status, row_count
        FROM ingestion_runs WHERE id = ?
        """,
        (ingestion_run_id,),
        "Ingestion run",
    )
    checks = _checks(database, players, fixtures, summary, ingestion)

    stamp = captured_at.strftime("%Y%m%dT%H%M%SZ")
    season_root = Path(report_root) / season_code
    directory = season_root / stamp
    latest = season_root / "latest"
    directory.mkdir(parents=True, exist_ok=False)
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
    database: HistoricalDatabase, season_code: str, gameweek_number: int
) -> list[dict[str, Any]]:
    rows = database.connection.execute(
        """
        SELECT players.source_player_id AS player_id, players.web_name,
               players.first_name, players.second_name, teams.name AS team,
               teams.short_name AS team_short_name, ps.position,
               snapshots.price_tenths, snapshots.selected_by_percent,
               snapshots.transfers_in, snapshots.transfers_out, snapshots.status,
               snapshots.chance_of_playing_next_round, snapshots.news,
               snapshots.captured_at
        FROM player_gameweek_snapshots snapshots
        JOIN player_seasons ps ON ps.id = snapshots.player_season_id
        JOIN seasons ON seasons.id = ps.season_id
        JOIN players ON players.id = ps.player_id
        JOIN teams ON teams.id = ps.team_id
        JOIN gameweeks ON gameweeks.id = snapshots.gameweek_id
        WHERE seasons.code = ? AND gameweeks.number = ?
        ORDER BY players.web_name COLLATE NOCASE, players.source_player_id
        """,
        (season_code, gameweek_number),
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
        WHERE seasons.code = ?
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
) -> list[VerificationCheck]:
    player_ids = [row["player_id"] for row in players]
    fixture_ids = [row["fixture_id"] for row in fixtures]
    expected_rows = (
        summary["teams"]
        + (summary["players"] * 3)
        + summary["gameweeks"]
        + summary["fixtures"]
    )
    foreign_keys = database.connection.execute("PRAGMA foreign_key_check").fetchall()
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
        "transfers_in",
        "transfers_out",
        "status",
        "chance_of_playing_next_round",
        "news",
        "captured_at",
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
        "Status",
        "Chance",
        "News",
    )
    fixture_headers = ("ID", "GW", "Kick-off", "Home", "Away", "Score", "Finished")
    provenance = [
        ("Ingestion run", ingestion["id"]),
        ("Source", ingestion["source_name"]),
        ("Source URL", ingestion["source_url"] or ""),
        ("Retrieved", ingestion["retrieved_at"]),
        ("SHA-256", ingestion["content_sha256"] or ""),
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
