"""SQLite schema and migrations for historical FPL data."""

SCHEMA_VERSION = 15

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
    appearance_probability REAL NOT NULL DEFAULT 0 CHECK (
        appearance_probability BETWEEN 0 AND 1
    ),
    sixty_probability REAL NOT NULL DEFAULT 0 CHECK (
        sixty_probability BETWEEN 0 AND 1
    ),
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
    rationale TEXT,
    schema_version INTEGER NOT NULL DEFAULT 1 CHECK (schema_version IN (1, 2, 3)),
    source_name TEXT,
    published_at TEXT,
    source_tier TEXT CHECK (
        source_tier IS NULL
        OR source_tier IN ('official', 'strong_reporting', 'predicted_lineup', 'rumour')
    ),
    model_area TEXT CHECK (
        model_area IS NULL
        OR model_area IN (
            'minutes', 'role', 'availability', 'set_pieces', 'fixture', 'none',
            'expected_minutes', 'appearance_probability', 'starting_probability',
            'sixty_probability', 'return_date', 'penalties', 'corners',
            'direct_free_kicks', 'tactical_role', 'attacking_position',
            'team_attack', 'team_defence', 'fixture_status', 'informational'
        )
    ),
    suggested_adjustment_json TEXT,
    adjustment_basis TEXT,
    requires_decision INTEGER NOT NULL DEFAULT 1 CHECK (
        requires_decision IN (0, 1)
    ),
    decision_question TEXT,
    expires_at TEXT,
    prompt_version TEXT,
    research_run_id TEXT,
    reviewed_at TEXT,
    decision_maker TEXT,
    original_value REAL,
    proposed_value REAL,
    accepted_value REAL,
    input_package_id TEXT,
    input_package_hash TEXT,
    research_window_start TEXT,
    target_deadline TEXT,
    research_mode TEXT CHECK (
        research_mode IS NULL OR research_mode IN ('preseason', 'provisional', 'final')
    ),
    priority TEXT,
    selected_player_status TEXT,
    adjustment_support TEXT,
    temporal_status TEXT,
    conflict_group_id TEXT,
    supporting_evidence_json TEXT,
    conflicting_evidence_json TEXT,
    unresolved_uncertainty TEXT,
    resolution_event TEXT,
    confidence_after_conflict TEXT,
    research_result_id INTEGER
);

CREATE TABLE IF NOT EXISTS team_news_input_packages (
    id INTEGER PRIMARY KEY,
    package_id TEXT NOT NULL UNIQUE,
    package_hash TEXT NOT NULL UNIQUE,
    season_id INTEGER NOT NULL REFERENCES seasons(id) ON DELETE CASCADE,
    gameweek_id INTEGER NOT NULL REFERENCES gameweeks(id),
    target_deadline TEXT,
    research_timestamp TEXT NOT NULL,
    research_window_start TEXT NOT NULL,
    research_mode TEXT NOT NULL CHECK (
        research_mode IN ('preseason', 'provisional', 'final')
    ),
    projection_run_id INTEGER NOT NULL REFERENCES projection_runs(id),
    recommendation_run_id TEXT,
    source_ingestion_run_id INTEGER REFERENCES ingestion_runs(id),
    prompt_version TEXT NOT NULL,
    package_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS team_news_research_runs (
    id INTEGER PRIMARY KEY,
    research_run_id TEXT NOT NULL UNIQUE,
    input_package_id TEXT NOT NULL REFERENCES team_news_input_packages(package_id),
    input_package_hash TEXT NOT NULL,
    season_id INTEGER NOT NULL REFERENCES seasons(id) ON DELETE CASCADE,
    gameweek_id INTEGER NOT NULL REFERENCES gameweeks(id),
    research_mode TEXT NOT NULL CHECK (
        research_mode IN ('preseason', 'provisional', 'final')
    ),
    research_window_start TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    target_deadline TEXT,
    prompt_version TEXT NOT NULL,
    schema_version INTEGER NOT NULL CHECK (schema_version = 3),
    raw_result_json TEXT NOT NULL,
    import_status TEXT NOT NULL DEFAULT 'imported' CHECK (
        import_status IN ('imported', 'quarantined')
    ),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS team_news_coverage (
    id INTEGER PRIMARY KEY,
    research_result_id INTEGER NOT NULL REFERENCES team_news_research_runs(id) ON DELETE CASCADE,
    source_player_id TEXT NOT NULL,
    priority TEXT NOT NULL CHECK (
        priority IN ('critical', 'starting_xi', 'bench_cover', 'squad', 'alternative', 'broad_scan')
    ),
    status TEXT NOT NULL CHECK (
        status IN ('checked_material_evidence', 'checked_no_material_evidence',
                   'partially_checked', 'source_unavailable', 'identity_unresolved', 'not_checked')
    ),
    areas_checked_json TEXT NOT NULL,
    latest_source_checked_at TEXT,
    notes TEXT,
    UNIQUE (research_result_id, source_player_id)
);

CREATE TABLE IF NOT EXISTS team_news_discoveries (
    id INTEGER PRIMARY KEY,
    research_result_id INTEGER NOT NULL REFERENCES team_news_research_runs(id) ON DELETE CASCADE,
    discovery_id TEXT NOT NULL,
    source_player_id TEXT,
    identity_status TEXT NOT NULL CHECK (identity_status IN ('resolved', 'unresolved')),
    discovery_json TEXT NOT NULL,
    UNIQUE (research_result_id, discovery_id)
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

CREATE TABLE IF NOT EXISTS news_projection_pairs (
    id INTEGER PRIMARY KEY,
    season_id INTEGER NOT NULL REFERENCES seasons(id) ON DELETE CASCADE,
    gameweek_id INTEGER NOT NULL REFERENCES gameweeks(id),
    pre_news_projection_run_id INTEGER NOT NULL UNIQUE
        REFERENCES projection_runs(id),
    post_news_projection_run_id INTEGER NOT NULL UNIQUE
        REFERENCES projection_runs(id),
    created_at TEXT NOT NULL,
    evidence_ids_json TEXT NOT NULL,
    input_package_id TEXT,
    research_run_id TEXT,
    source_ingestion_run_id INTEGER REFERENCES ingestion_runs(id),
    CHECK (pre_news_projection_run_id <> post_news_projection_run_id)
);

CREATE TABLE IF NOT EXISTS news_projection_evaluations (
    id INTEGER PRIMARY KEY,
    news_projection_pair_id INTEGER NOT NULL UNIQUE
        REFERENCES news_projection_pairs(id) ON DELETE CASCADE,
    evaluated_at TEXT NOT NULL,
    sample_count INTEGER NOT NULL CHECK (sample_count > 0),
    pre_news_points_mae REAL NOT NULL,
    post_news_points_mae REAL NOT NULL,
    pre_news_minutes_mae REAL NOT NULL,
    post_news_minutes_mae REAL NOT NULL,
    points_mae_change REAL NOT NULL,
    minutes_mae_change REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS projection_backtest_runs (
    id INTEGER PRIMARY KEY,
    season_id INTEGER NOT NULL REFERENCES seasons(id) ON DELETE CASCADE,
    model_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    origin_gameweek_start INTEGER NOT NULL CHECK (
        origin_gameweek_start BETWEEN 1 AND 38
    ),
    origin_gameweek_end INTEGER NOT NULL CHECK (
        origin_gameweek_end BETWEEN origin_gameweek_start AND 38
    ),
    horizon_gameweeks INTEGER NOT NULL CHECK (horizon_gameweeks > 0),
    evidence_policy TEXT NOT NULL CHECK (
        evidence_policy IN ('performance_only', 'pre_deadline_only')
    ),
    model_config_json TEXT NOT NULL,
    limitations_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
    generated_prediction_count INTEGER NOT NULL DEFAULT 0 CHECK (
        generated_prediction_count >= 0
    ),
    prediction_count INTEGER NOT NULL DEFAULT 0 CHECK (prediction_count >= 0),
    missing_outcome_count INTEGER NOT NULL DEFAULT 0 CHECK (
        missing_outcome_count >= 0
    ),
    source_ingestion_run_id INTEGER REFERENCES ingestion_runs(id),
    data_fingerprint TEXT,
    error_message TEXT
);

CREATE TABLE IF NOT EXISTS projection_backtest_predictions (
    id INTEGER PRIMARY KEY,
    backtest_run_id INTEGER NOT NULL
        REFERENCES projection_backtest_runs(id) ON DELETE CASCADE,
    origin_gameweek INTEGER NOT NULL CHECK (origin_gameweek BETWEEN 1 AND 38),
    target_gameweek INTEGER NOT NULL CHECK (target_gameweek BETWEEN 1 AND 38),
    horizon_step INTEGER NOT NULL CHECK (horizon_step > 0),
    player_season_id INTEGER NOT NULL REFERENCES player_seasons(id) ON DELETE CASCADE,
    fixture_count INTEGER NOT NULL CHECK (fixture_count > 0),
    expected_minutes REAL NOT NULL,
    appearance_probability REAL NOT NULL DEFAULT 0 CHECK (
        appearance_probability BETWEEN 0 AND 1
    ),
    sixty_probability REAL NOT NULL DEFAULT 0 CHECK (
        sixty_probability BETWEEN 0 AND 1
    ),
    actual_minutes INTEGER NOT NULL CHECK (actual_minutes >= 0),
    expected_points REAL NOT NULL,
    actual_points INTEGER NOT NULL,
    uncertainty REAL NOT NULL CHECK (uncertainty >= 0),
    component_points_json TEXT,
    UNIQUE (
        backtest_run_id, origin_gameweek, target_gameweek, player_season_id
    )
);

CREATE TABLE IF NOT EXISTS model_candidate_registrations (
    id INTEGER PRIMARY KEY,
    candidate_key TEXT NOT NULL UNIQUE,
    season_id INTEGER NOT NULL REFERENCES seasons(id) ON DELETE CASCADE,
    model_version TEXT NOT NULL,
    registered_at TEXT NOT NULL,
    model_config_json TEXT NOT NULL,
    model_config_sha256 TEXT NOT NULL,
    gate_policy_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'declared' CHECK (
        status IN ('declared', 'qualified', 'rejected')
    ),
    evaluated_at TEXT,
    evaluation_report_json TEXT,
    CHECK (
        (status = 'declared' AND evaluated_at IS NULL
         AND evaluation_report_json IS NULL)
        OR (status IN ('qualified', 'rejected')
            AND evaluated_at IS NOT NULL
            AND evaluation_report_json IS NOT NULL)
    )
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
CREATE INDEX IF NOT EXISTS idx_news_projection_pairs_gameweek
    ON news_projection_pairs(season_id, gameweek_id, created_at);
CREATE INDEX IF NOT EXISTS idx_backtest_runs_season
    ON projection_backtest_runs(season_id, created_at);
CREATE INDEX IF NOT EXISTS idx_backtest_predictions_run
    ON projection_backtest_predictions(backtest_run_id, horizon_step);
CREATE INDEX IF NOT EXISTS idx_candidate_registrations_season
    ON model_candidate_registrations(season_id, status, registered_at);

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

CREATE TRIGGER IF NOT EXISTS prevent_candidate_declaration_change
BEFORE UPDATE OF candidate_key, season_id, model_version, registered_at,
                 model_config_json, model_config_sha256, gate_policy_json
ON model_candidate_registrations
BEGIN
    SELECT RAISE(ABORT, 'model candidate declarations are immutable');
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

CREATE TABLE IF NOT EXISTS reviewed_projection_modifiers (
    id INTEGER PRIMARY KEY,
    season_id INTEGER NOT NULL REFERENCES seasons(id) ON DELETE CASCADE,
    gameweek_id INTEGER NOT NULL REFERENCES gameweeks(id),
    source_player_id TEXT,
    source_team_id TEXT,
    modifier_type TEXT NOT NULL CHECK (modifier_type IN (
        'expected_minutes', 'expected_minutes_delta',
        'appearance_probability', 'appearance_probability_delta',
        'starting_probability', 'starting_probability_delta',
        'sixty_probability', 'sixty_probability_delta', 'availability'
    )),
    operation TEXT NOT NULL CHECK (operation IN ('set', 'delta', 'multiplier', 'unavailable')),
    value REAL NOT NULL,
    start_gameweek INTEGER NOT NULL CHECK (start_gameweek BETWEEN 1 AND 38),
    end_gameweek INTEGER NOT NULL CHECK (end_gameweek BETWEEN 1 AND 38),
    evidence_ids_json TEXT NOT NULL,
    rationale TEXT NOT NULL,
    reviewed_by TEXT NOT NULL,
    reviewed_at TEXT NOT NULL,
    expires_at TEXT,
    research_run_id TEXT,
    input_package_id TEXT,
    status TEXT NOT NULL DEFAULT 'accepted' CHECK (
        status IN ('accepted', 'superseded', 'rejected')
    ),
    model_support TEXT NOT NULL DEFAULT 'supported' CHECK (
        model_support IN ('supported', 'informational', 'unsupported')
    ),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    supersedes_id INTEGER REFERENCES reviewed_projection_modifiers(id),
    created_at TEXT NOT NULL,
    CHECK (start_gameweek <= end_gameweek),
    CHECK (source_player_id IS NOT NULL OR source_team_id IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS projection_run_modifier_links (
    projection_run_id INTEGER NOT NULL REFERENCES projection_runs(id) ON DELETE CASCADE,
    modifier_id INTEGER NOT NULL REFERENCES reviewed_projection_modifiers(id),
    effective_value_json TEXT NOT NULL,
    PRIMARY KEY (projection_run_id, modifier_id)
);

CREATE TABLE IF NOT EXISTS research_projection_runs (
    revised_projection_run_id INTEGER PRIMARY KEY REFERENCES projection_runs(id) ON DELETE CASCADE,
    baseline_projection_run_id INTEGER NOT NULL REFERENCES projection_runs(id),
    decision_type TEXT NOT NULL CHECK (decision_type IN ('opening_squad', 'transfers', 'weekly_xi')),
    input_package_id TEXT,
    research_run_id TEXT,
    source_ingestion_run_id INTEGER REFERENCES ingestion_runs(id),
    model_config_hash TEXT NOT NULL,
    horizon_gameweeks INTEGER NOT NULL CHECK (horizon_gameweeks > 0),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS research_decision_comparisons (
    id INTEGER PRIMARY KEY,
    decision_type TEXT NOT NULL CHECK (decision_type IN ('opening_squad', 'transfers', 'weekly_xi')),
    baseline_projection_run_id INTEGER NOT NULL REFERENCES projection_runs(id),
    revised_projection_run_id INTEGER NOT NULL REFERENCES projection_runs(id),
    baseline_recommendation_json TEXT NOT NULL,
    revised_recommendation_json TEXT NOT NULL,
    baseline_objective REAL NOT NULL,
    baseline_revalued_objective REAL NOT NULL,
    revised_objective REAL NOT NULL,
    decision_improvement REAL NOT NULL,
    projection_impact REAL NOT NULL,
    changed_players_json TEXT NOT NULL,
    explanations_json TEXT NOT NULL,
    modifier_ids_json TEXT NOT NULL,
    robustness TEXT NOT NULL CHECK (robustness IN ('robust', 'moderate', 'near_tie')),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_reviewed_modifiers_scope
    ON reviewed_projection_modifiers(season_id, start_gameweek, end_gameweek, status);
CREATE INDEX IF NOT EXISTS idx_projection_modifier_links_run
    ON projection_run_modifier_links(projection_run_id);
CREATE INDEX IF NOT EXISTS idx_research_comparisons_runs
    ON research_decision_comparisons(baseline_projection_run_id, revised_projection_run_id);

CREATE TRIGGER IF NOT EXISTS prevent_reviewed_modifier_update
BEFORE UPDATE ON reviewed_projection_modifiers
BEGIN
    SELECT RAISE(ABORT, 'reviewed projection modifiers are immutable; create a superseding modifier');
END;

CREATE TRIGGER IF NOT EXISTS prevent_reviewed_modifier_delete
BEFORE DELETE ON reviewed_projection_modifiers
BEGIN
    SELECT RAISE(ABORT, 'reviewed projection modifiers are immutable');
END;
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

MIGRATE_V8_TO_V9_SQL = """
PRAGMA foreign_keys = ON;
BEGIN;

CREATE TABLE projection_backtest_runs (
    id INTEGER PRIMARY KEY,
    season_id INTEGER NOT NULL REFERENCES seasons(id) ON DELETE CASCADE,
    model_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    origin_gameweek_start INTEGER NOT NULL CHECK (
        origin_gameweek_start BETWEEN 1 AND 38
    ),
    origin_gameweek_end INTEGER NOT NULL CHECK (
        origin_gameweek_end BETWEEN origin_gameweek_start AND 38
    ),
    horizon_gameweeks INTEGER NOT NULL CHECK (horizon_gameweeks > 0),
    evidence_policy TEXT NOT NULL CHECK (
        evidence_policy IN ('performance_only', 'pre_deadline_only')
    ),
    model_config_json TEXT NOT NULL,
    limitations_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
    prediction_count INTEGER NOT NULL DEFAULT 0 CHECK (prediction_count >= 0),
    error_message TEXT
);

CREATE TABLE projection_backtest_predictions (
    id INTEGER PRIMARY KEY,
    backtest_run_id INTEGER NOT NULL
        REFERENCES projection_backtest_runs(id) ON DELETE CASCADE,
    origin_gameweek INTEGER NOT NULL CHECK (origin_gameweek BETWEEN 1 AND 38),
    target_gameweek INTEGER NOT NULL CHECK (target_gameweek BETWEEN 1 AND 38),
    horizon_step INTEGER NOT NULL CHECK (horizon_step > 0),
    player_season_id INTEGER NOT NULL REFERENCES player_seasons(id) ON DELETE CASCADE,
    fixture_count INTEGER NOT NULL CHECK (fixture_count > 0),
    expected_minutes REAL NOT NULL,
    actual_minutes INTEGER NOT NULL CHECK (actual_minutes >= 0),
    expected_points REAL NOT NULL,
    actual_points INTEGER NOT NULL,
    uncertainty REAL NOT NULL CHECK (uncertainty >= 0),
    UNIQUE (
        backtest_run_id, origin_gameweek, target_gameweek, player_season_id
    )
);

CREATE INDEX idx_backtest_runs_season
    ON projection_backtest_runs(season_id, created_at);
CREATE INDEX idx_backtest_predictions_run
    ON projection_backtest_predictions(backtest_run_id, horizon_step);

PRAGMA user_version = 9;
COMMIT;
"""

MIGRATE_V9_TO_V10_SQL = """
PRAGMA foreign_keys = ON;
BEGIN;

ALTER TABLE projection_backtest_runs
    ADD COLUMN generated_prediction_count INTEGER NOT NULL DEFAULT 0
    CHECK (generated_prediction_count >= 0);
ALTER TABLE projection_backtest_runs
    ADD COLUMN missing_outcome_count INTEGER NOT NULL DEFAULT 0
    CHECK (missing_outcome_count >= 0);
ALTER TABLE projection_backtest_runs
    ADD COLUMN source_ingestion_run_id INTEGER;
ALTER TABLE projection_backtest_runs
    ADD COLUMN data_fingerprint TEXT;

UPDATE projection_backtest_runs
SET generated_prediction_count = prediction_count;

PRAGMA user_version = 10;
COMMIT;
"""

MIGRATE_V10_TO_V11_SQL = """
PRAGMA foreign_keys = ON;
BEGIN;

ALTER TABLE news_evidence
    ADD COLUMN schema_version INTEGER NOT NULL DEFAULT 1
    CHECK (schema_version IN (1, 2));
ALTER TABLE news_evidence ADD COLUMN source_name TEXT;
ALTER TABLE news_evidence ADD COLUMN published_at TEXT;
ALTER TABLE news_evidence ADD COLUMN source_tier TEXT CHECK (
    source_tier IS NULL
    OR source_tier IN ('official', 'strong_reporting', 'predicted_lineup', 'rumour')
);
ALTER TABLE news_evidence ADD COLUMN model_area TEXT CHECK (
    model_area IS NULL
    OR model_area IN (
        'minutes', 'role', 'availability', 'set_pieces', 'fixture', 'none'
    )
);
ALTER TABLE news_evidence ADD COLUMN suggested_adjustment_json TEXT;
ALTER TABLE news_evidence ADD COLUMN adjustment_basis TEXT;
ALTER TABLE news_evidence
    ADD COLUMN requires_decision INTEGER NOT NULL DEFAULT 1
    CHECK (requires_decision IN (0, 1));
ALTER TABLE news_evidence ADD COLUMN decision_question TEXT;
ALTER TABLE news_evidence ADD COLUMN expires_at TEXT;
ALTER TABLE news_evidence ADD COLUMN prompt_version TEXT;
ALTER TABLE news_evidence ADD COLUMN research_run_id TEXT;
ALTER TABLE news_evidence ADD COLUMN reviewed_at TEXT;
ALTER TABLE news_evidence ADD COLUMN decision_maker TEXT;
ALTER TABLE news_evidence ADD COLUMN original_value REAL;
ALTER TABLE news_evidence ADD COLUMN proposed_value REAL;
ALTER TABLE news_evidence ADD COLUMN accepted_value REAL;

CREATE TABLE news_projection_pairs (
    id INTEGER PRIMARY KEY,
    season_id INTEGER NOT NULL REFERENCES seasons(id) ON DELETE CASCADE,
    gameweek_id INTEGER NOT NULL REFERENCES gameweeks(id),
    pre_news_projection_run_id INTEGER NOT NULL UNIQUE
        REFERENCES projection_runs(id),
    post_news_projection_run_id INTEGER NOT NULL UNIQUE
        REFERENCES projection_runs(id),
    created_at TEXT NOT NULL,
    evidence_ids_json TEXT NOT NULL,
    CHECK (pre_news_projection_run_id <> post_news_projection_run_id)
);

CREATE TABLE news_projection_evaluations (
    id INTEGER PRIMARY KEY,
    news_projection_pair_id INTEGER NOT NULL UNIQUE
        REFERENCES news_projection_pairs(id) ON DELETE CASCADE,
    evaluated_at TEXT NOT NULL,
    sample_count INTEGER NOT NULL CHECK (sample_count > 0),
    pre_news_points_mae REAL NOT NULL,
    post_news_points_mae REAL NOT NULL,
    pre_news_minutes_mae REAL NOT NULL,
    post_news_minutes_mae REAL NOT NULL,
    points_mae_change REAL NOT NULL,
    minutes_mae_change REAL NOT NULL
);

CREATE INDEX idx_news_projection_pairs_gameweek
    ON news_projection_pairs(season_id, gameweek_id, created_at);

PRAGMA user_version = 11;
COMMIT;
"""

MIGRATE_V11_TO_V12_SQL = """
PRAGMA foreign_keys = ON;
BEGIN;

ALTER TABLE player_gameweek_projections
    ADD COLUMN appearance_probability REAL NOT NULL DEFAULT 0
    CHECK (appearance_probability BETWEEN 0 AND 1);
ALTER TABLE player_gameweek_projections
    ADD COLUMN sixty_probability REAL NOT NULL DEFAULT 0
    CHECK (sixty_probability BETWEEN 0 AND 1);

PRAGMA user_version = 12;
COMMIT;
"""

MIGRATE_V12_TO_V13_SQL = """
PRAGMA foreign_keys = ON;
BEGIN;

ALTER TABLE projection_backtest_predictions
    ADD COLUMN appearance_probability REAL NOT NULL DEFAULT 0
    CHECK (appearance_probability BETWEEN 0 AND 1);
ALTER TABLE projection_backtest_predictions
    ADD COLUMN sixty_probability REAL NOT NULL DEFAULT 0
    CHECK (sixty_probability BETWEEN 0 AND 1);

PRAGMA user_version = 13;
COMMIT;
"""

MIGRATE_V13_TO_V14_SQL = """
PRAGMA foreign_keys = ON;
BEGIN;

ALTER TABLE projection_backtest_predictions
    ADD COLUMN component_points_json TEXT;

PRAGMA user_version = 14;
COMMIT;
"""

MIGRATE_V14_TO_V15_SQL = """
PRAGMA foreign_keys = ON;
BEGIN;

CREATE TABLE model_candidate_registrations (
    id INTEGER PRIMARY KEY,
    candidate_key TEXT NOT NULL UNIQUE,
    season_id INTEGER NOT NULL REFERENCES seasons(id) ON DELETE CASCADE,
    model_version TEXT NOT NULL,
    registered_at TEXT NOT NULL,
    model_config_json TEXT NOT NULL,
    model_config_sha256 TEXT NOT NULL,
    gate_policy_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'declared' CHECK (
        status IN ('declared', 'qualified', 'rejected')
    ),
    evaluated_at TEXT,
    evaluation_report_json TEXT,
    CHECK (
        (status = 'declared' AND evaluated_at IS NULL
         AND evaluation_report_json IS NULL)
        OR (status IN ('qualified', 'rejected')
            AND evaluated_at IS NOT NULL
            AND evaluation_report_json IS NOT NULL)
    )
);

CREATE INDEX idx_candidate_registrations_season
    ON model_candidate_registrations(season_id, status, registered_at);

CREATE TRIGGER prevent_candidate_declaration_change
BEFORE UPDATE OF candidate_key, season_id, model_version, registered_at,
                 model_config_json, model_config_sha256, gate_policy_json
ON model_candidate_registrations
BEGIN
    SELECT RAISE(ABORT, 'model candidate declarations are immutable');
END;

PRAGMA user_version = 15;
COMMIT;
"""

MIGRATE_V15_TO_V16_SQL = """
PRAGMA foreign_keys = ON;
BEGIN;

ALTER TABLE news_evidence RENAME TO news_evidence_v15;

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
    review_status TEXT NOT NULL CHECK (review_status IN ('pending', 'accepted', 'rejected')),
    expected_minutes_adjustment REAL,
    rationale TEXT,
    schema_version INTEGER NOT NULL DEFAULT 1 CHECK (schema_version IN (1, 2, 3)),
    source_name TEXT,
    published_at TEXT,
    source_tier TEXT CHECK (source_tier IS NULL OR source_tier IN ('official', 'strong_reporting', 'predicted_lineup', 'rumour')),
    model_area TEXT CHECK (model_area IS NULL OR model_area IN ('minutes', 'role', 'availability', 'set_pieces', 'fixture', 'none', 'expected_minutes', 'appearance_probability', 'starting_probability', 'sixty_probability', 'return_date', 'penalties', 'corners', 'direct_free_kicks', 'tactical_role', 'attacking_position', 'team_attack', 'team_defence', 'fixture_status', 'informational')),
    suggested_adjustment_json TEXT,
    adjustment_basis TEXT,
    requires_decision INTEGER NOT NULL DEFAULT 1 CHECK (requires_decision IN (0, 1)),
    decision_question TEXT,
    expires_at TEXT,
    prompt_version TEXT,
    research_run_id TEXT,
    reviewed_at TEXT,
    decision_maker TEXT,
    original_value REAL,
    proposed_value REAL,
    accepted_value REAL,
    input_package_id TEXT,
    input_package_hash TEXT,
    research_window_start TEXT,
    target_deadline TEXT,
    research_mode TEXT CHECK (research_mode IS NULL OR research_mode IN ('preseason', 'provisional', 'final')),
    priority TEXT,
    selected_player_status TEXT,
    adjustment_support TEXT,
    temporal_status TEXT,
    conflict_group_id TEXT,
    supporting_evidence_json TEXT,
    conflicting_evidence_json TEXT,
    unresolved_uncertainty TEXT,
    resolution_event TEXT,
    confidence_after_conflict TEXT,
    research_result_id INTEGER
);

INSERT INTO news_evidence (
    id, season_id, gameweek_id, player_season_id, evidence_type, summary,
    source_url, evidence_at, confidence, review_status,
    expected_minutes_adjustment, rationale, schema_version, source_name,
    published_at, source_tier, model_area, suggested_adjustment_json,
    adjustment_basis, requires_decision, decision_question, expires_at,
    prompt_version, research_run_id, reviewed_at, decision_maker,
    original_value, proposed_value, accepted_value
)
SELECT id, season_id, gameweek_id, player_season_id, evidence_type, summary,
       source_url, evidence_at, confidence, review_status,
       expected_minutes_adjustment, rationale, schema_version, source_name,
       published_at, source_tier, model_area, suggested_adjustment_json,
       adjustment_basis, requires_decision, decision_question, expires_at,
       prompt_version, research_run_id, reviewed_at, decision_maker,
       original_value, proposed_value, accepted_value
FROM news_evidence_v15;
DROP TABLE news_evidence_v15;

CREATE TABLE team_news_input_packages (
    id INTEGER PRIMARY KEY,
    package_id TEXT NOT NULL UNIQUE,
    package_hash TEXT NOT NULL UNIQUE,
    season_id INTEGER NOT NULL REFERENCES seasons(id) ON DELETE CASCADE,
    gameweek_id INTEGER NOT NULL REFERENCES gameweeks(id),
    target_deadline TEXT,
    research_timestamp TEXT NOT NULL,
    research_window_start TEXT NOT NULL,
    research_mode TEXT NOT NULL CHECK (research_mode IN ('preseason', 'provisional', 'final')),
    projection_run_id INTEGER NOT NULL REFERENCES projection_runs(id),
    recommendation_run_id TEXT,
    source_ingestion_run_id INTEGER REFERENCES ingestion_runs(id),
    prompt_version TEXT NOT NULL,
    package_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE team_news_research_runs (
    id INTEGER PRIMARY KEY,
    research_run_id TEXT NOT NULL UNIQUE,
    input_package_id TEXT NOT NULL REFERENCES team_news_input_packages(package_id),
    input_package_hash TEXT NOT NULL,
    season_id INTEGER NOT NULL REFERENCES seasons(id) ON DELETE CASCADE,
    gameweek_id INTEGER NOT NULL REFERENCES gameweeks(id),
    research_mode TEXT NOT NULL CHECK (research_mode IN ('preseason', 'provisional', 'final')),
    research_window_start TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    target_deadline TEXT,
    prompt_version TEXT NOT NULL,
    schema_version INTEGER NOT NULL CHECK (schema_version = 3),
    raw_result_json TEXT NOT NULL,
    import_status TEXT NOT NULL DEFAULT 'imported' CHECK (import_status IN ('imported', 'quarantined')),
    created_at TEXT NOT NULL
);
CREATE TABLE team_news_coverage (
    id INTEGER PRIMARY KEY,
    research_result_id INTEGER NOT NULL REFERENCES team_news_research_runs(id) ON DELETE CASCADE,
    source_player_id TEXT NOT NULL,
    priority TEXT NOT NULL CHECK (priority IN ('critical', 'starting_xi', 'bench_cover', 'squad', 'alternative', 'broad_scan')),
    status TEXT NOT NULL CHECK (status IN ('checked_material_evidence', 'checked_no_material_evidence', 'partially_checked', 'source_unavailable', 'identity_unresolved', 'not_checked')),
    areas_checked_json TEXT NOT NULL,
    latest_source_checked_at TEXT,
    notes TEXT,
    UNIQUE (research_result_id, source_player_id)
);
CREATE TABLE team_news_discoveries (
    id INTEGER PRIMARY KEY,
    research_result_id INTEGER NOT NULL REFERENCES team_news_research_runs(id) ON DELETE CASCADE,
    discovery_id TEXT NOT NULL,
    source_player_id TEXT,
    identity_status TEXT NOT NULL CHECK (identity_status IN ('resolved', 'unresolved')),
    discovery_json TEXT NOT NULL,
    UNIQUE (research_result_id, discovery_id)
);
ALTER TABLE news_projection_pairs ADD COLUMN input_package_id TEXT;
ALTER TABLE news_projection_pairs ADD COLUMN research_run_id TEXT;
ALTER TABLE news_projection_pairs ADD COLUMN source_ingestion_run_id INTEGER REFERENCES ingestion_runs(id);

CREATE INDEX idx_team_news_coverage_result ON team_news_coverage(research_result_id, status);
CREATE INDEX idx_team_news_research_package ON team_news_research_runs(input_package_id, generated_at);

CREATE TABLE reviewed_projection_modifiers (
    id INTEGER PRIMARY KEY,
    season_id INTEGER NOT NULL REFERENCES seasons(id) ON DELETE CASCADE,
    gameweek_id INTEGER NOT NULL REFERENCES gameweeks(id),
    source_player_id TEXT,
    source_team_id TEXT,
    modifier_type TEXT NOT NULL CHECK (modifier_type IN (
        'expected_minutes', 'expected_minutes_delta',
        'appearance_probability', 'appearance_probability_delta',
        'starting_probability', 'starting_probability_delta',
        'sixty_probability', 'sixty_probability_delta', 'availability'
    )),
    operation TEXT NOT NULL CHECK (operation IN ('set', 'delta', 'multiplier', 'unavailable')),
    value REAL NOT NULL,
    start_gameweek INTEGER NOT NULL CHECK (start_gameweek BETWEEN 1 AND 38),
    end_gameweek INTEGER NOT NULL CHECK (end_gameweek BETWEEN 1 AND 38),
    evidence_ids_json TEXT NOT NULL,
    rationale TEXT NOT NULL,
    reviewed_by TEXT NOT NULL,
    reviewed_at TEXT NOT NULL,
    expires_at TEXT,
    research_run_id TEXT,
    input_package_id TEXT,
    status TEXT NOT NULL DEFAULT 'accepted' CHECK (status IN ('accepted', 'superseded', 'rejected')),
    model_support TEXT NOT NULL DEFAULT 'supported' CHECK (model_support IN ('supported', 'informational', 'unsupported')),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    supersedes_id INTEGER REFERENCES reviewed_projection_modifiers(id),
    created_at TEXT NOT NULL,
    CHECK (start_gameweek <= end_gameweek),
    CHECK (source_player_id IS NOT NULL OR source_team_id IS NOT NULL)
);
CREATE TABLE projection_run_modifier_links (
    projection_run_id INTEGER NOT NULL REFERENCES projection_runs(id) ON DELETE CASCADE,
    modifier_id INTEGER NOT NULL REFERENCES reviewed_projection_modifiers(id),
    effective_value_json TEXT NOT NULL,
    PRIMARY KEY (projection_run_id, modifier_id)
);
CREATE TABLE research_projection_runs (
    revised_projection_run_id INTEGER PRIMARY KEY REFERENCES projection_runs(id) ON DELETE CASCADE,
    baseline_projection_run_id INTEGER NOT NULL REFERENCES projection_runs(id),
    decision_type TEXT NOT NULL CHECK (decision_type IN ('opening_squad', 'transfers', 'weekly_xi')),
    input_package_id TEXT,
    research_run_id TEXT,
    source_ingestion_run_id INTEGER REFERENCES ingestion_runs(id),
    model_config_hash TEXT NOT NULL,
    horizon_gameweeks INTEGER NOT NULL CHECK (horizon_gameweeks > 0),
    created_at TEXT NOT NULL
);
CREATE TABLE research_decision_comparisons (
    id INTEGER PRIMARY KEY,
    decision_type TEXT NOT NULL CHECK (decision_type IN ('opening_squad', 'transfers', 'weekly_xi')),
    baseline_projection_run_id INTEGER NOT NULL REFERENCES projection_runs(id),
    revised_projection_run_id INTEGER NOT NULL REFERENCES projection_runs(id),
    baseline_recommendation_json TEXT NOT NULL,
    revised_recommendation_json TEXT NOT NULL,
    baseline_objective REAL NOT NULL,
    baseline_revalued_objective REAL NOT NULL,
    revised_objective REAL NOT NULL,
    decision_improvement REAL NOT NULL,
    projection_impact REAL NOT NULL,
    changed_players_json TEXT NOT NULL,
    explanations_json TEXT NOT NULL,
    modifier_ids_json TEXT NOT NULL,
    robustness TEXT NOT NULL CHECK (robustness IN ('robust', 'moderate', 'near_tie')),
    created_at TEXT NOT NULL
);
CREATE INDEX idx_reviewed_modifiers_scope ON reviewed_projection_modifiers(season_id, start_gameweek, end_gameweek, status);
CREATE INDEX idx_projection_modifier_links_run ON projection_run_modifier_links(projection_run_id);
CREATE INDEX idx_research_comparisons_runs ON research_decision_comparisons(baseline_projection_run_id, revised_projection_run_id);
CREATE TRIGGER prevent_reviewed_modifier_update
BEFORE UPDATE ON reviewed_projection_modifiers
BEGIN
    SELECT RAISE(ABORT, 'reviewed projection modifiers are immutable; create a superseding modifier');
END;
CREATE TRIGGER prevent_reviewed_modifier_delete
BEFORE DELETE ON reviewed_projection_modifiers
BEGIN
    SELECT RAISE(ABORT, 'reviewed projection modifiers are immutable');
END;

PRAGMA user_version = 15;
COMMIT;
"""
