"""Validated, append-only manager squad snapshots."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .config import SeasonRules
from .domain import Player, Position, Squad
from .history.database import HistoricalDatabase
from .rules import calculate_selling_price, validate_squad


class ManagerStateError(ValueError):
    """Raised when a manager snapshot is incomplete or illegal."""

    def __init__(self, messages: tuple[str, ...]) -> None:
        self.messages = messages
        super().__init__("; ".join(messages))


@dataclass(frozen=True)
class ManagerSquadEntry:
    source_player_id: str
    purchase_price_tenths: int
    selling_price_tenths: int
    is_starter: bool
    bench_order: int | None = None


@dataclass(frozen=True)
class ManagerSnapshot:
    season_code: str
    gameweek_number: int
    captured_at: datetime
    bank_tenths: int
    free_transfers: int
    remaining_chips: dict[str, int]
    entries: tuple[ManagerSquadEntry, ...]
    captain_source_player_id: str | None = None
    vice_captain_source_player_id: str | None = None
    data_ingestion_run_id: int | None = None
    note: str | None = None


@dataclass(frozen=True)
class StoredManagerSnapshot:
    snapshot_id: int
    deadline_time: str | None
    snapshot: ManagerSnapshot


class ManagerStateRepository:
    """Persists private manager state in local SQLite only."""

    def __init__(self, database: HistoricalDatabase, rules: SeasonRules) -> None:
        self.database = database
        self.rules = rules

    def available_players(
        self, season_code: str, gameweek_number: int
    ) -> list[dict[str, Any]]:
        rows = self.database.connection.execute(
            """
            WITH ranked AS (
                SELECT observations.*,
                       ROW_NUMBER() OVER (
                           PARTITION BY observations.player_season_id
                           ORDER BY
                               CASE observations.timing_quality
                                   WHEN 'exact' THEN 0
                                   WHEN 'date_only' THEN 1
                                   ELSE 2
                               END,
                               observations.observed_at DESC,
                               observations.observed_on DESC,
                               observations.id DESC
                       ) AS observation_rank
                FROM player_gameweek_observations observations
                JOIN gameweeks ON gameweeks.id = observations.gameweek_id
                JOIN seasons ON seasons.id = gameweeks.season_id
                WHERE seasons.code = ? AND gameweeks.number = ?
            )
            SELECT ps.source_player_id, ps.id AS player_season_id,
                   players.web_name, ps.position, teams.id AS team_id,
                   teams.name AS team_name, teams.short_name AS team_short_name,
                   ranked.price_tenths, ranked.status, ranked.news,
                   ranked.observed_at, ranked.observed_on, ranked.timing_quality
            FROM ranked
            JOIN player_seasons ps ON ps.id = ranked.player_season_id
            JOIN players ON players.id = ps.player_id
            JOIN teams ON teams.id = COALESCE(ranked.team_id, ps.team_id)
            WHERE ranked.observation_rank = 1
              AND ps.identifier_namespace = 'official-fpl'
            ORDER BY ps.position, players.web_name COLLATE NOCASE
            """,
            (season_code, gameweek_number),
        ).fetchall()
        return [dict(row) for row in rows]

    def save(self, snapshot: ManagerSnapshot) -> int:
        resolved, errors = self._validate(snapshot)
        if errors:
            raise ManagerStateError(tuple(errors))

        captured_at = snapshot.captured_at.astimezone(UTC).isoformat()
        chips_json = json.dumps(
            snapshot.remaining_chips, sort_keys=True, separators=(",", ":")
        )
        try:
            with self.database.transaction():
                cursor = self.database.connection.execute(
                    """
                    INSERT INTO manager_snapshots (
                        season_id, gameweek_id, data_ingestion_run_id,
                        captured_at, bank_tenths, free_transfers,
                        remaining_chips_json, captain_player_season_id,
                        vice_captain_player_season_id, note
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    RETURNING id
                    """,
                    (
                        resolved["season_id"],
                        resolved["gameweek_id"],
                        snapshot.data_ingestion_run_id,
                        captured_at,
                        snapshot.bank_tenths,
                        snapshot.free_transfers,
                        chips_json,
                        resolved["captain_id"],
                        resolved["vice_captain_id"],
                        snapshot.note,
                    ),
                )
                snapshot_id = int(cursor.fetchone()[0])
                self.database.connection.executemany(
                    """
                    INSERT INTO manager_squad_entries (
                        manager_snapshot_id, player_season_id,
                        purchase_price_tenths, selling_price_tenths,
                        is_starter, bench_order
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        (
                            snapshot_id,
                            resolved["players"][entry.source_player_id]["player_season_id"],
                            entry.purchase_price_tenths,
                            entry.selling_price_tenths,
                            int(entry.is_starter),
                            entry.bench_order,
                        )
                        for entry in snapshot.entries
                    ),
                )
            return snapshot_id
        except sqlite3.IntegrityError as error:
            raise ManagerStateError((f"Manager snapshot could not be saved: {error}",)) from error

    def latest(
        self, season_code: str, gameweek_number: int | None = None
    ) -> StoredManagerSnapshot | None:
        gameweek_filter = "" if gameweek_number is None else "AND gameweeks.number = ?"
        values: tuple[object, ...] = (
            (season_code,)
            if gameweek_number is None
            else (season_code, gameweek_number)
        )
        row = self.database.connection.execute(
            f"""
            SELECT manager_snapshots.*, seasons.code AS season_code,
                   gameweeks.number AS gameweek_number,
                   gameweeks.deadline_time,
                   captain.source_player_id AS captain_source_player_id,
                   vice.source_player_id AS vice_captain_source_player_id
            FROM manager_snapshots
            JOIN seasons ON seasons.id = manager_snapshots.season_id
            JOIN gameweeks ON gameweeks.id = manager_snapshots.gameweek_id
            LEFT JOIN player_seasons captain
              ON captain.id = manager_snapshots.captain_player_season_id
            LEFT JOIN player_seasons vice
              ON vice.id = manager_snapshots.vice_captain_player_season_id
            WHERE seasons.code = ? {gameweek_filter}
            ORDER BY manager_snapshots.captured_at DESC, manager_snapshots.id DESC
            LIMIT 1
            """,
            values,
        ).fetchone()
        if row is None:
            return None
        entry_rows = self.database.connection.execute(
            """
            SELECT ps.source_player_id, entries.purchase_price_tenths,
                   entries.selling_price_tenths, entries.is_starter,
                   entries.bench_order
            FROM manager_squad_entries entries
            JOIN player_seasons ps ON ps.id = entries.player_season_id
            WHERE entries.manager_snapshot_id = ?
            ORDER BY entries.is_starter DESC, entries.bench_order, entries.id
            """,
            (row["id"],),
        ).fetchall()
        entries = tuple(
            ManagerSquadEntry(
                source_player_id=entry["source_player_id"],
                purchase_price_tenths=entry["purchase_price_tenths"],
                selling_price_tenths=entry["selling_price_tenths"],
                is_starter=bool(entry["is_starter"]),
                bench_order=entry["bench_order"],
            )
            for entry in entry_rows
        )
        captured_at = datetime.fromisoformat(row["captured_at"])
        return StoredManagerSnapshot(
            snapshot_id=int(row["id"]),
            deadline_time=row["deadline_time"],
            snapshot=ManagerSnapshot(
                season_code=row["season_code"],
                gameweek_number=int(row["gameweek_number"]),
                captured_at=captured_at,
                bank_tenths=int(row["bank_tenths"]),
                free_transfers=int(row["free_transfers"]),
                remaining_chips=json.loads(row["remaining_chips_json"]),
                entries=entries,
                captain_source_player_id=row["captain_source_player_id"],
                vice_captain_source_player_id=row["vice_captain_source_player_id"],
                data_ingestion_run_id=row["data_ingestion_run_id"],
                note=row["note"],
            ),
        )

    def _validate(
        self, snapshot: ManagerSnapshot
    ) -> tuple[dict[str, Any], list[str]]:
        errors: list[str] = []
        if snapshot.captured_at.tzinfo is None:
            errors.append("Snapshot time must be timezone-aware")
        if snapshot.bank_tenths < 0:
            errors.append("Money in the bank cannot be negative")
        if not 0 <= snapshot.free_transfers <= self.rules.transfers.maximum_free_transfers:
            errors.append(
                "Free transfers must be between 0 and "
                f"{self.rules.transfers.maximum_free_transfers}"
            )

        expected_chips = set(self.rules.chips.names)
        if set(snapshot.remaining_chips) != expected_chips:
            errors.append("Remaining chips must include every configured chip exactly once")
        elif any(
            not 0 <= count <= self.rules.chips.sets_per_season
            for count in snapshot.remaining_chips.values()
        ):
            errors.append(
                f"Each remaining-chip count must be between 0 and "
                f"{self.rules.chips.sets_per_season}"
            )

        season_row = self.database.connection.execute(
            "SELECT id FROM seasons WHERE code = ?", (snapshot.season_code,)
        ).fetchone()
        if season_row is None:
            errors.append(f"Season {snapshot.season_code!r} is not in the database")
            season_id = None
        else:
            season_id = int(season_row["id"])
        gameweek_row = (
            None
            if season_id is None
            else self.database.connection.execute(
                "SELECT id FROM gameweeks WHERE season_id = ? AND number = ?",
                (season_id, snapshot.gameweek_number),
            ).fetchone()
        )
        if gameweek_row is None:
            errors.append(
                f"Gameweek {snapshot.gameweek_number} is not available for "
                f"{snapshot.season_code}"
            )

        source_ids = [entry.source_player_id for entry in snapshot.entries]
        if len(set(source_ids)) != len(source_ids):
            errors.append("Squad contains duplicate players")
        for role, source_player_id in (
            ("Captain", snapshot.captain_source_player_id),
            ("Vice-captain", snapshot.vice_captain_source_player_id),
        ):
            if source_player_id is not None and source_player_id not in source_ids:
                errors.append(f"{role} must be selected from the squad")
        players = self._resolve_players(snapshot.season_code, source_ids)
        missing = sorted(set(source_ids) - set(players))
        if missing:
            errors.append(f"Squad contains unknown player IDs: {', '.join(missing)}")

        if not missing:
            squad = Squad(
                players=tuple(
                    Player(
                        player_id=players[entry.source_player_id]["player_season_id"],
                        name=players[entry.source_player_id]["web_name"],
                        team_id=players[entry.source_player_id]["team_id"],
                        position=Position(players[entry.source_player_id]["position"]),
                        price_tenths=players[entry.source_player_id]["current_price_tenths"],
                    )
                    for entry in snapshot.entries
                ),
                starting_player_ids=frozenset(
                    players[entry.source_player_id]["player_season_id"]
                    for entry in snapshot.entries
                    if entry.is_starter
                ),
                bench_player_ids=tuple(
                    players[entry.source_player_id]["player_season_id"]
                    for entry in sorted(
                        (entry for entry in snapshot.entries if not entry.is_starter),
                        key=lambda entry: entry.bench_order or 99,
                    )
                ),
                captain_id=self._role_id(snapshot.captain_source_player_id, players),
                vice_captain_id=self._role_id(
                    snapshot.vice_captain_source_player_id, players
                ),
            )
            errors.extend(
                error.message
                for error in validate_squad(
                    squad, self.rules, check_budget=False
                )
            )
            for entry in snapshot.entries:
                current_price = players[entry.source_player_id]["current_price_tenths"]
                expected_selling = calculate_selling_price(
                    entry.purchase_price_tenths, current_price, self.rules
                )
                if entry.selling_price_tenths != expected_selling:
                    errors.append(
                        f"{players[entry.source_player_id]['web_name']} selling price "
                        f"should be {expected_selling / 10:.1f}"
                    )

        return (
            {
                "season_id": season_id,
                "gameweek_id": (
                    None if gameweek_row is None else int(gameweek_row["id"])
                ),
                "players": players,
                "captain_id": self._role_id(
                    snapshot.captain_source_player_id, players
                ),
                "vice_captain_id": self._role_id(
                    snapshot.vice_captain_source_player_id, players
                ),
            },
            errors,
        )

    def _resolve_players(
        self, season_code: str, source_player_ids: list[str]
    ) -> dict[str, dict[str, Any]]:
        if not source_player_ids:
            return {}
        placeholders = ",".join("?" for _ in source_player_ids)
        rows = self.database.connection.execute(
            f"""
            WITH current_prices AS (
                SELECT observations.player_season_id, observations.price_tenths,
                       ROW_NUMBER() OVER (
                           PARTITION BY observations.player_season_id
                           ORDER BY observations.observed_at DESC,
                                    observations.observed_on DESC,
                                    observations.id DESC
                       ) AS price_rank
                FROM player_gameweek_observations observations
            )
            SELECT ps.source_player_id, ps.id AS player_season_id,
                   players.web_name, ps.position, ps.team_id,
                   current_prices.price_tenths AS current_price_tenths
            FROM player_seasons ps
            JOIN seasons ON seasons.id = ps.season_id
            JOIN players ON players.id = ps.player_id
            JOIN current_prices
              ON current_prices.player_season_id = ps.id
             AND current_prices.price_rank = 1
            WHERE seasons.code = ?
              AND ps.identifier_namespace = 'official-fpl'
              AND ps.source_player_id IN ({placeholders})
            """,
            (season_code, *source_player_ids),
        ).fetchall()
        return {row["source_player_id"]: dict(row) for row in rows}

    @staticmethod
    def _role_id(
        source_player_id: str | None, players: dict[str, dict[str, Any]]
    ) -> int | None:
        if source_player_id is None or source_player_id not in players:
            return None
        return int(players[source_player_id]["player_season_id"])
