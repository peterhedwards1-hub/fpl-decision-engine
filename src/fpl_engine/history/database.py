"""Historical FPL database and idempotent ingestion operations."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .records import (
    FixtureRecord,
    GameweekRecord,
    HistoricalBundle,
    IngestionSource,
    PlayerFixtureStatsRecord,
    PlayerGameweekSnapshotRecord,
    PlayerRecord,
    PlayerSeasonRecord,
    SeasonRecord,
    TeamRecord,
)
from .schema import SCHEMA_SQL, SCHEMA_VERSION


class HistoricalDatabase:
    """Owns the SQLite connection and all historical-data persistence."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> HistoricalDatabase:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def initialise(self) -> None:
        current_version = self.schema_version
        if current_version > SCHEMA_VERSION:
            raise RuntimeError(
                f"Database schema version {current_version} is newer than supported "
                f"version {SCHEMA_VERSION}"
            )
        self.connection.executescript(SCHEMA_SQL)
        if current_version < 2:
            columns = {
                row[1]
                for row in self.connection.execute(
                    "PRAGMA table_info(player_gameweek_snapshots)"
                )
            }
            if "team_id" not in columns:
                self.connection.execute(
                    "ALTER TABLE player_gameweek_snapshots ADD COLUMN "
                    "team_id INTEGER REFERENCES teams(id)"
                )
        self.connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self.connection.commit()

    @property
    def schema_version(self) -> int:
        row = self.connection.execute("PRAGMA user_version").fetchone()
        return int(row[0])

    @contextmanager
    def transaction(self) -> Iterator[None]:
        try:
            self.connection.execute("BEGIN")
            yield
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def start_ingestion(self, source: IngestionSource) -> int:
        cursor = self.connection.execute(
            """
            INSERT INTO ingestion_runs (
                source_name, source_url, retrieved_at, content_sha256, status
            ) VALUES (?, ?, ?, ?, 'running')
            """,
            (
                source.name,
                source.url,
                source.retrieved_at.isoformat(),
                source.content_sha256,
            ),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def finish_ingestion(
        self,
        run_id: int,
        *,
        row_count: int,
        error_message: str | None = None,
    ) -> None:
        status = "failed" if error_message else "completed"
        self.connection.execute(
            """
            UPDATE ingestion_runs
            SET status = ?, row_count = ?, error_message = ?
            WHERE id = ?
            """,
            (status, row_count, error_message, run_id),
        )
        self.connection.commit()

    def ingest_bundle(self, source: IngestionSource, bundle: HistoricalBundle) -> int:
        """Atomically upsert a complete normalised historical bundle.

        The ingestion run itself is retained if ingestion fails, while all domain
        rows from the failed transaction are rolled back.
        """

        run_id = self.start_ingestion(source)
        row_count = 0
        try:
            with self.transaction():
                season_id = self.upsert_season(bundle.season)
                for record in bundle.teams:
                    self.upsert_team(season_id, source.name, record, run_id)
                    row_count += 1
                for record in bundle.players:
                    self.upsert_player(source.name, record)
                    row_count += 1
                for record in bundle.player_seasons:
                    self.upsert_player_season(
                        season_id, source.name, record, run_id
                    )
                    row_count += 1
                for record in bundle.gameweeks:
                    self.upsert_gameweek(season_id, record, run_id)
                    row_count += 1
                for record in bundle.fixtures:
                    self.upsert_fixture(season_id, source.name, record, run_id)
                    row_count += 1
                for record in bundle.fixture_stats:
                    self.upsert_fixture_stats(
                        season_id, source.name, record, run_id
                    )
                    row_count += 1
                for record in bundle.gameweek_snapshots:
                    self.upsert_gameweek_snapshot(
                        season_id, source.name, record, run_id
                    )
                    row_count += 1
        except Exception as error:
            self.finish_ingestion(run_id, row_count=0, error_message=str(error))
            raise

        self.finish_ingestion(run_id, row_count=row_count)
        return run_id

    def upsert_season(self, record: SeasonRecord) -> int:
        return self._upsert_id(
            """
            INSERT INTO seasons (code, name, starts_on, ends_on)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(code) DO UPDATE SET
                name = excluded.name,
                starts_on = excluded.starts_on,
                ends_on = excluded.ends_on
            RETURNING id
            """,
            (record.code, record.name, record.starts_on, record.ends_on),
        )

    def upsert_team(
        self, season_id: int, source_name: str, record: TeamRecord, run_id: int
    ) -> int:
        self._assert_source_ownership(
            "teams", season_id, "source_team_id", record.source_team_id, source_name
        )
        return self._upsert_id(
            """
            INSERT INTO teams (
                season_id, source_team_id, name, short_name, provenance_run_id
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(season_id, source_team_id) DO UPDATE SET
                name = excluded.name,
                short_name = excluded.short_name,
                provenance_run_id = excluded.provenance_run_id
            RETURNING id
            """,
            (season_id, record.source_team_id, record.name, record.short_name, run_id),
        )

    def upsert_player(self, source_name: str, record: PlayerRecord) -> int:
        return self._upsert_id(
            """
            INSERT INTO players (
                source_name, source_player_id, first_name, second_name, web_name,
                date_of_birth
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_name, source_player_id) DO UPDATE SET
                first_name = excluded.first_name,
                second_name = excluded.second_name,
                web_name = excluded.web_name,
                date_of_birth = excluded.date_of_birth
            RETURNING id
            """,
            (
                source_name,
                record.source_player_id,
                record.first_name,
                record.second_name,
                record.web_name,
                record.date_of_birth,
            ),
        )

    def upsert_player_season(
        self,
        season_id: int,
        source_name: str,
        record: PlayerSeasonRecord,
        run_id: int,
    ) -> int:
        player_id = self._required_id(
            "SELECT id FROM players WHERE source_name = ? AND source_player_id = ?",
            (source_name, record.source_player_id),
            "player",
        )
        team_id = self._required_id(
            "SELECT id FROM teams WHERE season_id = ? AND source_team_id = ?",
            (season_id, record.source_team_id),
            "team",
        )
        return self._upsert_id(
            """
            INSERT INTO player_seasons (
                season_id, player_id, team_id, position, start_price_tenths,
                end_price_tenths, provenance_run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(season_id, player_id) DO UPDATE SET
                position = excluded.position,
                start_price_tenths = excluded.start_price_tenths,
                end_price_tenths = excluded.end_price_tenths,
                provenance_run_id = excluded.provenance_run_id
            RETURNING id
            """,
            (
                season_id,
                player_id,
                team_id,
                record.position.value,
                record.start_price_tenths,
                record.end_price_tenths,
                run_id,
            ),
        )

    def upsert_gameweek(
        self, season_id: int, record: GameweekRecord, run_id: int
    ) -> int:
        return self._upsert_id(
            """
            INSERT INTO gameweeks (
                season_id, number, deadline_time, is_finished, provenance_run_id
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(season_id, number) DO UPDATE SET
                deadline_time = excluded.deadline_time,
                is_finished = excluded.is_finished,
                provenance_run_id = excluded.provenance_run_id
            RETURNING id
            """,
            (
                season_id,
                record.number,
                record.deadline_time,
                int(record.is_finished),
                run_id,
            ),
        )

    def upsert_fixture(
        self, season_id: int, source_name: str, record: FixtureRecord, run_id: int
    ) -> int:
        self._assert_source_ownership(
            "fixtures", season_id, "source_fixture_id", record.source_fixture_id, source_name
        )
        home_team_id = self._team_id(season_id, record.home_team_source_id)
        away_team_id = self._team_id(season_id, record.away_team_source_id)
        gameweek_id = None
        if record.gameweek_number is not None:
            gameweek_id = self._gameweek_id(season_id, record.gameweek_number)
        return self._upsert_id(
            """
            INSERT INTO fixtures (
                season_id, source_fixture_id, gameweek_id, kickoff_time,
                home_team_id, away_team_id, home_score, away_score, finished,
                provenance_run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(season_id, source_fixture_id) DO UPDATE SET
                gameweek_id = excluded.gameweek_id,
                kickoff_time = excluded.kickoff_time,
                home_team_id = excluded.home_team_id,
                away_team_id = excluded.away_team_id,
                home_score = excluded.home_score,
                away_score = excluded.away_score,
                finished = excluded.finished,
                provenance_run_id = excluded.provenance_run_id
            RETURNING id
            """,
            (
                season_id,
                record.source_fixture_id,
                gameweek_id,
                record.kickoff_time,
                home_team_id,
                away_team_id,
                record.home_score,
                record.away_score,
                int(record.finished),
                run_id,
            ),
        )

    def upsert_fixture_stats(
        self,
        season_id: int,
        source_name: str,
        record: PlayerFixtureStatsRecord,
        run_id: int,
    ) -> int:
        player_season_id = self._player_season_id(
            season_id, source_name, record.source_player_id
        )
        fixture_id = self._fixture_id(season_id, record.source_fixture_id)
        values = (
            player_season_id,
            fixture_id,
            record.minutes,
            int(record.starts),
            record.goals,
            record.assists,
            int(record.clean_sheet),
            record.goals_conceded,
            record.own_goals,
            record.penalties_saved,
            record.penalties_missed,
            record.yellow_cards,
            record.red_cards,
            record.saves,
            record.bonus,
            record.bps,
            record.defensive_contributions,
            record.expected_goals,
            record.expected_assists,
            record.expected_goal_involvements,
            record.expected_goals_conceded,
            record.total_points,
            run_id,
        )
        return self._upsert_id(
            """
            INSERT INTO player_fixture_stats (
                player_season_id, fixture_id, minutes, starts, goals, assists,
                clean_sheet, goals_conceded, own_goals, penalties_saved,
                penalties_missed, yellow_cards, red_cards, saves, bonus, bps,
                defensive_contributions, expected_goals, expected_assists,
                expected_goal_involvements, expected_goals_conceded, total_points,
                provenance_run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(player_season_id, fixture_id) DO UPDATE SET
                minutes = excluded.minutes,
                starts = excluded.starts,
                goals = excluded.goals,
                assists = excluded.assists,
                clean_sheet = excluded.clean_sheet,
                goals_conceded = excluded.goals_conceded,
                own_goals = excluded.own_goals,
                penalties_saved = excluded.penalties_saved,
                penalties_missed = excluded.penalties_missed,
                yellow_cards = excluded.yellow_cards,
                red_cards = excluded.red_cards,
                saves = excluded.saves,
                bonus = excluded.bonus,
                bps = excluded.bps,
                defensive_contributions = excluded.defensive_contributions,
                expected_goals = excluded.expected_goals,
                expected_assists = excluded.expected_assists,
                expected_goal_involvements = excluded.expected_goal_involvements,
                expected_goals_conceded = excluded.expected_goals_conceded,
                total_points = excluded.total_points,
                provenance_run_id = excluded.provenance_run_id
            RETURNING id
            """,
            values,
        )

    def upsert_gameweek_snapshot(
        self,
        season_id: int,
        source_name: str,
        record: PlayerGameweekSnapshotRecord,
        run_id: int,
    ) -> int:
        player_season_id = self._player_season_id(
            season_id, source_name, record.source_player_id
        )
        gameweek_id = self._gameweek_id(season_id, record.gameweek_number)
        return self._upsert_id(
            """
            INSERT INTO player_gameweek_snapshots (
                player_season_id, gameweek_id, price_tenths, selected_by_percent, team_id,
                transfers_in, transfers_out, status, chance_of_playing_next_round,
                news, captured_at, provenance_run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(player_season_id, gameweek_id) DO UPDATE SET
                price_tenths = excluded.price_tenths,
                selected_by_percent = excluded.selected_by_percent,
                team_id = excluded.team_id,
                transfers_in = excluded.transfers_in,
                transfers_out = excluded.transfers_out,
                status = excluded.status,
                chance_of_playing_next_round = excluded.chance_of_playing_next_round,
                news = excluded.news,
                captured_at = excluded.captured_at,
                provenance_run_id = excluded.provenance_run_id
            RETURNING id
            """,
            (
                player_season_id,
                gameweek_id,
                record.price_tenths,
                record.selected_by_percent,
                self._team_id(season_id, record.source_team_id)
                if record.source_team_id is not None
                else None,
                record.transfers_in,
                record.transfers_out,
                record.status,
                record.chance_of_playing_next_round,
                record.news,
                record.captured_at.isoformat(),
                run_id,
            ),
        )

    def season_summary(self, season_code: str) -> dict[str, int]:
        season_id = self._required_id(
            "SELECT id FROM seasons WHERE code = ?", (season_code,), "season"
        )
        queries = {
            "teams": "SELECT COUNT(*) FROM teams WHERE season_id = ?",
            "players": "SELECT COUNT(*) FROM player_seasons WHERE season_id = ?",
            "gameweeks": "SELECT COUNT(*) FROM gameweeks WHERE season_id = ?",
            "fixtures": "SELECT COUNT(*) FROM fixtures WHERE season_id = ?",
            "fixture_stats": """
                SELECT COUNT(*) FROM player_fixture_stats stats
                JOIN player_seasons ps ON ps.id = stats.player_season_id
                WHERE ps.season_id = ?
            """,
            "gameweek_snapshots": """
                SELECT COUNT(*) FROM player_gameweek_snapshots snapshots
                JOIN player_seasons ps ON ps.id = snapshots.player_season_id
                WHERE ps.season_id = ?
            """,
        }
        return {
            name: int(self.connection.execute(sql, (season_id,)).fetchone()[0])
            for name, sql in queries.items()
        }

    def player_gameweek_totals(
        self, season_code: str, source_player_id: str, gameweek_number: int
    ) -> sqlite3.Row | None:
        """Return fixture-aggregated performance and the matching GW snapshot."""

        return self.connection.execute(
            """
            SELECT
                players.source_player_id,
                players.web_name,
                gameweeks.number AS gameweek,
                COALESCE(SUM(stats.minutes), 0) AS minutes,
                COALESCE(SUM(stats.total_points), 0) AS total_points,
                COALESCE(SUM(stats.expected_goals), 0.0) AS expected_goals,
                COALESCE(SUM(stats.expected_assists), 0.0) AS expected_assists,
                snapshots.price_tenths,
                snapshots.selected_by_percent,
                snapshots.status
            FROM player_seasons ps
            JOIN seasons ON seasons.id = ps.season_id
            JOIN players ON players.id = ps.player_id
            JOIN gameweeks ON gameweeks.season_id = seasons.id AND gameweeks.number = ?
            LEFT JOIN fixtures ON fixtures.gameweek_id = gameweeks.id
            LEFT JOIN player_fixture_stats stats
                ON stats.player_season_id = ps.id AND stats.fixture_id = fixtures.id
            LEFT JOIN player_gameweek_snapshots snapshots
                ON snapshots.player_season_id = ps.id
                AND snapshots.gameweek_id = gameweeks.id
            WHERE seasons.code = ? AND players.source_player_id = ?
            GROUP BY ps.id, gameweeks.id, snapshots.id
            """,
            (gameweek_number, season_code, source_player_id),
        ).fetchone()

    def _upsert_id(self, sql: str, values: tuple[object, ...]) -> int:
        row = self.connection.execute(sql, values).fetchone()
        if row is None:
            raise RuntimeError("Upsert did not return an identifier")
        return int(row[0])

    def _required_id(
        self, sql: str, values: tuple[object, ...], entity_name: str
    ) -> int:
        row = self.connection.execute(sql, values).fetchone()
        if row is None:
            raise ValueError(f"Referenced {entity_name} does not exist")
        return int(row[0])

    def _assert_source_ownership(
        self,
        table: str,
        season_id: int,
        source_id_column: str,
        source_id: str,
        source_name: str,
    ) -> None:
        allowed_tables = {"teams": "source_team_id", "fixtures": "source_fixture_id"}
        if allowed_tables.get(table) != source_id_column:
            raise ValueError("Unsupported source-owned table")
        row = self.connection.execute(
            f"""
            SELECT ingestion_runs.source_name
            FROM {table}
            JOIN ingestion_runs ON ingestion_runs.id = {table}.provenance_run_id
            WHERE {table}.season_id = ? AND {table}.{source_id_column} = ?
            """,
            (season_id, source_id),
        ).fetchone()
        if row is not None and row[0] != source_name:
            raise ValueError(
                f"{table[:-1].capitalize()} ID {source_id!r} is already owned by "
                f"source {row[0]!r}"
            )

    def _team_id(self, season_id: int, source_team_id: str) -> int:
        return self._required_id(
            "SELECT id FROM teams WHERE season_id = ? AND source_team_id = ?",
            (season_id, source_team_id),
            "team",
        )

    def _gameweek_id(self, season_id: int, number: int) -> int:
        return self._required_id(
            "SELECT id FROM gameweeks WHERE season_id = ? AND number = ?",
            (season_id, number),
            "gameweek",
        )

    def _fixture_id(self, season_id: int, source_fixture_id: str) -> int:
        return self._required_id(
            "SELECT id FROM fixtures WHERE season_id = ? AND source_fixture_id = ?",
            (season_id, source_fixture_id),
            "fixture",
        )

    def _player_season_id(
        self, season_id: int, source_name: str, source_player_id: str
    ) -> int:
        return self._required_id(
            """
            SELECT ps.id
            FROM player_seasons ps
            JOIN players ON players.id = ps.player_id
            WHERE ps.season_id = ?
              AND players.source_name = ?
              AND players.source_player_id = ?
            """,
            (season_id, source_name, source_player_id),
            "player season",
        )
