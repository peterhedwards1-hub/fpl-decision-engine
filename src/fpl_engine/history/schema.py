"""SQLite schema and migrations for historical FPL data."""

SCHEMA_VERSION = 3

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS seasons (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    starts_on TEXT,
    ends_on TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ingestion_runs (
    id INTEGER PRIMARY KEY,
    source_name TEXT NOT NULL,
    identifier_namespace TEXT NOT NULL DEFAULT 'official-fpl',
    source_url TEXT,
    retrieved_at TEXT NOT NULL,
    content_sha256 TEXT,
    source_revision TEXT,
    adapter_version TEXT,
    status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
    row_count INTEGER NOT NULL DEFAULT 0 CHECK (row_count >= 0),
    error_message TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS players (
    id INTEGER PRIMARY KEY,
    first_name TEXT NOT NULL DEFAULT '',
    second_name TEXT NOT NULL DEFAULT '',
    web_name TEXT NOT NULL,
    date_of_birth TEXT,
    provenance_run_id INTEGER REFERENCES ingestion_runs(id)
);

CREATE TABLE IF NOT EXISTS player_identifiers (
    id INTEGER PRIMARY KEY,
    player_id INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    identifier_type TEXT NOT NULL,
    identifier_value TEXT NOT NULL,
    provenance_run_id INTEGER REFERENCES ingestion_runs(id),
    CHECK (identifier_type IN ('official_fpl_code', 'opta_code')),
    UNIQUE (identifier_type, identifier_value),
    UNIQUE (player_id, identifier_type)
);

CREATE TABLE IF NOT EXISTS teams (
    id INTEGER PRIMARY KEY,
    season_id INTEGER NOT NULL REFERENCES seasons(id) ON DELETE CASCADE,
    identifier_namespace TEXT NOT NULL,
    source_team_id TEXT NOT NULL,
    name TEXT NOT NULL,
    short_name TEXT NOT NULL,
    provenance_run_id INTEGER NOT NULL REFERENCES ingestion_runs(id),
    UNIQUE (season_id, identifier_namespace, source_team_id)
);

CREATE TABLE IF NOT EXISTS player_seasons (
    id INTEGER PRIMARY KEY,
    season_id INTEGER NOT NULL REFERENCES seasons(id) ON DELETE CASCADE,
    player_id INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    identifier_namespace TEXT NOT NULL,
    source_player_id TEXT NOT NULL,
    team_id INTEGER NOT NULL REFERENCES teams(id),
    position TEXT NOT NULL CHECK (position IN ('GK', 'DEF', 'MID', 'FWD')),
    start_price_tenths INTEGER CHECK (start_price_tenths >= 0),
    end_price_tenths INTEGER CHECK (end_price_tenths >= 0),
    provenance_run_id INTEGER NOT NULL REFERENCES ingestion_runs(id),
    UNIQUE (season_id, identifier_namespace, source_player_id)
);

CREATE TABLE IF NOT EXISTS gameweeks (
    id INTEGER PRIMARY KEY,
    season_id INTEGER NOT NULL REFERENCES seasons(id) ON DELETE CASCADE,
    number INTEGER NOT NULL CHECK (number BETWEEN 1 AND 60),
    deadline_time TEXT,
    is_finished INTEGER NOT NULL DEFAULT 0 CHECK (is_finished IN (0, 1)),
    provenance_run_id INTEGER NOT NULL REFERENCES ingestion_runs(id),
    UNIQUE (season_id, number)
);

CREATE TABLE IF NOT EXISTS fixtures (
    id INTEGER PRIMARY KEY,
    season_id INTEGER NOT NULL REFERENCES seasons(id) ON DELETE CASCADE,
    identifier_namespace TEXT NOT NULL,
    source_fixture_id TEXT NOT NULL,
    gameweek_id INTEGER REFERENCES gameweeks(id),
    kickoff_time TEXT,
    home_team_id INTEGER NOT NULL REFERENCES teams(id),
    away_team_id INTEGER NOT NULL REFERENCES teams(id),
    home_score INTEGER CHECK (home_score IS NULL OR home_score >= 0),
    away_score INTEGER CHECK (away_score IS NULL OR away_score >= 0),
    finished INTEGER NOT NULL DEFAULT 0 CHECK (finished IN (0, 1)),
    provenance_run_id INTEGER NOT NULL REFERENCES ingestion_runs(id),
    CHECK (home_team_id <> away_team_id),
    UNIQUE (season_id, identifier_namespace, source_fixture_id)
);

CREATE TABLE IF NOT EXISTS player_fixture_stats (
    id INTEGER PRIMARY KEY,
    player_season_id INTEGER NOT NULL REFERENCES player_seasons(id) ON DELETE CASCADE,
    fixture_id INTEGER NOT NULL REFERENCES fixtures(id) ON DELETE CASCADE,
    minutes INTEGER NOT NULL DEFAULT 0 CHECK (minutes BETWEEN 0 AND 180),
    starts INTEGER NOT NULL DEFAULT 0 CHECK (starts IN (0, 1)),
    goals INTEGER NOT NULL DEFAULT 0 CHECK (goals >= 0),
    assists INTEGER NOT NULL DEFAULT 0 CHECK (assists >= 0),
    clean_sheet INTEGER NOT NULL DEFAULT 0 CHECK (clean_sheet IN (0, 1)),
    goals_conceded INTEGER NOT NULL DEFAULT 0 CHECK (goals_conceded >= 0),
    own_goals INTEGER NOT NULL DEFAULT 0 CHECK (own_goals >= 0),
    penalties_saved INTEGER NOT NULL DEFAULT 0 CHECK (penalties_saved >= 0),
    penalties_missed INTEGER NOT NULL DEFAULT 0 CHECK (penalties_missed >= 0),
    yellow_cards INTEGER NOT NULL DEFAULT 0 CHECK (yellow_cards >= 0),
    red_cards INTEGER NOT NULL DEFAULT 0 CHECK (red_cards >= 0),
    saves INTEGER NOT NULL DEFAULT 0 CHECK (saves >= 0),
    bonus INTEGER NOT NULL DEFAULT 0 CHECK (bonus >= 0),
    bps INTEGER NOT NULL DEFAULT 0,
    defensive_contributions INTEGER NOT NULL DEFAULT 0 CHECK (defensive_contributions >= 0),
    expected_goals REAL,
    expected_assists REAL,
    expected_goal_involvements REAL,
    expected_goals_conceded REAL,
    total_points INTEGER NOT NULL DEFAULT 0,
    provenance_run_id INTEGER NOT NULL REFERENCES ingestion_runs(id),
    UNIQUE (player_season_id, fixture_id)
);

CREATE TABLE IF NOT EXISTS player_gameweek_observations (
    id INTEGER PRIMARY KEY,
    player_season_id INTEGER NOT NULL REFERENCES player_seasons(id) ON DELETE CASCADE,
    gameweek_id INTEGER NOT NULL REFERENCES gameweeks(id) ON DELETE CASCADE,
    observation_kind TEXT NOT NULL CHECK (
        observation_kind IN ('live_pre_deadline', 'post_gameweek', 'historical_reconstruction')
    ),
    observed_at TEXT,
    timing_quality TEXT NOT NULL CHECK (
        timing_quality IN ('exact', 'date_only', 'unknown')
    ),
    team_id INTEGER REFERENCES teams(id),
    price_tenths INTEGER NOT NULL CHECK (price_tenths >= 0),
    selected_count INTEGER CHECK (selected_count IS NULL OR selected_count >= 0),
    selected_by_percent REAL CHECK (
        selected_by_percent IS NULL OR selected_by_percent BETWEEN 0 AND 100
    ),
    transfers_in INTEGER CHECK (transfers_in IS NULL OR transfers_in >= 0),
    transfers_out INTEGER CHECK (transfers_out IS NULL OR transfers_out >= 0),
    status TEXT,
    chance_of_playing_next_round INTEGER CHECK (
        chance_of_playing_next_round IS NULL
        OR chance_of_playing_next_round BETWEEN 0 AND 100
    ),
    news TEXT,
    source_observation_key TEXT NOT NULL,
    provenance_run_id INTEGER NOT NULL REFERENCES ingestion_runs(id),
    UNIQUE (
        player_season_id, gameweek_id, observation_kind, source_observation_key
    )
);

CREATE INDEX IF NOT EXISTS idx_player_seasons_team ON player_seasons(team_id);
CREATE INDEX IF NOT EXISTS idx_player_seasons_source
    ON player_seasons(identifier_namespace, source_player_id);
CREATE INDEX IF NOT EXISTS idx_fixtures_gameweek ON fixtures(gameweek_id);
CREATE INDEX IF NOT EXISTS idx_fixture_stats_fixture ON player_fixture_stats(fixture_id);
CREATE INDEX IF NOT EXISTS idx_observations_gameweek
    ON player_gameweek_observations(gameweek_id, observed_at);

CREATE VIEW IF NOT EXISTS player_gameweek_snapshots AS
SELECT
    id,
    player_season_id,
    gameweek_id,
    team_id,
    price_tenths,
    selected_count,
    selected_by_percent,
    transfers_in,
    transfers_out,
    status,
    chance_of_playing_next_round,
    news,
    observed_at AS captured_at,
    provenance_run_id
FROM player_gameweek_observations;
"""

# This is deliberately kept in the application so the migration is testable against
# the exact schema shipped by the previous milestone. It preserves row IDs while
# rebuilding the tables whose uniqueness and identity semantics changed.
MIGRATE_V2_TO_V3_SQL = """
PRAGMA foreign_keys = OFF;
BEGIN;

DROP INDEX IF EXISTS idx_player_seasons_team;
DROP INDEX IF EXISTS idx_fixtures_gameweek;
DROP INDEX IF EXISTS idx_fixture_stats_fixture;
DROP INDEX IF EXISTS idx_snapshots_gameweek;

ALTER TABLE seasons RENAME TO _v2_seasons;
ALTER TABLE ingestion_runs RENAME TO _v2_ingestion_runs;
ALTER TABLE teams RENAME TO _v2_teams;
ALTER TABLE players RENAME TO _v2_players;
ALTER TABLE player_seasons RENAME TO _v2_player_seasons;
ALTER TABLE gameweeks RENAME TO _v2_gameweeks;
ALTER TABLE fixtures RENAME TO _v2_fixtures;
ALTER TABLE player_fixture_stats RENAME TO _v2_player_fixture_stats;
ALTER TABLE player_gameweek_snapshots RENAME TO _v2_player_gameweek_snapshots;

CREATE TABLE seasons (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    starts_on TEXT,
    ends_on TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE ingestion_runs (
    id INTEGER PRIMARY KEY,
    source_name TEXT NOT NULL,
    identifier_namespace TEXT NOT NULL DEFAULT 'official-fpl',
    source_url TEXT,
    retrieved_at TEXT NOT NULL,
    content_sha256 TEXT,
    source_revision TEXT,
    adapter_version TEXT,
    status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
    row_count INTEGER NOT NULL DEFAULT 0 CHECK (row_count >= 0),
    error_message TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE players (
    id INTEGER PRIMARY KEY,
    first_name TEXT NOT NULL DEFAULT '',
    second_name TEXT NOT NULL DEFAULT '',
    web_name TEXT NOT NULL,
    date_of_birth TEXT,
    provenance_run_id INTEGER REFERENCES ingestion_runs(id)
);
CREATE TABLE player_identifiers (
    id INTEGER PRIMARY KEY,
    player_id INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    identifier_type TEXT NOT NULL,
    identifier_value TEXT NOT NULL,
    provenance_run_id INTEGER REFERENCES ingestion_runs(id),
    CHECK (identifier_type IN ('official_fpl_code', 'opta_code')),
    UNIQUE (identifier_type, identifier_value),
    UNIQUE (player_id, identifier_type)
);
CREATE TABLE teams (
    id INTEGER PRIMARY KEY,
    season_id INTEGER NOT NULL REFERENCES seasons(id) ON DELETE CASCADE,
    identifier_namespace TEXT NOT NULL,
    source_team_id TEXT NOT NULL,
    name TEXT NOT NULL,
    short_name TEXT NOT NULL,
    provenance_run_id INTEGER NOT NULL REFERENCES ingestion_runs(id),
    UNIQUE (season_id, identifier_namespace, source_team_id)
);
CREATE TABLE player_seasons (
    id INTEGER PRIMARY KEY,
    season_id INTEGER NOT NULL REFERENCES seasons(id) ON DELETE CASCADE,
    player_id INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    identifier_namespace TEXT NOT NULL,
    source_player_id TEXT NOT NULL,
    team_id INTEGER NOT NULL REFERENCES teams(id),
    position TEXT NOT NULL CHECK (position IN ('GK', 'DEF', 'MID', 'FWD')),
    start_price_tenths INTEGER CHECK (start_price_tenths >= 0),
    end_price_tenths INTEGER CHECK (end_price_tenths >= 0),
    provenance_run_id INTEGER NOT NULL REFERENCES ingestion_runs(id),
    UNIQUE (season_id, identifier_namespace, source_player_id)
);
CREATE TABLE gameweeks (
    id INTEGER PRIMARY KEY,
    season_id INTEGER NOT NULL REFERENCES seasons(id) ON DELETE CASCADE,
    number INTEGER NOT NULL CHECK (number BETWEEN 1 AND 60),
    deadline_time TEXT,
    is_finished INTEGER NOT NULL DEFAULT 0 CHECK (is_finished IN (0, 1)),
    provenance_run_id INTEGER NOT NULL REFERENCES ingestion_runs(id),
    UNIQUE (season_id, number)
);
CREATE TABLE fixtures (
    id INTEGER PRIMARY KEY,
    season_id INTEGER NOT NULL REFERENCES seasons(id) ON DELETE CASCADE,
    identifier_namespace TEXT NOT NULL,
    source_fixture_id TEXT NOT NULL,
    gameweek_id INTEGER REFERENCES gameweeks(id),
    kickoff_time TEXT,
    home_team_id INTEGER NOT NULL REFERENCES teams(id),
    away_team_id INTEGER NOT NULL REFERENCES teams(id),
    home_score INTEGER CHECK (home_score IS NULL OR home_score >= 0),
    away_score INTEGER CHECK (away_score IS NULL OR away_score >= 0),
    finished INTEGER NOT NULL DEFAULT 0 CHECK (finished IN (0, 1)),
    provenance_run_id INTEGER NOT NULL REFERENCES ingestion_runs(id),
    CHECK (home_team_id <> away_team_id),
    UNIQUE (season_id, identifier_namespace, source_fixture_id)
);
CREATE TABLE player_fixture_stats (
    id INTEGER PRIMARY KEY,
    player_season_id INTEGER NOT NULL REFERENCES player_seasons(id) ON DELETE CASCADE,
    fixture_id INTEGER NOT NULL REFERENCES fixtures(id) ON DELETE CASCADE,
    minutes INTEGER NOT NULL DEFAULT 0 CHECK (minutes BETWEEN 0 AND 180),
    starts INTEGER NOT NULL DEFAULT 0 CHECK (starts IN (0, 1)),
    goals INTEGER NOT NULL DEFAULT 0 CHECK (goals >= 0),
    assists INTEGER NOT NULL DEFAULT 0 CHECK (assists >= 0),
    clean_sheet INTEGER NOT NULL DEFAULT 0 CHECK (clean_sheet IN (0, 1)),
    goals_conceded INTEGER NOT NULL DEFAULT 0 CHECK (goals_conceded >= 0),
    own_goals INTEGER NOT NULL DEFAULT 0 CHECK (own_goals >= 0),
    penalties_saved INTEGER NOT NULL DEFAULT 0 CHECK (penalties_saved >= 0),
    penalties_missed INTEGER NOT NULL DEFAULT 0 CHECK (penalties_missed >= 0),
    yellow_cards INTEGER NOT NULL DEFAULT 0 CHECK (yellow_cards >= 0),
    red_cards INTEGER NOT NULL DEFAULT 0 CHECK (red_cards >= 0),
    saves INTEGER NOT NULL DEFAULT 0 CHECK (saves >= 0),
    bonus INTEGER NOT NULL DEFAULT 0 CHECK (bonus >= 0),
    bps INTEGER NOT NULL DEFAULT 0,
    defensive_contributions INTEGER NOT NULL DEFAULT 0 CHECK (defensive_contributions >= 0),
    expected_goals REAL,
    expected_assists REAL,
    expected_goal_involvements REAL,
    expected_goals_conceded REAL,
    total_points INTEGER NOT NULL DEFAULT 0,
    provenance_run_id INTEGER NOT NULL REFERENCES ingestion_runs(id),
    UNIQUE (player_season_id, fixture_id)
);
CREATE TABLE player_gameweek_observations (
    id INTEGER PRIMARY KEY,
    player_season_id INTEGER NOT NULL REFERENCES player_seasons(id) ON DELETE CASCADE,
    gameweek_id INTEGER NOT NULL REFERENCES gameweeks(id) ON DELETE CASCADE,
    observation_kind TEXT NOT NULL CHECK (
        observation_kind IN ('live_pre_deadline', 'post_gameweek', 'historical_reconstruction')
    ),
    observed_at TEXT,
    timing_quality TEXT NOT NULL CHECK (
        timing_quality IN ('exact', 'date_only', 'unknown')
    ),
    team_id INTEGER REFERENCES teams(id),
    price_tenths INTEGER NOT NULL CHECK (price_tenths >= 0),
    selected_count INTEGER CHECK (selected_count IS NULL OR selected_count >= 0),
    selected_by_percent REAL CHECK (
        selected_by_percent IS NULL OR selected_by_percent BETWEEN 0 AND 100
    ),
    transfers_in INTEGER CHECK (transfers_in IS NULL OR transfers_in >= 0),
    transfers_out INTEGER CHECK (transfers_out IS NULL OR transfers_out >= 0),
    status TEXT,
    chance_of_playing_next_round INTEGER CHECK (
        chance_of_playing_next_round IS NULL
        OR chance_of_playing_next_round BETWEEN 0 AND 100
    ),
    news TEXT,
    source_observation_key TEXT NOT NULL,
    provenance_run_id INTEGER NOT NULL REFERENCES ingestion_runs(id),
    UNIQUE (player_season_id, gameweek_id, observation_kind, source_observation_key)
);
CREATE INDEX idx_player_seasons_team ON player_seasons(team_id);
CREATE INDEX idx_player_seasons_source
    ON player_seasons(identifier_namespace, source_player_id);
CREATE INDEX idx_fixtures_gameweek ON fixtures(gameweek_id);
CREATE INDEX idx_fixture_stats_fixture ON player_fixture_stats(fixture_id);
CREATE INDEX idx_observations_gameweek
    ON player_gameweek_observations(gameweek_id, observed_at);

INSERT INTO seasons SELECT * FROM _v2_seasons;
INSERT INTO ingestion_runs (
    id, source_name, identifier_namespace, source_url, retrieved_at,
    content_sha256, source_revision, adapter_version, status, row_count,
    error_message, created_at
)
SELECT id, source_name, 'official-fpl', source_url, retrieved_at,
       content_sha256, NULL, NULL, status, row_count, error_message, created_at
FROM _v2_ingestion_runs;
INSERT INTO players (
    id, first_name, second_name, web_name, date_of_birth, provenance_run_id
)
SELECT id, first_name, second_name, web_name, date_of_birth, NULL
FROM _v2_players;
INSERT INTO teams (
    id, season_id, identifier_namespace, source_team_id, name, short_name,
    provenance_run_id
)
SELECT id, season_id, 'official-fpl', source_team_id, name, short_name,
       provenance_run_id
FROM _v2_teams;
INSERT INTO player_seasons (
    id, season_id, player_id, identifier_namespace, source_player_id, team_id,
    position, start_price_tenths, end_price_tenths, provenance_run_id
)
SELECT ps.id, ps.season_id, ps.player_id, 'official-fpl', p.source_player_id,
       ps.team_id, ps.position, ps.start_price_tenths, ps.end_price_tenths,
       ps.provenance_run_id
FROM _v2_player_seasons ps
JOIN _v2_players p ON p.id = ps.player_id;
INSERT INTO gameweeks SELECT * FROM _v2_gameweeks;
INSERT INTO fixtures (
    id, season_id, identifier_namespace, source_fixture_id, gameweek_id,
    kickoff_time, home_team_id, away_team_id, home_score, away_score,
    finished, provenance_run_id
)
SELECT id, season_id, 'official-fpl', source_fixture_id, gameweek_id,
       kickoff_time, home_team_id, away_team_id, home_score, away_score,
       finished, provenance_run_id
FROM _v2_fixtures;
INSERT INTO player_fixture_stats SELECT * FROM _v2_player_fixture_stats;
INSERT INTO player_gameweek_observations (
    id, player_season_id, gameweek_id, observation_kind, observed_at,
    timing_quality, team_id, price_tenths, selected_count, selected_by_percent,
    transfers_in, transfers_out, status, chance_of_playing_next_round, news,
    source_observation_key, provenance_run_id
)
SELECT id, player_season_id, gameweek_id, 'live_pre_deadline', captured_at,
       'exact', team_id, price_tenths, NULL, selected_by_percent,
       transfers_in, transfers_out, status, chance_of_playing_next_round, news,
       'legacy-v2-' || id, provenance_run_id
FROM _v2_player_gameweek_snapshots;

DROP TABLE _v2_player_gameweek_snapshots;
DROP TABLE _v2_player_fixture_stats;
DROP TABLE _v2_fixtures;
DROP TABLE _v2_gameweeks;
DROP TABLE _v2_player_seasons;
DROP TABLE _v2_teams;
DROP TABLE _v2_players;
DROP TABLE _v2_ingestion_runs;
DROP TABLE _v2_seasons;

CREATE VIEW player_gameweek_snapshots AS
SELECT id, player_season_id, gameweek_id, team_id, price_tenths,
       selected_count, selected_by_percent, transfers_in, transfers_out,
       status, chance_of_playing_next_round, news,
       observed_at AS captured_at, provenance_run_id
FROM player_gameweek_observations;

PRAGMA user_version = 3;
"""
