"""SQLite schema and migrations for historical FPL data."""

SCHEMA_VERSION = 8

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

CREATE TABLE IF NOT EXISTS fixture_observations (
    id INTEGER PRIMARY KEY,
    fixture_id INTEGER NOT NULL REFERENCES fixtures(id) ON DELETE CASCADE,
    gameweek_id INTEGER REFERENCES gameweeks(id),
    kickoff_time TEXT,
    home_score INTEGER CHECK (home_score IS NULL OR home_score >= 0),
    away_score INTEGER CHECK (away_score IS NULL OR away_score >= 0),
    finished INTEGER NOT NULL DEFAULT 0 CHECK (finished IN (0, 1)),
    provenance_run_id INTEGER NOT NULL REFERENCES ingestion_runs(id),
    UNIQUE (fixture_id, provenance_run_id)
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

CREATE TABLE IF NOT EXISTS player_season_stats_observations (
    id INTEGER PRIMARY KEY,
    player_season_id INTEGER NOT NULL REFERENCES player_seasons(id) ON DELETE CASCADE,
    observed_at TEXT NOT NULL,
    minutes INTEGER NOT NULL DEFAULT 0 CHECK (minutes >= 0),
    starts INTEGER NOT NULL DEFAULT 0 CHECK (starts >= 0),
    goals INTEGER NOT NULL DEFAULT 0 CHECK (goals >= 0),
    assists INTEGER NOT NULL DEFAULT 0 CHECK (assists >= 0),
    clean_sheets INTEGER NOT NULL DEFAULT 0 CHECK (clean_sheets >= 0),
    goals_conceded INTEGER NOT NULL DEFAULT 0 CHECK (goals_conceded >= 0),
    own_goals INTEGER NOT NULL DEFAULT 0 CHECK (own_goals >= 0),
    penalties_saved INTEGER NOT NULL DEFAULT 0 CHECK (penalties_saved >= 0),
    penalties_missed INTEGER NOT NULL DEFAULT 0 CHECK (penalties_missed >= 0),
    yellow_cards INTEGER NOT NULL DEFAULT 0 CHECK (yellow_cards >= 0),
    red_cards INTEGER NOT NULL DEFAULT 0 CHECK (red_cards >= 0),
    saves INTEGER NOT NULL DEFAULT 0 CHECK (saves >= 0),
    bonus INTEGER NOT NULL DEFAULT 0 CHECK (bonus >= 0),
    bps INTEGER NOT NULL DEFAULT 0,
    defensive_contributions INTEGER NOT NULL DEFAULT 0 CHECK (
        defensive_contributions >= 0
    ),
    expected_goals REAL,
    expected_assists REAL,
    expected_goal_involvements REAL,
    expected_goals_conceded REAL,
    total_points INTEGER NOT NULL DEFAULT 0,
    source_observation_key TEXT NOT NULL,
    provenance_run_id INTEGER NOT NULL REFERENCES ingestion_runs(id),
    UNIQUE (player_season_id, source_observation_key)
);

CREATE TABLE IF NOT EXISTS player_gameweek_observations (
    id INTEGER PRIMARY KEY,
    player_season_id INTEGER NOT NULL REFERENCES player_seasons(id) ON DELETE CASCADE,
    gameweek_id INTEGER NOT NULL REFERENCES gameweeks(id) ON DELETE CASCADE,
    observation_kind TEXT NOT NULL CHECK (
        observation_kind IN ('live_pre_deadline', 'post_gameweek', 'historical_reconstruction')
    ),
    observed_at TEXT,
    observed_on TEXT,
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
    CHECK (
        (timing_quality = 'exact' AND observed_at IS NOT NULL AND observed_on IS NULL)
        OR (timing_quality = 'date_only' AND observed_at IS NULL
            AND observed_on GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]')
        OR (timing_quality = 'unknown' AND observed_at IS NULL AND observed_on IS NULL)
    ),
    UNIQUE (
        player_season_id, gameweek_id, observation_kind, source_observation_key
    )
);

CREATE TABLE IF NOT EXISTS manager_snapshots (
    id INTEGER PRIMARY KEY,
    season_id INTEGER NOT NULL REFERENCES seasons(id) ON DELETE CASCADE,
    gameweek_id INTEGER NOT NULL REFERENCES gameweeks(id),
    data_ingestion_run_id INTEGER REFERENCES ingestion_runs(id),
    captured_at TEXT NOT NULL,
    bank_tenths INTEGER NOT NULL CHECK (bank_tenths >= 0),
    free_transfers INTEGER NOT NULL CHECK (free_transfers BETWEEN 0 AND 5),
    remaining_chips_json TEXT NOT NULL,
    captain_player_season_id INTEGER REFERENCES player_seasons(id),
    vice_captain_player_season_id INTEGER REFERENCES player_seasons(id),
    note TEXT,
    CHECK (
        captain_player_season_id IS NULL
        OR vice_captain_player_season_id IS NULL
        OR captain_player_season_id <> vice_captain_player_season_id
    )
);

CREATE TABLE IF NOT EXISTS manager_squad_entries (
    id INTEGER PRIMARY KEY,
    manager_snapshot_id INTEGER NOT NULL
        REFERENCES manager_snapshots(id) ON DELETE CASCADE,
    player_season_id INTEGER NOT NULL REFERENCES player_seasons(id),
    purchase_price_tenths INTEGER NOT NULL CHECK (purchase_price_tenths >= 0),
    selling_price_tenths INTEGER NOT NULL CHECK (selling_price_tenths >= 0),
    is_starter INTEGER NOT NULL CHECK (is_starter IN (0, 1)),
    bench_order INTEGER CHECK (
        (is_starter = 1 AND bench_order IS NULL)
        OR (is_starter = 0 AND bench_order BETWEEN 1 AND 4)
    ),
    UNIQUE (manager_snapshot_id, player_season_id),
    UNIQUE (manager_snapshot_id, bench_order)
);

CREATE TABLE IF NOT EXISTS projection_runs (
    id INTEGER PRIMARY KEY,
    season_id INTEGER NOT NULL REFERENCES seasons(id) ON DELETE CASCADE,
    generated_at TEXT NOT NULL,
    start_gameweek INTEGER NOT NULL CHECK (start_gameweek BETWEEN 1 AND 38),
    horizon_gameweeks INTEGER NOT NULL CHECK (horizon_gameweeks > 0),
    model_version TEXT NOT NULL,
    observation_mode TEXT NOT NULL,
    assumptions_json TEXT NOT NULL,
    source_ingestion_run_id INTEGER REFERENCES ingestion_runs(id)
);

CREATE TABLE IF NOT EXISTS player_gameweek_projections (
    id INTEGER PRIMARY KEY,
    projection_run_id INTEGER NOT NULL REFERENCES projection_runs(id) ON DELETE CASCADE,
    player_season_id INTEGER NOT NULL REFERENCES player_seasons(id) ON DELETE CASCADE,
    gameweek_number INTEGER NOT NULL CHECK (gameweek_number BETWEEN 1 AND 38),
    expected_minutes REAL NOT NULL CHECK (expected_minutes BETWEEN 0 AND 180),
    appearance_points REAL NOT NULL,
    goal_points REAL NOT NULL,
    assist_points REAL NOT NULL,
    clean_sheet_points REAL NOT NULL,
    save_points REAL NOT NULL,
    defensive_contribution_points REAL NOT NULL,
    bonus_points REAL NOT NULL,
    deduction_points REAL NOT NULL,
    expected_points REAL NOT NULL,
    uncertainty REAL NOT NULL CHECK (uncertainty >= 0),
    assumptions_json TEXT NOT NULL,
    override_rationale TEXT,
    UNIQUE (projection_run_id, player_season_id, gameweek_number)
);

CREATE TABLE IF NOT EXISTS news_evidence (
    id INTEGER PRIMARY KEY,
    season_id INTEGER NOT NULL REFERENCES seasons(id) ON DELETE CASCADE,
    gameweek_id INTEGER NOT NULL REFERENCES gameweeks(id),
    player_season_id INTEGER REFERENCES player_seasons(id),
    evidence_type TEXT NOT NULL,
    summary TEXT NOT NULL,
    source_url TEXT,
    evidence_at TEXT NOT NULL,
    confidence TEXT NOT NULL CHECK (confidence IN ('low', 'medium', 'high')),
    review_status TEXT NOT NULL CHECK (
        review_status IN ('pending', 'accepted', 'rejected')
    ),
    expected_minutes_adjustment REAL,
    rationale TEXT
);

CREATE TABLE IF NOT EXISTS weekly_decision_runs (
    id INTEGER PRIMARY KEY,
    season_id INTEGER NOT NULL REFERENCES seasons(id) ON DELETE CASCADE,
    gameweek_id INTEGER NOT NULL REFERENCES gameweeks(id),
    manager_snapshot_id INTEGER NOT NULL REFERENCES manager_snapshots(id),
    projection_run_id INTEGER NOT NULL REFERENCES projection_runs(id),
    mode TEXT NOT NULL CHECK (mode IN ('provisional', 'final')),
    created_at TEXT NOT NULL,
    frozen_at TEXT,
    recommendation_json TEXT NOT NULL,
    decision_triggers_json TEXT NOT NULL,
    overrides_json TEXT NOT NULL,
    CHECK (
        (mode = 'provisional' AND frozen_at IS NULL)
        OR (mode = 'final' AND frozen_at IS NOT NULL)
    )
);

CREATE TABLE IF NOT EXISTS actual_actions (
    id INTEGER PRIMARY KEY,
    weekly_decision_run_id INTEGER NOT NULL UNIQUE
        REFERENCES weekly_decision_runs(id) ON DELETE CASCADE,
    recorded_at TEXT NOT NULL,
    action_json TEXT NOT NULL,
    followed_recommendation INTEGER NOT NULL CHECK (
        followed_recommendation IN (0, 1)
    ),
    deviation_reason TEXT
);

CREATE TABLE IF NOT EXISTS weekly_evaluations (
    id INTEGER PRIMARY KEY,
    weekly_decision_run_id INTEGER NOT NULL UNIQUE
        REFERENCES weekly_decision_runs(id) ON DELETE CASCADE,
    evaluated_at TEXT NOT NULL,
    forecast_points REAL NOT NULL,
    realised_points REAL NOT NULL,
    score_error REAL NOT NULL,
    review_notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_player_seasons_team ON player_seasons(team_id);
CREATE INDEX IF NOT EXISTS idx_player_seasons_source
    ON player_seasons(identifier_namespace, source_player_id);
CREATE INDEX IF NOT EXISTS idx_fixtures_gameweek ON fixtures(gameweek_id);
CREATE INDEX IF NOT EXISTS idx_fixture_observations_fixture
    ON fixture_observations(fixture_id, provenance_run_id);
CREATE INDEX IF NOT EXISTS idx_fixture_stats_fixture ON player_fixture_stats(fixture_id);
CREATE INDEX IF NOT EXISTS idx_season_stats_player_time
    ON player_season_stats_observations(player_season_id, observed_at);
CREATE INDEX IF NOT EXISTS idx_observations_gameweek
    ON player_gameweek_observations(gameweek_id, observed_at, observed_on);
CREATE INDEX IF NOT EXISTS idx_manager_snapshots_gameweek
    ON manager_snapshots(season_id, gameweek_id, captured_at);
CREATE INDEX IF NOT EXISTS idx_manager_entries_snapshot
    ON manager_squad_entries(manager_snapshot_id);
CREATE INDEX IF NOT EXISTS idx_projection_runs_season
    ON projection_runs(season_id, generated_at);
CREATE INDEX IF NOT EXISTS idx_projections_run_gameweek
    ON player_gameweek_projections(projection_run_id, gameweek_number);
CREATE INDEX IF NOT EXISTS idx_news_review_queue
    ON news_evidence(season_id, gameweek_id, review_status);
CREATE INDEX IF NOT EXISTS idx_weekly_runs_gameweek
    ON weekly_decision_runs(season_id, gameweek_id, created_at);

CREATE TRIGGER IF NOT EXISTS prevent_final_weekly_run_update
BEFORE UPDATE ON weekly_decision_runs
WHEN OLD.mode = 'final'
BEGIN
    SELECT RAISE(ABORT, 'final weekly decision runs are immutable');
END;

CREATE TRIGGER IF NOT EXISTS prevent_final_weekly_run_delete
BEFORE DELETE ON weekly_decision_runs
WHEN OLD.mode = 'final'
BEGIN
    SELECT RAISE(ABORT, 'final weekly decision runs are immutable');
END;

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
    observed_on,
    timing_quality,
    observation_kind,
    source_observation_key,
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

MIGRATE_V3_TO_V4_SQL = """
PRAGMA foreign_keys = OFF;
BEGIN;

DROP VIEW IF EXISTS player_gameweek_snapshots;
DROP INDEX IF EXISTS idx_observations_gameweek;
ALTER TABLE player_gameweek_observations RENAME TO _v3_player_gameweek_observations;

CREATE TABLE player_gameweek_observations (
    id INTEGER PRIMARY KEY,
    player_season_id INTEGER NOT NULL REFERENCES player_seasons(id) ON DELETE CASCADE,
    gameweek_id INTEGER NOT NULL REFERENCES gameweeks(id) ON DELETE CASCADE,
    observation_kind TEXT NOT NULL CHECK (
        observation_kind IN ('live_pre_deadline', 'post_gameweek', 'historical_reconstruction')
    ),
    observed_at TEXT,
    observed_on TEXT,
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
    CHECK (
        (timing_quality = 'exact' AND observed_at IS NOT NULL AND observed_on IS NULL)
        OR (timing_quality = 'date_only' AND observed_at IS NULL
            AND observed_on GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]')
        OR (timing_quality = 'unknown' AND observed_at IS NULL AND observed_on IS NULL)
    ),
    UNIQUE (player_season_id, gameweek_id, observation_kind, source_observation_key)
);

CREATE INDEX idx_observations_gameweek
    ON player_gameweek_observations(gameweek_id, observed_at, observed_on);

INSERT INTO player_gameweek_observations (
    id, player_season_id, gameweek_id, observation_kind, observed_at, observed_on,
    timing_quality, team_id, price_tenths, selected_count, selected_by_percent,
    transfers_in, transfers_out, status, chance_of_playing_next_round, news,
    source_observation_key, provenance_run_id
)
SELECT id, player_season_id, gameweek_id, observation_kind,
       CASE WHEN timing_quality = 'exact' THEN observed_at ELSE NULL END,
       CASE WHEN timing_quality = 'date_only' THEN substr(observed_at, 1, 10) ELSE NULL END,
       CASE
           WHEN timing_quality = 'exact' AND observed_at IS NOT NULL THEN 'exact'
           WHEN timing_quality = 'date_only' AND observed_at IS NOT NULL THEN 'date_only'
           ELSE 'unknown'
       END,
       team_id, price_tenths, selected_count, selected_by_percent,
       transfers_in, transfers_out, status, chance_of_playing_next_round, news,
       source_observation_key, provenance_run_id
FROM _v3_player_gameweek_observations;

DROP TABLE _v3_player_gameweek_observations;

CREATE VIEW player_gameweek_snapshots AS
SELECT id, player_season_id, gameweek_id, team_id, price_tenths,
       selected_count, selected_by_percent, transfers_in, transfers_out,
       status, chance_of_playing_next_round, news,
       observed_at AS captured_at, observed_on, timing_quality,
       observation_kind, source_observation_key, provenance_run_id
FROM player_gameweek_observations;

PRAGMA user_version = 4;
"""

MIGRATE_V4_TO_V5_SQL = """
PRAGMA foreign_keys = ON;
BEGIN;

CREATE TABLE fixture_observations (
    id INTEGER PRIMARY KEY,
    fixture_id INTEGER NOT NULL REFERENCES fixtures(id) ON DELETE CASCADE,
    gameweek_id INTEGER REFERENCES gameweeks(id),
    kickoff_time TEXT,
    home_score INTEGER CHECK (home_score IS NULL OR home_score >= 0),
    away_score INTEGER CHECK (away_score IS NULL OR away_score >= 0),
    finished INTEGER NOT NULL DEFAULT 0 CHECK (finished IN (0, 1)),
    provenance_run_id INTEGER NOT NULL REFERENCES ingestion_runs(id),
    UNIQUE (fixture_id, provenance_run_id)
);

CREATE INDEX idx_fixture_observations_fixture
    ON fixture_observations(fixture_id, provenance_run_id);

CREATE TABLE player_season_stats_observations (
    id INTEGER PRIMARY KEY,
    player_season_id INTEGER NOT NULL REFERENCES player_seasons(id) ON DELETE CASCADE,
    observed_at TEXT NOT NULL,
    minutes INTEGER NOT NULL DEFAULT 0 CHECK (minutes >= 0),
    starts INTEGER NOT NULL DEFAULT 0 CHECK (starts >= 0),
    goals INTEGER NOT NULL DEFAULT 0 CHECK (goals >= 0),
    assists INTEGER NOT NULL DEFAULT 0 CHECK (assists >= 0),
    clean_sheets INTEGER NOT NULL DEFAULT 0 CHECK (clean_sheets >= 0),
    goals_conceded INTEGER NOT NULL DEFAULT 0 CHECK (goals_conceded >= 0),
    own_goals INTEGER NOT NULL DEFAULT 0 CHECK (own_goals >= 0),
    penalties_saved INTEGER NOT NULL DEFAULT 0 CHECK (penalties_saved >= 0),
    penalties_missed INTEGER NOT NULL DEFAULT 0 CHECK (penalties_missed >= 0),
    yellow_cards INTEGER NOT NULL DEFAULT 0 CHECK (yellow_cards >= 0),
    red_cards INTEGER NOT NULL DEFAULT 0 CHECK (red_cards >= 0),
    saves INTEGER NOT NULL DEFAULT 0 CHECK (saves >= 0),
    bonus INTEGER NOT NULL DEFAULT 0 CHECK (bonus >= 0),
    bps INTEGER NOT NULL DEFAULT 0,
    defensive_contributions INTEGER NOT NULL DEFAULT 0 CHECK (
        defensive_contributions >= 0
    ),
    expected_goals REAL,
    expected_assists REAL,
    expected_goal_involvements REAL,
    expected_goals_conceded REAL,
    total_points INTEGER NOT NULL DEFAULT 0,
    source_observation_key TEXT NOT NULL,
    provenance_run_id INTEGER NOT NULL REFERENCES ingestion_runs(id),
    UNIQUE (player_season_id, source_observation_key)
);

CREATE INDEX idx_season_stats_player_time
    ON player_season_stats_observations(player_season_id, observed_at);

INSERT INTO fixture_observations (
    fixture_id, gameweek_id, kickoff_time, home_score, away_score,
    finished, provenance_run_id
)
SELECT id, gameweek_id, kickoff_time, home_score, away_score,
       finished, provenance_run_id
FROM fixtures;

PRAGMA user_version = 5;
COMMIT;
"""

MIGRATE_V5_TO_V6_SQL = """
PRAGMA foreign_keys = ON;
BEGIN;

CREATE TABLE manager_snapshots (
    id INTEGER PRIMARY KEY,
    season_id INTEGER NOT NULL REFERENCES seasons(id) ON DELETE CASCADE,
    gameweek_id INTEGER NOT NULL REFERENCES gameweeks(id),
    data_ingestion_run_id INTEGER REFERENCES ingestion_runs(id),
    captured_at TEXT NOT NULL,
    bank_tenths INTEGER NOT NULL CHECK (bank_tenths >= 0),
    free_transfers INTEGER NOT NULL CHECK (free_transfers BETWEEN 0 AND 5),
    remaining_chips_json TEXT NOT NULL,
    captain_player_season_id INTEGER REFERENCES player_seasons(id),
    vice_captain_player_season_id INTEGER REFERENCES player_seasons(id),
    note TEXT,
    CHECK (
        captain_player_season_id IS NULL
        OR vice_captain_player_season_id IS NULL
        OR captain_player_season_id <> vice_captain_player_season_id
    )
);

CREATE TABLE manager_squad_entries (
    id INTEGER PRIMARY KEY,
    manager_snapshot_id INTEGER NOT NULL
        REFERENCES manager_snapshots(id) ON DELETE CASCADE,
    player_season_id INTEGER NOT NULL REFERENCES player_seasons(id),
    purchase_price_tenths INTEGER NOT NULL CHECK (purchase_price_tenths >= 0),
    selling_price_tenths INTEGER NOT NULL CHECK (selling_price_tenths >= 0),
    is_starter INTEGER NOT NULL CHECK (is_starter IN (0, 1)),
    bench_order INTEGER CHECK (
        (is_starter = 1 AND bench_order IS NULL)
        OR (is_starter = 0 AND bench_order BETWEEN 1 AND 4)
    ),
    UNIQUE (manager_snapshot_id, player_season_id),
    UNIQUE (manager_snapshot_id, bench_order)
);

CREATE INDEX idx_manager_snapshots_gameweek
    ON manager_snapshots(season_id, gameweek_id, captured_at);
CREATE INDEX idx_manager_entries_snapshot
    ON manager_squad_entries(manager_snapshot_id);

PRAGMA user_version = 6;
COMMIT;
"""

MIGRATE_V6_TO_V7_SQL = """
PRAGMA foreign_keys = ON;
BEGIN;

CREATE TABLE projection_runs (
    id INTEGER PRIMARY KEY,
    season_id INTEGER NOT NULL REFERENCES seasons(id) ON DELETE CASCADE,
    generated_at TEXT NOT NULL,
    start_gameweek INTEGER NOT NULL CHECK (start_gameweek BETWEEN 1 AND 38),
    horizon_gameweeks INTEGER NOT NULL CHECK (horizon_gameweeks > 0),
    model_version TEXT NOT NULL,
    observation_mode TEXT NOT NULL,
    assumptions_json TEXT NOT NULL,
    source_ingestion_run_id INTEGER REFERENCES ingestion_runs(id)
);

CREATE TABLE player_gameweek_projections (
    id INTEGER PRIMARY KEY,
    projection_run_id INTEGER NOT NULL REFERENCES projection_runs(id) ON DELETE CASCADE,
    player_season_id INTEGER NOT NULL REFERENCES player_seasons(id) ON DELETE CASCADE,
    gameweek_number INTEGER NOT NULL CHECK (gameweek_number BETWEEN 1 AND 38),
    expected_minutes REAL NOT NULL CHECK (expected_minutes BETWEEN 0 AND 180),
    appearance_points REAL NOT NULL,
    goal_points REAL NOT NULL,
    assist_points REAL NOT NULL,
    clean_sheet_points REAL NOT NULL,
    save_points REAL NOT NULL,
    defensive_contribution_points REAL NOT NULL,
    bonus_points REAL NOT NULL,
    deduction_points REAL NOT NULL,
    expected_points REAL NOT NULL,
    uncertainty REAL NOT NULL CHECK (uncertainty >= 0),
    assumptions_json TEXT NOT NULL,
    override_rationale TEXT,
    UNIQUE (projection_run_id, player_season_id, gameweek_number)
);

CREATE INDEX idx_projection_runs_season
    ON projection_runs(season_id, generated_at);
CREATE INDEX idx_projections_run_gameweek
    ON player_gameweek_projections(projection_run_id, gameweek_number);

PRAGMA user_version = 7;
COMMIT;
"""

MIGRATE_V7_TO_V8_SQL = """
PRAGMA foreign_keys = ON;
BEGIN;

CREATE TABLE news_evidence (
    id INTEGER PRIMARY KEY,
    season_id INTEGER NOT NULL REFERENCES seasons(id) ON DELETE CASCADE,
    gameweek_id INTEGER NOT NULL REFERENCES gameweeks(id),
    player_season_id INTEGER REFERENCES player_seasons(id),
    evidence_type TEXT NOT NULL,
    summary TEXT NOT NULL,
    source_url TEXT,
    evidence_at TEXT NOT NULL,
    confidence TEXT NOT NULL CHECK (confidence IN ('low', 'medium', 'high')),
    review_status TEXT NOT NULL CHECK (
        review_status IN ('pending', 'accepted', 'rejected')
    ),
    expected_minutes_adjustment REAL,
    rationale TEXT
);

CREATE TABLE weekly_decision_runs (
    id INTEGER PRIMARY KEY,
    season_id INTEGER NOT NULL REFERENCES seasons(id) ON DELETE CASCADE,
    gameweek_id INTEGER NOT NULL REFERENCES gameweeks(id),
    manager_snapshot_id INTEGER NOT NULL REFERENCES manager_snapshots(id),
    projection_run_id INTEGER NOT NULL REFERENCES projection_runs(id),
    mode TEXT NOT NULL CHECK (mode IN ('provisional', 'final')),
    created_at TEXT NOT NULL,
    frozen_at TEXT,
    recommendation_json TEXT NOT NULL,
    decision_triggers_json TEXT NOT NULL,
    overrides_json TEXT NOT NULL,
    CHECK (
        (mode = 'provisional' AND frozen_at IS NULL)
        OR (mode = 'final' AND frozen_at IS NOT NULL)
    )
);

CREATE TABLE actual_actions (
    id INTEGER PRIMARY KEY,
    weekly_decision_run_id INTEGER NOT NULL UNIQUE
        REFERENCES weekly_decision_runs(id) ON DELETE CASCADE,
    recorded_at TEXT NOT NULL,
    action_json TEXT NOT NULL,
    followed_recommendation INTEGER NOT NULL CHECK (
        followed_recommendation IN (0, 1)
    ),
    deviation_reason TEXT
);

CREATE TABLE weekly_evaluations (
    id INTEGER PRIMARY KEY,
    weekly_decision_run_id INTEGER NOT NULL UNIQUE
        REFERENCES weekly_decision_runs(id) ON DELETE CASCADE,
    evaluated_at TEXT NOT NULL,
    forecast_points REAL NOT NULL,
    realised_points REAL NOT NULL,
    score_error REAL NOT NULL,
    review_notes TEXT
);

CREATE INDEX idx_news_review_queue
    ON news_evidence(season_id, gameweek_id, review_status);
CREATE INDEX idx_weekly_runs_gameweek
    ON weekly_decision_runs(season_id, gameweek_id, created_at);

CREATE TRIGGER prevent_final_weekly_run_update
BEFORE UPDATE ON weekly_decision_runs
WHEN OLD.mode = 'final'
BEGIN
    SELECT RAISE(ABORT, 'final weekly decision runs are immutable');
END;

CREATE TRIGGER prevent_final_weekly_run_delete
BEFORE DELETE ON weekly_decision_runs
WHEN OLD.mode = 'final'
BEGIN
    SELECT RAISE(ABORT, 'final weekly decision runs are immutable');
END;

PRAGMA user_version = 8;
COMMIT;
"""
