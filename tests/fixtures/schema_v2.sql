PRAGMA foreign_keys = ON;

CREATE TABLE seasons (
    id INTEGER PRIMARY KEY, code TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
    starts_on TEXT, ends_on TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE ingestion_runs (
    id INTEGER PRIMARY KEY, source_name TEXT NOT NULL, source_url TEXT,
    retrieved_at TEXT NOT NULL, content_sha256 TEXT,
    status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
    row_count INTEGER NOT NULL DEFAULT 0 CHECK (row_count >= 0), error_message TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE teams (
    id INTEGER PRIMARY KEY, season_id INTEGER NOT NULL REFERENCES seasons(id) ON DELETE CASCADE,
    source_team_id TEXT NOT NULL, name TEXT NOT NULL, short_name TEXT NOT NULL,
    provenance_run_id INTEGER NOT NULL REFERENCES ingestion_runs(id),
    UNIQUE (season_id, source_team_id)
);
CREATE TABLE players (
    id INTEGER PRIMARY KEY, source_name TEXT NOT NULL, source_player_id TEXT NOT NULL,
    first_name TEXT NOT NULL DEFAULT '', second_name TEXT NOT NULL DEFAULT '',
    web_name TEXT NOT NULL, date_of_birth TEXT,
    UNIQUE (source_name, source_player_id)
);
CREATE TABLE player_seasons (
    id INTEGER PRIMARY KEY, season_id INTEGER NOT NULL REFERENCES seasons(id) ON DELETE CASCADE,
    player_id INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    team_id INTEGER NOT NULL REFERENCES teams(id),
    position TEXT NOT NULL CHECK (position IN ('GK', 'DEF', 'MID', 'FWD')),
    start_price_tenths INTEGER CHECK (start_price_tenths >= 0),
    end_price_tenths INTEGER CHECK (end_price_tenths >= 0),
    provenance_run_id INTEGER NOT NULL REFERENCES ingestion_runs(id),
    UNIQUE (season_id, player_id)
);
CREATE TABLE gameweeks (
    id INTEGER PRIMARY KEY, season_id INTEGER NOT NULL REFERENCES seasons(id) ON DELETE CASCADE,
    number INTEGER NOT NULL CHECK (number BETWEEN 1 AND 60), deadline_time TEXT,
    is_finished INTEGER NOT NULL DEFAULT 0 CHECK (is_finished IN (0, 1)),
    provenance_run_id INTEGER NOT NULL REFERENCES ingestion_runs(id),
    UNIQUE (season_id, number)
);
CREATE TABLE fixtures (
    id INTEGER PRIMARY KEY, season_id INTEGER NOT NULL REFERENCES seasons(id) ON DELETE CASCADE,
    source_fixture_id TEXT NOT NULL, gameweek_id INTEGER REFERENCES gameweeks(id),
    kickoff_time TEXT, home_team_id INTEGER NOT NULL REFERENCES teams(id),
    away_team_id INTEGER NOT NULL REFERENCES teams(id),
    home_score INTEGER CHECK (home_score IS NULL OR home_score >= 0),
    away_score INTEGER CHECK (away_score IS NULL OR away_score >= 0),
    finished INTEGER NOT NULL DEFAULT 0 CHECK (finished IN (0, 1)),
    provenance_run_id INTEGER NOT NULL REFERENCES ingestion_runs(id),
    CHECK (home_team_id <> away_team_id), UNIQUE (season_id, source_fixture_id)
);
CREATE TABLE player_fixture_stats (
    id INTEGER PRIMARY KEY, player_season_id INTEGER NOT NULL REFERENCES player_seasons(id) ON DELETE CASCADE,
    fixture_id INTEGER NOT NULL REFERENCES fixtures(id) ON DELETE CASCADE,
    minutes INTEGER NOT NULL DEFAULT 0 CHECK (minutes BETWEEN 0 AND 180),
    starts INTEGER NOT NULL DEFAULT 0 CHECK (starts IN (0, 1)),
    goals INTEGER NOT NULL DEFAULT 0 CHECK (goals >= 0), assists INTEGER NOT NULL DEFAULT 0 CHECK (assists >= 0),
    clean_sheet INTEGER NOT NULL DEFAULT 0 CHECK (clean_sheet IN (0, 1)),
    goals_conceded INTEGER NOT NULL DEFAULT 0 CHECK (goals_conceded >= 0),
    own_goals INTEGER NOT NULL DEFAULT 0 CHECK (own_goals >= 0),
    penalties_saved INTEGER NOT NULL DEFAULT 0 CHECK (penalties_saved >= 0),
    penalties_missed INTEGER NOT NULL DEFAULT 0 CHECK (penalties_missed >= 0),
    yellow_cards INTEGER NOT NULL DEFAULT 0 CHECK (yellow_cards >= 0),
    red_cards INTEGER NOT NULL DEFAULT 0 CHECK (red_cards >= 0),
    saves INTEGER NOT NULL DEFAULT 0 CHECK (saves >= 0), bonus INTEGER NOT NULL DEFAULT 0 CHECK (bonus >= 0),
    bps INTEGER NOT NULL DEFAULT 0,
    defensive_contributions INTEGER NOT NULL DEFAULT 0 CHECK (defensive_contributions >= 0),
    expected_goals REAL, expected_assists REAL, expected_goal_involvements REAL,
    expected_goals_conceded REAL, total_points INTEGER NOT NULL DEFAULT 0,
    provenance_run_id INTEGER NOT NULL REFERENCES ingestion_runs(id),
    UNIQUE (player_season_id, fixture_id)
);
CREATE TABLE player_gameweek_snapshots (
    id INTEGER PRIMARY KEY, player_season_id INTEGER NOT NULL REFERENCES player_seasons(id) ON DELETE CASCADE,
    gameweek_id INTEGER NOT NULL REFERENCES gameweeks(id) ON DELETE CASCADE,
    team_id INTEGER REFERENCES teams(id), price_tenths INTEGER NOT NULL CHECK (price_tenths >= 0),
    selected_by_percent REAL CHECK (selected_by_percent IS NULL OR selected_by_percent BETWEEN 0 AND 100),
    transfers_in INTEGER CHECK (transfers_in IS NULL OR transfers_in >= 0),
    transfers_out INTEGER CHECK (transfers_out IS NULL OR transfers_out >= 0), status TEXT,
    chance_of_playing_next_round INTEGER CHECK (
        chance_of_playing_next_round IS NULL OR chance_of_playing_next_round BETWEEN 0 AND 100
    ), news TEXT, captured_at TEXT NOT NULL,
    provenance_run_id INTEGER NOT NULL REFERENCES ingestion_runs(id),
    UNIQUE (player_season_id, gameweek_id)
);
CREATE INDEX idx_player_seasons_team ON player_seasons(team_id);
CREATE INDEX idx_fixtures_gameweek ON fixtures(gameweek_id);
CREATE INDEX idx_fixture_stats_fixture ON player_fixture_stats(fixture_id);
CREATE INDEX idx_snapshots_gameweek ON player_gameweek_snapshots(gameweek_id);
