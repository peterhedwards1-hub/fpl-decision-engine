"""Historical FPL database and transactional ingestion operations."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
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
    PlayerSeasonStatsObservationRecord,
    SeasonRecord,
    TeamRecord,
)
from .schema import (
    MIGRATE_V2_TO_V3_SQL,
    MIGRATE_V3_TO_V4_SQL,
    MIGRATE_V4_TO_V5_SQL,
    MIGRATE_V5_TO_V6_SQL,
    MIGRATE_V6_TO_V7_SQL,
    MIGRATE_V7_TO_V8_SQL,
    MIGRATE_V8_TO_V9_SQL,
    MIGRATE_V9_TO_V10_SQL,
    SCHEMA_SQL,
    SCHEMA_VERSION,
)


def _observation_mode_filter(mode: str) -> str:
    filters = {
        "latest_available": "1 = 1",
        "latest_pre_deadline": "observations.observation_kind = 'live_pre_deadline'",
        "latest_post_gameweek": "observations.observation_kind = 'post_gameweek'",
    }
    try:
        return filters[mode]
    except KeyError as error:
        raise ValueError(f"Unknown observation mode {mode!r}") from error


def _stable_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _utc_timestamp(value: datetime, field_name: str) -> str:
    if value.tzinfo is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC).isoformat()


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
        if current_version == 0:
            self.connection.executescript(SCHEMA_SQL)
            self.connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self.connection.commit()
            return
        if current_version == 2:
            self._migrate_v2_to_v3()
            current_version = 3
        if current_version == 3:
            self._migrate_v3_to_v4()
            current_version = 4
        if current_version == 4:
            self._migrate_v4_to_v5()
            current_version = 5
        if current_version == 5:
            self._migrate_v5_to_v6()
            current_version = 6
        if current_version == 6:
            self._migrate_v6_to_v7()
            current_version = 7
        if current_version == 7:
            self._migrate_v7_to_v8()
            current_version = 8
        if current_version == 8:
            self._migrate_v8_to_v9()
            current_version = 9
        if current_version == 9:
            self._migrate_v9_to_v10()
            return
        if current_version != SCHEMA_VERSION:
            raise RuntimeError(
                f"Cannot migrate database schema version {current_version} to "
                f"version {SCHEMA_VERSION}; supported migrations start at version 2"
            )
        self.connection.executescript(SCHEMA_SQL)
        self.connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self.connection.commit()

    def _migrate_v2_to_v3(self) -> None:
        try:
            self.connection.execute("PRAGMA foreign_keys = OFF")
            self.connection.executescript(MIGRATE_V2_TO_V3_SQL)
            foreign_key_issues = self.connection.execute(
                "PRAGMA foreign_key_check"
            ).fetchall()
            if foreign_key_issues:
                self.connection.rollback()
                raise RuntimeError(
                    f"Version 2 to 3 migration produced {len(foreign_key_issues)} "
                    "foreign-key issue(s)"
                )
            self.connection.commit()
        except Exception as error:
            self.connection.rollback()
            raise RuntimeError(f"Version 2 to 3 migration failed safely: {error}") from error
        finally:
            self.connection.execute("PRAGMA foreign_keys = ON")

    def _migrate_v3_to_v4(self) -> None:
        try:
            timing_rows = self._validate_v3_timing_rows()
            ingestion_rows = self._validate_v3_ingestion_timestamps()
            self.connection.execute("PRAGMA foreign_keys = OFF")
            self.connection.executescript(MIGRATE_V3_TO_V4_SQL)
            for row_id, observed_at in timing_rows.items():
                if observed_at is not None:
                    self.connection.execute(
                        """
                        UPDATE player_gameweek_observations
                        SET observed_at = ?
                        WHERE id = ?
                        """,
                        (observed_at, row_id),
                    )
            for run_id, retrieved_at in ingestion_rows.items():
                self.connection.execute(
                    "UPDATE ingestion_runs SET retrieved_at = ? WHERE id = ?",
                    (retrieved_at, run_id),
                )
            foreign_key_issues = self.connection.execute(
                "PRAGMA foreign_key_check"
            ).fetchall()
            if foreign_key_issues:
                self.connection.rollback()
                raise RuntimeError(
                    f"Version 3 to 4 migration produced {len(foreign_key_issues)} "
                    "foreign-key issue(s)"
                )
            self.connection.commit()
        except Exception as error:
            self.connection.rollback()
            raise RuntimeError(f"Version 3 to 4 migration failed safely: {error}") from error
        finally:
            self.connection.execute("PRAGMA foreign_keys = ON")

    def _migrate_v4_to_v5(self) -> None:
        try:
            self.connection.executescript(MIGRATE_V4_TO_V5_SQL)
            foreign_key_issues = self.connection.execute(
                "PRAGMA foreign_key_check"
            ).fetchall()
            if foreign_key_issues:
                raise RuntimeError(
                    f"Version 4 to 5 migration produced {len(foreign_key_issues)} "
                    "foreign-key issue(s)"
                )
            self.connection.commit()
        except Exception as error:
            self.connection.rollback()
            raise RuntimeError(f"Version 4 to 5 migration failed safely: {error}") from error

    def _migrate_v5_to_v6(self) -> None:
        try:
            self.connection.executescript(MIGRATE_V5_TO_V6_SQL)
            foreign_key_issues = self.connection.execute(
                "PRAGMA foreign_key_check"
            ).fetchall()
            if foreign_key_issues:
                raise RuntimeError(
                    f"Version 5 to 6 migration produced {len(foreign_key_issues)} "
                    "foreign-key issue(s)"
                )
            self.connection.commit()
        except Exception as error:
            self.connection.rollback()
            raise RuntimeError(f"Version 5 to 6 migration failed safely: {error}") from error

    def _migrate_v6_to_v7(self) -> None:
        try:
            self.connection.executescript(MIGRATE_V6_TO_V7_SQL)
            foreign_key_issues = self.connection.execute(
                "PRAGMA foreign_key_check"
            ).fetchall()
            if foreign_key_issues:
                raise RuntimeError(
                    f"Version 6 to 7 migration produced {len(foreign_key_issues)} "
                    "foreign-key issue(s)"
                )
            self.connection.commit()
        except Exception as error:
            self.connection.rollback()
            raise RuntimeError(f"Version 6 to 7 migration failed safely: {error}") from error

    def _migrate_v7_to_v8(self) -> None:
        try:
            self.connection.executescript(MIGRATE_V7_TO_V8_SQL)
            foreign_key_issues = self.connection.execute(
                "PRAGMA foreign_key_check"
            ).fetchall()
            if foreign_key_issues:
                raise RuntimeError(
                    f"Version 7 to 8 migration produced {len(foreign_key_issues)} "
                    "foreign-key issue(s)"
                )
            self.connection.commit()
        except Exception as error:
            self.connection.rollback()
            raise RuntimeError(f"Version 7 to 8 migration failed safely: {error}") from error

    def _migrate_v8_to_v9(self) -> None:
        try:
            self.connection.executescript(MIGRATE_V8_TO_V9_SQL)
            foreign_key_issues = self.connection.execute(
                "PRAGMA foreign_key_check"
            ).fetchall()
            if foreign_key_issues:
                raise RuntimeError(
                    f"Version 8 to 9 migration produced {len(foreign_key_issues)} "
                    "foreign-key issue(s)"
                )
            self.connection.commit()
        except Exception as error:
            self.connection.rollback()
            raise RuntimeError(f"Version 8 to 9 migration failed safely: {error}") from error

    def _migrate_v9_to_v10(self) -> None:
        try:
            self.connection.executescript(MIGRATE_V9_TO_V10_SQL)
            foreign_key_issues = self.connection.execute(
                "PRAGMA foreign_key_check"
            ).fetchall()
            if foreign_key_issues:
                raise RuntimeError(
                    f"Version 9 to 10 migration produced {len(foreign_key_issues)} "
                    "foreign-key issue(s)"
                )
            self.connection.commit()
        except Exception as error:
            self.connection.rollback()
            raise RuntimeError(
                f"Version 9 to 10 migration failed safely: {error}"
            ) from error

    def _validate_v3_timing_rows(self) -> dict[int, str | None]:
        rows = self.connection.execute(
            """
            SELECT id, timing_quality, observed_at
            FROM player_gameweek_observations
            ORDER BY id
            """
        ).fetchall()
        normalized: dict[int, str | None] = {}
        for row in rows:
            row_id = int(row["id"])
            quality = row["timing_quality"]
            observed_at = row["observed_at"]
            if quality not in {"exact", "date_only", "unknown"}:
                raise RuntimeError(
                    f"Version 3 observation row {row_id} has invalid timing_quality "
                    f"value {quality!r}"
                )
            if quality == "unknown":
                if observed_at is not None:
                    raise RuntimeError(
                        "Version 3 observation row "
                        f"{row_id} is inconsistent: timing_quality='unknown' "
                        "has an observed_at timestamp"
                    )
                normalized[row_id] = None
                continue
            if observed_at is None:
                raise RuntimeError(
                    "Version 3 observation row "
                    f"{row_id} is inconsistent: timing_quality={quality!r} "
                    "requires an observed_at timestamp"
                )
            try:
                parsed = datetime.fromisoformat(observed_at)
            except ValueError as error:
                raise RuntimeError(
                    f"Version 3 observation row {row_id} has invalid observed_at "
                    f"value {observed_at!r}"
                ) from error
            if parsed.tzinfo is None:
                raise RuntimeError(
                    f"Version 3 observation row {row_id} has a naive observed_at "
                    f"value {observed_at!r}"
                )
            normalized[row_id] = (
                parsed.astimezone(UTC).isoformat() if quality == "exact" else None
            )
        return normalized

    def _validate_v3_ingestion_timestamps(self) -> dict[int, str]:
        rows = self.connection.execute(
            "SELECT id, retrieved_at FROM ingestion_runs ORDER BY id"
        ).fetchall()
        normalized: dict[int, str] = {}
        for row in rows:
            run_id = int(row["id"])
            retrieved_at = row["retrieved_at"]
            try:
                parsed = datetime.fromisoformat(retrieved_at)
            except (TypeError, ValueError) as error:
                raise RuntimeError(
                    f"Version 3 ingestion run {run_id} has invalid retrieved_at "
                    f"value {retrieved_at!r}"
                ) from error
            if parsed.tzinfo is None:
                raise RuntimeError(
                    f"Version 3 ingestion run {run_id} has a naive retrieved_at "
                    f"value {retrieved_at!r}"
                )
            normalized[run_id] = parsed.astimezone(UTC).isoformat()
        return normalized

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
        retrieved_at = _utc_timestamp(source.retrieved_at, "ingestion retrieved_at")
        cursor = self.connection.execute(
            """
            INSERT INTO ingestion_runs (
                source_name, identifier_namespace, source_url, retrieved_at,
                content_sha256, source_revision, adapter_version, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'running')
            """,
            (
                source.name,
                source.identifier_namespace,
                source.url,
                retrieved_at,
                source.content_sha256,
                source.source_revision,
                source.adapter_version,
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
        """Atomically upsert a complete normalised historical bundle."""

        run_id = self.start_ingestion(source)
        row_count = 0
        try:
            with self.transaction():
                season_id = self.upsert_season(bundle.season)
                player_ids: dict[str, int] = {}
                for record in bundle.teams:
                    self.upsert_team(
                        season_id, source.identifier_namespace, record, run_id
                    )
                    row_count += 1
                for record in bundle.players:
                    player_ids[record.source_player_id] = self.upsert_player(
                        season_id, source, record, run_id
                    )
                    row_count += 1
                for record in bundle.player_seasons:
                    player_id = player_ids.get(record.source_player_id)
                    self.upsert_player_season(
                        season_id,
                        source.identifier_namespace
                        if record.identifier_namespace is None
                        else record.identifier_namespace,
                        record,
                        run_id,
                        player_id=player_id,
                    )
                    row_count += 1
                for record in bundle.gameweeks:
                    self.upsert_gameweek(season_id, record, run_id)
                    row_count += 1
                for record in bundle.fixtures:
                    self.upsert_fixture(
                        season_id, source.identifier_namespace, record, run_id
                    )
                    row_count += 1
                for record in bundle.fixture_stats:
                    self.upsert_fixture_stats(
                        season_id, source.identifier_namespace, record, run_id
                    )
                    row_count += 1
                for record in bundle.season_stats_observations:
                    self.upsert_player_season_stats_observation(
                        season_id,
                        source.identifier_namespace,
                        record,
                        run_id,
                    )
                    row_count += 1
                for record in bundle.gameweek_snapshots:
                    self.upsert_gameweek_observation(
                        season_id,
                        source.identifier_namespace,
                        source,
                        record,
                        run_id,
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
        self,
        season_id: int,
        identifier_namespace: str,
        record: TeamRecord,
        run_id: int,
    ) -> int:
        existing = self.connection.execute(
            """
            SELECT id, name, short_name
            FROM teams
            WHERE season_id = ? AND identifier_namespace = ? AND source_team_id = ?
            """,
            (season_id, identifier_namespace, record.source_team_id),
        ).fetchone()
        if existing is not None and (
            existing["name"] != record.name
            or existing["short_name"] != record.short_name
        ):
            raise ValueError(
                f"Contradictory team identity for namespace {identifier_namespace!r}, "
                f"ID {record.source_team_id!r}: existing {existing['name']!r}, "
                f"incoming {record.name!r}"
            )
        return self._upsert_id(
            """
            INSERT INTO teams (
                season_id, identifier_namespace, source_team_id, name, short_name,
                provenance_run_id
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(season_id, identifier_namespace, source_team_id) DO UPDATE SET
                name = excluded.name,
                short_name = excluded.short_name,
                provenance_run_id = excluded.provenance_run_id
            RETURNING id
            """,
            (
                season_id,
                identifier_namespace,
                record.source_team_id,
                record.name,
                record.short_name,
                run_id,
            ),
        )

    def upsert_player(
        self,
        season_id: int,
        source: IngestionSource,
        record: PlayerRecord,
        run_id: int,
    ) -> int:
        stable_ids = {
            "official_fpl_code": record.official_fpl_code,
            "opta_code": record.opta_code,
        }
        stable_ids = {key: value for key, value in stable_ids.items() if value}
        stable_matches = {
            row["player_id"]
            for identifier_type, identifier_value in stable_ids.items()
            for row in self.connection.execute(
                """
                SELECT player_id FROM player_identifiers
                WHERE identifier_type = ? AND identifier_value = ?
                """,
                (identifier_type, identifier_value),
            )
        }
        if len(stable_matches) > 1:
            raise ValueError(
                f"Stable identifiers for player {record.source_player_id!r} "
                "refer to different identities"
            )

        season_match = self.connection.execute(
            """
            SELECT player_id
            FROM player_seasons
            WHERE season_id = ? AND identifier_namespace = ? AND source_player_id = ?
            """,
            (season_id, source.identifier_namespace, record.source_player_id),
        ).fetchone()
        player_id = next(iter(stable_matches), None)
        if season_match is not None:
            if player_id is not None and player_id != season_match["player_id"]:
                player_id = self.reconcile_player_identities(
                    stable_player_id=player_id,
                    duplicate_player_id=season_match["player_id"],
                )
            else:
                player_id = season_match["player_id"]

        if player_id is None:
            player_id = self._upsert_id(
                """
                INSERT INTO players (
                    first_name, second_name, web_name, date_of_birth, provenance_run_id
                ) VALUES (?, ?, ?, ?, ?)
                RETURNING id
                """,
                (
                    record.first_name,
                    record.second_name,
                    record.web_name,
                    record.date_of_birth,
                    run_id,
                ),
            )
        else:
            self.connection.execute(
                """
                UPDATE players
                SET first_name = ?, second_name = ?, web_name = ?, date_of_birth = ?,
                    provenance_run_id = ?
                WHERE id = ?
                """,
                (
                    record.first_name,
                    record.second_name,
                    record.web_name,
                    record.date_of_birth,
                    run_id,
                    player_id,
                ),
            )

        for identifier_type, identifier_value in stable_ids.items():
            existing = self.connection.execute(
                """
                SELECT player_id FROM player_identifiers
                WHERE identifier_type = ? AND identifier_value = ?
                """,
                (identifier_type, identifier_value),
            ).fetchone()
            if existing is not None and existing["player_id"] != player_id:
                raise ValueError(
                    f"Stable identifier {identifier_type}={identifier_value!r} "
                    "is already assigned to another player"
                )
            existing_for_player = self.connection.execute(
                """
                SELECT identifier_value FROM player_identifiers
                WHERE player_id = ? AND identifier_type = ?
                """,
                (player_id, identifier_type),
            ).fetchone()
            if existing_for_player is not None:
                if existing_for_player["identifier_value"] != identifier_value:
                    raise ValueError(
                        f"Player {player_id} has contradictory {identifier_type} values"
                    )
                continue
            self.connection.execute(
                """
                INSERT INTO player_identifiers (
                    player_id, identifier_type, identifier_value, provenance_run_id
                ) VALUES (?, ?, ?, ?)
                """,
                (player_id, identifier_type, identifier_value, run_id),
            )
        return int(player_id)

    def reconcile_player_identities(
        self, *, stable_player_id: int, duplicate_player_id: int
    ) -> int:
        """Merge an unidentified identity into the identity backed by stable IDs.

        This is deliberately only callable with explicit database identities found
        through stable identifiers; names and descriptive metadata never trigger it.
        The surviving row is the stable-ID identity, so all season memberships and
        their child statistics/observations retain their existing primary keys.
        """

        if stable_player_id == duplicate_player_id:
            return stable_player_id
        if self.connection.in_transaction:
            return self._reconcile_player_identities(stable_player_id, duplicate_player_id)
        with self.transaction():
            return self._reconcile_player_identities(stable_player_id, duplicate_player_id)

    def _reconcile_player_identities(self, stable_player_id: int, duplicate_player_id: int) -> int:
        stable = self.connection.execute(
            "SELECT * FROM players WHERE id = ?", (stable_player_id,)
        ).fetchone()
        duplicate = self.connection.execute(
            "SELECT * FROM players WHERE id = ?", (duplicate_player_id,)
        ).fetchone()
        if stable is None or duplicate is None:
            raise ValueError("Both player identities must exist before reconciliation")

        stable_identifiers = {
            row["identifier_type"]: row["identifier_value"]
            for row in self.connection.execute(
                "SELECT identifier_type, identifier_value "
                "FROM player_identifiers WHERE player_id = ?",
                (stable_player_id,),
            )
        }
        duplicate_identifiers = {
            row["identifier_type"]: row["identifier_value"]
            for row in self.connection.execute(
                "SELECT identifier_type, identifier_value "
                "FROM player_identifiers WHERE player_id = ?",
                (duplicate_player_id,),
            )
        }
        for identifier_type, value in duplicate_identifiers.items():
            existing = stable_identifiers.get(identifier_type)
            if existing is not None and existing != value:
                raise ValueError(
                    f"Cannot reconcile players: contradictory {identifier_type} values "
                    f"{existing!r} and {value!r}"
                )
            collision = self.connection.execute(
                """
                SELECT player_id FROM player_identifiers
                WHERE identifier_type = ? AND identifier_value = ?
                """,
                (identifier_type, value),
            ).fetchone()
            if collision is not None and collision["player_id"] not in {
                stable_player_id,
                duplicate_player_id,
            }:
                raise ValueError(
                    f"Cannot reconcile players: {identifier_type}={value!r} is already assigned"
                )

        duplicate_seasons = self.connection.execute(
            """
            SELECT season_id, identifier_namespace, source_player_id
            FROM player_seasons WHERE player_id = ?
            """,
            (duplicate_player_id,),
        ).fetchall()
        for row in duplicate_seasons:
            collision = self.connection.execute(
                """
                SELECT id FROM player_seasons
                WHERE player_id = ? AND season_id = ?
                  AND identifier_namespace = ? AND source_player_id = ?
                """,
                (
                    stable_player_id,
                    row["season_id"],
                    row["identifier_namespace"],
                    row["source_player_id"],
                ),
            ).fetchone()
            if collision is not None:
                raise ValueError(
                    "Cannot reconcile players: duplicate season-specific identity "
                    f"{row['identifier_namespace']}:{row['source_player_id']}"
                )

        merged_metadata = {
            field: stable[field] or duplicate[field]
            for field in ("first_name", "second_name", "web_name", "date_of_birth")
        }
        self.connection.execute(
            """
            UPDATE players
            SET first_name = ?, second_name = ?, web_name = ?, date_of_birth = ?
            WHERE id = ?
            """,
            (
                merged_metadata["first_name"],
                merged_metadata["second_name"],
                merged_metadata["web_name"],
                merged_metadata["date_of_birth"],
                stable_player_id,
            ),
        )
        self.connection.execute(
            "UPDATE player_seasons SET player_id = ? WHERE player_id = ?",
            (stable_player_id, duplicate_player_id),
        )
        for identifier_type, _value in duplicate_identifiers.items():
            if identifier_type not in stable_identifiers:
                self.connection.execute(
                    "UPDATE player_identifiers SET player_id = ? WHERE player_id = ? "
                    "AND identifier_type = ?",
                    (stable_player_id, duplicate_player_id, identifier_type),
                )
        self.connection.execute("DELETE FROM players WHERE id = ?", (duplicate_player_id,))
        return stable_player_id

    def upsert_player_season(
        self,
        season_id: int,
        identifier_namespace: str,
        record: PlayerSeasonRecord,
        run_id: int,
        *,
        player_id: int | None = None,
    ) -> int:
        player_id = player_id or self._required_id(
            """
            SELECT player_id FROM player_seasons
            WHERE season_id = ? AND identifier_namespace = ? AND source_player_id = ?
            """,
            (season_id, identifier_namespace, record.source_player_id),
            "player season",
        )
        team_id = self._team_id(season_id, identifier_namespace, record.source_team_id)
        return self._upsert_id(
            """
            INSERT INTO player_seasons (
                season_id, player_id, identifier_namespace, source_player_id, team_id,
                position, start_price_tenths, end_price_tenths, provenance_run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(season_id, identifier_namespace, source_player_id) DO UPDATE SET
                player_id = excluded.player_id,
                position = excluded.position,
                start_price_tenths = excluded.start_price_tenths,
                end_price_tenths = excluded.end_price_tenths,
                provenance_run_id = excluded.provenance_run_id
            RETURNING id
            """,
            (
                season_id,
                player_id,
                identifier_namespace,
                record.source_player_id,
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
        self,
        season_id: int,
        identifier_namespace: str,
        record: FixtureRecord,
        run_id: int,
    ) -> int:
        home_team_id = self._team_id(
            season_id, identifier_namespace, record.home_team_source_id
        )
        away_team_id = self._team_id(
            season_id, identifier_namespace, record.away_team_source_id
        )
        existing = self.connection.execute(
            """
            SELECT home_team_id, away_team_id
            FROM fixtures
            WHERE season_id = ? AND identifier_namespace = ? AND source_fixture_id = ?
            """,
            (season_id, identifier_namespace, record.source_fixture_id),
        ).fetchone()
        if existing is not None and (
            existing["home_team_id"] != home_team_id
            or existing["away_team_id"] != away_team_id
        ):
            raise ValueError(
                f"Contradictory fixture identity for namespace {identifier_namespace!r}, "
                f"ID {record.source_fixture_id!r}"
            )
        gameweek_id = None
        if record.gameweek_number is not None:
            gameweek_id = self._gameweek_id(season_id, record.gameweek_number)
        fixture_id = self._upsert_id(
            """
            INSERT INTO fixtures (
                season_id, identifier_namespace, source_fixture_id, gameweek_id,
                kickoff_time, home_team_id, away_team_id, home_score, away_score,
                finished, provenance_run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(season_id, identifier_namespace, source_fixture_id) DO UPDATE SET
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
                identifier_namespace,
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
        self.connection.execute(
            """
            INSERT INTO fixture_observations (
                fixture_id, gameweek_id, kickoff_time, home_score, away_score,
                finished, provenance_run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(fixture_id, provenance_run_id) DO UPDATE SET
                gameweek_id = excluded.gameweek_id,
                kickoff_time = excluded.kickoff_time,
                home_score = excluded.home_score,
                away_score = excluded.away_score,
                finished = excluded.finished
            """,
            (
                fixture_id,
                gameweek_id,
                record.kickoff_time,
                record.home_score,
                record.away_score,
                int(record.finished),
                run_id,
            ),
        )
        return fixture_id

    def upsert_fixture_stats(
        self,
        season_id: int,
        identifier_namespace: str,
        record: PlayerFixtureStatsRecord,
        run_id: int,
    ) -> int:
        player_season_id = self._player_season_id(
            season_id, identifier_namespace, record.source_player_id
        )
        fixture_id = self._fixture_id(
            season_id, identifier_namespace, record.source_fixture_id
        )
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

    def upsert_player_season_stats_observation(
        self,
        season_id: int,
        identifier_namespace: str,
        record: PlayerSeasonStatsObservationRecord,
        run_id: int,
    ) -> int:
        player_season_id = self._player_season_id(
            season_id, identifier_namespace, record.source_player_id
        )
        observed_at = _utc_timestamp(
            record.observed_at, "season stats observed_at"
        )
        return self._upsert_id(
            """
            INSERT INTO player_season_stats_observations (
                player_season_id, observed_at, minutes, starts, goals, assists,
                clean_sheets, goals_conceded, own_goals, penalties_saved,
                penalties_missed, yellow_cards, red_cards, saves, bonus, bps,
                defensive_contributions, expected_goals, expected_assists,
                expected_goal_involvements, expected_goals_conceded,
                total_points, source_observation_key, provenance_run_id
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?
            )
            ON CONFLICT(player_season_id, source_observation_key) DO UPDATE SET
                observed_at = excluded.observed_at,
                minutes = excluded.minutes,
                starts = excluded.starts,
                goals = excluded.goals,
                assists = excluded.assists,
                clean_sheets = excluded.clean_sheets,
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
            (
                player_season_id,
                observed_at,
                record.minutes,
                record.starts,
                record.goals,
                record.assists,
                record.clean_sheets,
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
                record.source_observation_key,
                run_id,
            ),
        )

    def upsert_gameweek_observation(
        self,
        season_id: int,
        identifier_namespace: str,
        source: IngestionSource,
        record: PlayerGameweekSnapshotRecord,
        run_id: int,
    ) -> int:
        record.validate_timing()
        player_season_id = self._player_season_id(
            season_id, identifier_namespace, record.source_player_id
        )
        gameweek_id = self._gameweek_id(season_id, record.gameweek_number)
        source_key = record.source_observation_key
        if source_key is None and record.observation_kind == "live_pre_deadline":
            timestamp = (
                _utc_timestamp(record.captured_at, "observation captured_at")
                if record.captured_at
                else "unknown"
            )
            source_key = _stable_hash(
                "|".join(
                    (
                        "live-capture",
                        source.name,
                        str(season_id),
                        str(gameweek_id),
                        timestamp,
                        source.content_sha256 or "",
                    )
                )
            )
        if not source_key:
            source_key = source.content_sha256
        if not source_key:
            source_key = _stable_hash(
                json.dumps(
                    {
                        "player_season_id": player_season_id,
                        "gameweek_id": gameweek_id,
                        "kind": record.observation_kind,
                        "team": record.source_team_id,
                        "price": record.price_tenths,
                        "selected_count": record.selected_count,
                        "selected_by_percent": record.selected_by_percent,
                        "transfers_in": record.transfers_in,
                        "transfers_out": record.transfers_out,
                        "status": record.status,
                        "chance": record.chance_of_playing_next_round,
                        "news": record.news,
                    },
                    sort_keys=True,
                )
            )
        team_id = (
            self._team_id(season_id, identifier_namespace, record.source_team_id)
            if record.source_team_id is not None
            else None
        )
        return self._upsert_id(
            """
            INSERT INTO player_gameweek_observations (
                player_season_id, gameweek_id, observation_kind, observed_at,
                observed_on, timing_quality, team_id, price_tenths, selected_count,
                selected_by_percent, transfers_in, transfers_out, status,
                chance_of_playing_next_round, news, source_observation_key,
                provenance_run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(
                player_season_id, gameweek_id, observation_kind, source_observation_key
            ) DO UPDATE SET
                observed_at = excluded.observed_at,
                observed_on = excluded.observed_on,
                timing_quality = excluded.timing_quality,
                team_id = excluded.team_id,
                price_tenths = excluded.price_tenths,
                selected_count = excluded.selected_count,
                selected_by_percent = excluded.selected_by_percent,
                transfers_in = excluded.transfers_in,
                transfers_out = excluded.transfers_out,
                status = excluded.status,
                chance_of_playing_next_round = excluded.chance_of_playing_next_round,
                news = excluded.news,
                provenance_run_id = excluded.provenance_run_id
            RETURNING id
            """,
            (
                player_season_id,
                gameweek_id,
                record.observation_kind,
                _utc_timestamp(record.captured_at, "observation captured_at")
                if record.captured_at
                else None,
                record.observed_on.isoformat() if record.observed_on else None,
                record.timing_quality,
                team_id,
                record.price_tenths,
                record.selected_count,
                record.selected_by_percent,
                record.transfers_in,
                record.transfers_out,
                record.status,
                record.chance_of_playing_next_round,
                record.news,
                source_key,
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
            "season_stats_observations": """
                SELECT COUNT(*) FROM player_season_stats_observations observations
                JOIN player_seasons ps ON ps.id = observations.player_season_id
                WHERE ps.season_id = ?
            """,
            "gameweek_snapshots": """
                SELECT COUNT(*) FROM player_gameweek_observations observations
                JOIN player_seasons ps ON ps.id = observations.player_season_id
                WHERE ps.season_id = ?
            """,
        }
        return {
            name: int(self.connection.execute(sql, (season_id,)).fetchone()[0])
            for name, sql in queries.items()
        }

    def player_gameweek_totals(
        self,
        season_code: str,
        source_player_id: str,
        gameweek_number: int,
        identifier_namespace: str = "official-fpl",
        observation_mode: str = "latest_available",
    ) -> sqlite3.Row | None:
        """Return fixture totals and the latest relevant Gameweek observation."""

        observation_filter = _observation_mode_filter(observation_mode)

        return self.connection.execute(
            f"""
            WITH latest_observations AS (
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
                WHERE {observation_filter}
            )
            SELECT
                ps.source_player_id,
                players.web_name,
                gameweeks.number AS gameweek,
                COALESCE(SUM(stats.minutes), 0) AS minutes,
                COALESCE(SUM(stats.total_points), 0) AS total_points,
                COALESCE(SUM(stats.expected_goals), 0.0) AS expected_goals,
                COALESCE(SUM(stats.expected_assists), 0.0) AS expected_assists,
                latest_observations.price_tenths,
                latest_observations.selected_count,
                latest_observations.selected_by_percent,
                latest_observations.status,
                latest_observations.team_id
            FROM player_seasons ps
            JOIN seasons ON seasons.id = ps.season_id
            JOIN players ON players.id = ps.player_id
            JOIN gameweeks
              ON gameweeks.season_id = seasons.id AND gameweeks.number = ?
            LEFT JOIN fixtures ON fixtures.gameweek_id = gameweeks.id
            LEFT JOIN player_fixture_stats stats
              ON stats.player_season_id = ps.id AND stats.fixture_id = fixtures.id
            LEFT JOIN latest_observations
              ON latest_observations.player_season_id = ps.id
             AND latest_observations.gameweek_id = gameweeks.id
             AND latest_observations.observation_rank = 1
            WHERE seasons.code = ?
              AND ps.identifier_namespace = ?
              AND ps.source_player_id = ?
            GROUP BY ps.id, gameweeks.id, latest_observations.id
            """,
            (gameweek_number, season_code, identifier_namespace, source_player_id),
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

    def _team_id(
        self, season_id: int, identifier_namespace: str, source_team_id: str
    ) -> int:
        return self._required_id(
            """
            SELECT id FROM teams
            WHERE season_id = ? AND identifier_namespace = ? AND source_team_id = ?
            """,
            (season_id, identifier_namespace, source_team_id),
            "team",
        )

    def _gameweek_id(self, season_id: int, number: int) -> int:
        return self._required_id(
            "SELECT id FROM gameweeks WHERE season_id = ? AND number = ?",
            (season_id, number),
            "gameweek",
        )

    def _fixture_id(
        self, season_id: int, identifier_namespace: str, source_fixture_id: str
    ) -> int:
        return self._required_id(
            """
            SELECT id FROM fixtures
            WHERE season_id = ? AND identifier_namespace = ? AND source_fixture_id = ?
            """,
            (season_id, identifier_namespace, source_fixture_id),
            "fixture",
        )

    def _player_season_id(
        self, season_id: int, identifier_namespace: str, source_player_id: str
    ) -> int:
        return self._required_id(
            """
            SELECT id FROM player_seasons
            WHERE season_id = ? AND identifier_namespace = ? AND source_player_id = ?
            """,
            (season_id, identifier_namespace, source_player_id),
            "player season",
        )
