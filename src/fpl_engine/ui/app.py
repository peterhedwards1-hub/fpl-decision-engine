"""Streamlit entry point for manual squad-state management."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import streamlit as st

from fpl_engine.backtest import load_backtest_report
from fpl_engine.chips import recommend_chip_timing
from fpl_engine.config import load_season_rules
from fpl_engine.domain import Chip, Position
from fpl_engine.history.database import HistoricalDatabase
from fpl_engine.live.collector import LiveSnapshotCollector
from fpl_engine.manager import (
    ManagerSnapshot,
    ManagerSquadEntry,
    ManagerStateError,
    ManagerStateRepository,
)
from fpl_engine.model_health import build_model_health_report
from fpl_engine.news import ingest_structured_news
from fpl_engine.news_projection import (
    create_news_projection_pair,
    evaluate_news_projection_pair,
)
from fpl_engine.optimisation import (
    DEFAULT_OPENING_MINIMUM_MEAN_APPEARANCE,
    CandidatePlayer,
    GameweekPlayerValue,
    OptimisationError,
    appearance_qualified_candidates,
    optimise_opening_squads,
    optimise_starting_xi,
)
from fpl_engine.preseason_strength import (
    load_preseason_validation,
    preseason_model_is_validated,
)
from fpl_engine.production import (
    recommend_planning_horizon,
    select_production_projection_run,
)
from fpl_engine.projections import (
    ProjectionOverride,
    RatesProjectionModel,
)
from fpl_engine.prospective import build_prospective_capture_status
from fpl_engine.research_decision import (
    compare_opening_squad_decision,
    compare_transfer_decision,
    compare_weekly_xi_decision,
    generate_revised_projection,
    load_projection_candidates,
)
from fpl_engine.reviewed_modifiers import ReviewedProjectionModifier
from fpl_engine.team_news_v3 import generate_team_news_research_package
from fpl_engine.transfers import CurrentSquad, recommend_transfers
from fpl_engine.ui.view import pitch_html
from fpl_engine.workflow import WeeklyWorkflowRepository, WorkflowError

DATABASE_PATH = Path(os.environ.get("FPL_DATABASE_PATH", "data/fpl.sqlite3"))
RULES_PATH = Path(os.environ.get("FPL_RULES_PATH", "config/seasons/2026-27.json"))
TRANSFER_POLICY_PATH = Path(
    os.environ.get(
        "FPL_TRANSFER_POLICY_PATH",
        "data/models/transfer-policy-evaluation-v1.json",
    )
)
OPENING_SQUAD_HORIZON_GAMEWEEKS = 8
WEEKLY_PLANNING_HORIZON_GAMEWEEKS = 5


def _remaining_horizon(gameweek_number: int, requested: int) -> int:
    return min(requested, 39 - gameweek_number)


def _weekly_planning_horizon(
    database: HistoricalDatabase,
    rules: object,
    season_code: str,
    gameweek_number: int,
):
    return recommend_planning_horizon(
        database,
        season_code=season_code,
        start_gameweek=gameweek_number,
        base_horizon_gameweeks=WEEKLY_PLANNING_HORIZON_GAMEWEEKS,
        chip_expiry_gameweek=rules.chips.first_set_expiry_gameweek,
    )


def _load_future_transfer_needs() -> dict[int, float] | None:
    if not TRANSFER_POLICY_PATH.exists():
        return None
    raw = json.loads(TRANSFER_POLICY_PATH.read_text(encoding="utf-8"))
    if raw.get("qualified") is not True:
        return None
    raw_distribution = raw.get("future_transfer_need_distribution")
    if not isinstance(raw_distribution, dict):
        return None
    distribution = {
        int(count): float(probability)
        for count, probability in raw_distribution.items()
    }
    if not distribution or abs(sum(distribution.values()) - 1.0) > 1e-6:
        return None
    return distribution


def main() -> None:
    st.set_page_config(
        page_title="FPL Decision Engine · My Team",
        page_icon="⚽",
        layout="wide",
    )
    st.title("My Team")
    st.caption(
        "A private, manual snapshot of your real FPL squad. "
        "Nothing here logs in to or changes your FPL account."
    )

    rules = load_season_rules(RULES_PATH)
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with HistoricalDatabase(DATABASE_PATH) as database:
        database.initialise()
        _sidebar(database)
        seasons = database.connection.execute(
            "SELECT code, name FROM seasons ORDER BY code DESC"
        ).fetchall()
        if not seasons:
            st.info("Collect public FPL data first, then return here to enter your team.")
            _refresh_button(database, "2026-27", "2026/27")
            return

        season_code = st.selectbox(
            "Season",
            [row["code"] for row in seasons],
            index=0,
        )
        gameweeks = database.connection.execute(
            """
            SELECT gameweeks.number, gameweeks.deadline_time
            FROM gameweeks
            JOIN seasons ON seasons.id = gameweeks.season_id
            WHERE seasons.code = ?
            ORDER BY gameweeks.number
            """,
            (season_code,),
        ).fetchall()
        if not gameweeks:
            st.warning("The selected season has no Gameweeks.")
            return
        upcoming_index = next(
            (
                index
                for index, row in enumerate(gameweeks)
                if row["deadline_time"]
                and datetime.fromisoformat(row["deadline_time"].replace("Z", "+00:00"))
                > datetime.now(UTC)
            ),
            len(gameweeks) - 1,
        )
        gameweek_number = st.selectbox(
            "Gameweek",
            [row["number"] for row in gameweeks],
            index=upcoming_index,
            format_func=lambda value: f"Gameweek {value}",
        )
        deadline = next(
            row["deadline_time"] for row in gameweeks if row["number"] == gameweek_number
        )
        st.caption(f"State applies to GW{gameweek_number} · deadline {deadline}")

        repository = ManagerStateRepository(database, rules)
        available = repository.available_players(season_code, gameweek_number)
        if not available:
            st.warning(
                "No player snapshot is available for this Gameweek. "
                "Refresh public data before entering the squad."
            )
            _refresh_button(database, season_code, season_code.replace("-", "/"))
            return

        latest = repository.latest(season_code, gameweek_number)
        tabs = st.tabs(
            (
                "Team editor",
                "Saved team",
                "Projections",
                "Optimal XI",
                "Full squad",
                "Preseason",
                "Transfers",
                "Chips",
                "Weekly cycle",
                "Data health",
            )
        )
        with tabs[0]:
            _editor(repository, available, latest, season_code, gameweek_number)
        with tabs[1]:
            _saved_team(latest, available)
        with tabs[2]:
            _projection_explorer(
                database,
                rules,
                available,
                season_code,
                gameweek_number,
            )
        with tabs[3]:
            _starting_xi_explorer(
                database,
                rules,
                available,
                season_code,
                gameweek_number,
            )
        with tabs[4]:
            _full_squad_explorer(
                database,
                rules,
                season_code,
                gameweek_number,
            )
        with tabs[5]:
            _preseason_decision(season_code, gameweek_number)
        with tabs[6]:
            _transfer_explorer(
                database,
                rules,
                latest,
                season_code,
                gameweek_number,
            )
        with tabs[7]:
            _chip_explorer(
                database,
                rules,
                latest,
                season_code,
                gameweek_number,
            )
        with tabs[8]:
            _weekly_cycle(
                database,
                rules,
                latest,
                available,
                season_code,
                gameweek_number,
            )
        with tabs[9]:
            _data_health(database, season_code, gameweek_number, deadline)


def _editor(
    repository: ManagerStateRepository,
    available: list[dict],
    latest: object,
    season_code: str,
    gameweek_number: int,
) -> None:
    player_lookup = {row["source_player_id"]: row for row in available}
    defaults = (
        [] if latest is None else [entry.source_player_id for entry in latest.snapshot.entries]
    )
    selected_ids = st.multiselect(
        "Select exactly 15 players",
        options=list(player_lookup),
        default=defaults,
        max_selections=15,
        format_func=lambda source_id: (
            f"{player_lookup[source_id]['web_name']} · "
            f"{player_lookup[source_id]['team_short_name']} · "
            f"{player_lookup[source_id]['position']} · "
            f"£{player_lookup[source_id]['price_tenths'] / 10:.1f}m"
        ),
    )
    st.caption(f"{len(selected_ids)} of 15 selected")
    if len(selected_ids) != 15:
        return

    old_entries = (
        {}
        if latest is None
        else {entry.source_player_id: entry for entry in latest.snapshot.entries}
    )
    rows = []
    default_starters = set(selected_ids[:11])
    for source_id in selected_ids:
        player = player_lookup[source_id]
        previous = old_entries.get(source_id)
        rows.append(
            {
                "Player ID": source_id,
                "Player": player["web_name"],
                "Team": player["team_short_name"],
                "Position": player["position"],
                "Current £m": player["price_tenths"] / 10,
                "Purchase £m": (
                    player["price_tenths"] / 10
                    if previous is None
                    else previous.purchase_price_tenths / 10
                ),
                "Selling £m": (
                    player["price_tenths"] / 10
                    if previous is None
                    else previous.selling_price_tenths / 10
                ),
                "Starter": (
                    source_id in default_starters if previous is None else previous.is_starter
                ),
                "Bench order": (None if previous is None else previous.bench_order),
            }
        )
    st.write("Set the XI, substitute goalkeeper as bench 1, then outfield priority 2–4.")
    edited = st.data_editor(
        pd.DataFrame(rows),
        hide_index=True,
        width="stretch",
        disabled=("Player ID", "Player", "Team", "Position", "Current £m"),
        column_config={
            "Purchase £m": st.column_config.NumberColumn(
                min_value=3.0, max_value=20.0, step=0.1, format="£%.1f"
            ),
            "Selling £m": st.column_config.NumberColumn(
                min_value=3.0, max_value=20.0, step=0.1, format="£%.1f"
            ),
            "Bench order": st.column_config.NumberColumn(min_value=1, max_value=4, step=1),
        },
        key=f"squad-grid-{season_code}-{gameweek_number}",
    )
    edited_rows = edited.to_dict("records")
    starter_ids = [str(row["Player ID"]) for row in edited_rows if bool(row["Starter"])]
    role_options = ["", *selected_ids]
    old_snapshot = None if latest is None else latest.snapshot
    captain_index = _option_index(
        role_options,
        None if old_snapshot is None else old_snapshot.captain_source_player_id,
    )
    vice_index = _option_index(
        role_options,
        None if old_snapshot is None else old_snapshot.vice_captain_source_player_id,
    )

    left, middle, right = st.columns(3)
    with left:
        bank = st.number_input(
            "Money in bank (£m)",
            min_value=0.0,
            max_value=20.0,
            step=0.1,
            value=0.0 if old_snapshot is None else old_snapshot.bank_tenths / 10,
        )
        free_transfers = st.number_input(
            "Free transfers",
            min_value=0,
            max_value=repository.rules.transfers.maximum_free_transfers,
            step=1,
            value=1 if old_snapshot is None else old_snapshot.free_transfers,
        )
    with middle:
        captain = st.selectbox(
            "Captain",
            role_options,
            index=captain_index,
            format_func=lambda value: (
                "Select captain" if not value else player_lookup[value]["web_name"]
            ),
        )
        vice = st.selectbox(
            "Vice-captain",
            role_options,
            index=vice_index,
            format_func=lambda value: (
                "Select vice-captain" if not value else player_lookup[value]["web_name"]
            ),
        )
    with right:
        chips = {}
        for chip in repository.rules.chips.names:
            chips[chip] = st.number_input(
                chip.replace("_", " ").title(),
                min_value=0,
                max_value=repository.rules.chips.sets_per_season,
                value=(
                    repository.rules.chips.sets_per_season
                    if old_snapshot is None
                    else old_snapshot.remaining_chips[chip]
                ),
                step=1,
            )
    note = st.text_input("Optional note")
    if st.button("Validate and save snapshot", type="primary"):
        entries = tuple(
            ManagerSquadEntry(
                source_player_id=str(row["Player ID"]),
                purchase_price_tenths=round(float(row["Purchase £m"]) * 10),
                selling_price_tenths=round(float(row["Selling £m"]) * 10),
                is_starter=bool(row["Starter"]),
                bench_order=(
                    None
                    if bool(row["Starter"]) or pd.isna(row["Bench order"])
                    else int(row["Bench order"])
                ),
            )
            for row in edited_rows
        )
        ingestion = repository.database.connection.execute(
            """
            SELECT id FROM ingestion_runs
            WHERE status = 'completed'
            ORDER BY retrieved_at DESC, id DESC LIMIT 1
            """
        ).fetchone()
        try:
            snapshot_id = repository.save(
                ManagerSnapshot(
                    season_code=season_code,
                    gameweek_number=gameweek_number,
                    captured_at=datetime.now(UTC),
                    bank_tenths=round(bank * 10),
                    free_transfers=int(free_transfers),
                    remaining_chips={key: int(value) for key, value in chips.items()},
                    entries=entries,
                    captain_source_player_id=captain or None,
                    vice_captain_source_player_id=vice or None,
                    data_ingestion_run_id=None if ingestion is None else ingestion["id"],
                    note=note or None,
                )
            )
        except ManagerStateError as error:
            st.error("Please fix these issues:\n\n- " + "\n- ".join(error.messages))
        else:
            st.success(f"Saved manager snapshot {snapshot_id}.")
            if len(starter_ids) == 11:
                entry_view = [
                    {
                        "source_player_id": entry.source_player_id,
                        "is_starter": entry.is_starter,
                        "bench_order": entry.bench_order,
                    }
                    for entry in entries
                ]
                st.markdown(
                    pitch_html(entry_view, player_lookup, captain or None, vice or None),
                    unsafe_allow_html=True,
                )


def _saved_team(latest: object, available: list[dict]) -> None:
    if latest is None:
        st.info("No saved squad exists for this Gameweek.")
        return
    player_lookup = {row["source_player_id"]: row for row in available}
    snapshot = latest.snapshot
    entries = [
        {
            "source_player_id": entry.source_player_id,
            "is_starter": entry.is_starter,
            "bench_order": entry.bench_order,
        }
        for entry in snapshot.entries
    ]
    top = st.columns(4)
    top[0].metric("Bank", f"£{snapshot.bank_tenths / 10:.1f}m")
    top[1].metric("Free transfers", snapshot.free_transfers)
    top[2].metric("Gameweek", snapshot.gameweek_number)
    top[3].metric("Saved", snapshot.captured_at.strftime("%d %b %H:%M UTC"))
    st.markdown(
        pitch_html(
            entries,
            player_lookup,
            snapshot.captain_source_player_id,
            snapshot.vice_captain_source_player_id,
        ),
        unsafe_allow_html=True,
    )


def _projection_explorer(
    database: HistoricalDatabase,
    rules: object,
    available: list[dict],
    season_code: str,
    gameweek_number: int,
) -> None:
    st.subheader("Eight-Gameweek baseline")
    st.caption(
        "Transparent per-90 rates, position shrinkage, current availability, "
        "team-strength priors and fixture location. Every run is versioned."
    )
    horizon = st.slider("Projection horizon", 1, 8, 8)
    player_lookup = {row["source_player_id"]: row for row in available}
    override_key = f"projection-overrides-{season_code}-{gameweek_number}"
    if override_key not in st.session_state:
        st.session_state[override_key] = []
    with st.expander("Expected-minutes overrides"):
        override_player = st.selectbox(
            "Player to override",
            ["", *player_lookup],
            format_func=lambda value: (
                "Select player" if not value else player_lookup[value]["web_name"]
            ),
        )
        override_gameweek = st.number_input(
            "Override Gameweek",
            min_value=gameweek_number,
            max_value=min(38, gameweek_number + horizon - 1),
            value=gameweek_number,
            step=1,
        )
        override_minutes = st.number_input(
            "Expected minutes",
            min_value=0.0,
            max_value=180.0,
            value=60.0,
            step=5.0,
        )
        override_rationale = st.text_input("Override rationale")
        add_col, clear_col = st.columns(2)
        if add_col.button("Add override"):
            if not override_player or not override_rationale.strip():
                st.error("Choose a player and record a rationale.")
            else:
                new_override = {
                    "source_player_id": override_player,
                    "gameweek_number": int(override_gameweek),
                    "expected_minutes": float(override_minutes),
                    "rationale": override_rationale.strip(),
                }
                existing = [
                    item
                    for item in st.session_state[override_key]
                    if (
                        item["source_player_id"],
                        item["gameweek_number"],
                    )
                    != (override_player, int(override_gameweek))
                ]
                st.session_state[override_key] = [*existing, new_override]
                st.rerun()
        if clear_col.button("Clear overrides"):
            st.session_state[override_key] = []
            st.rerun()
        if st.session_state[override_key]:
            st.dataframe(st.session_state[override_key], hide_index=True)

    if st.button("Generate projection baseline", type="primary"):
        with st.spinner("Calculating player projections…"):
            result = RatesProjectionModel(database, rules).project(
                season_code=season_code,
                start_gameweek=gameweek_number,
                horizon_gameweeks=horizon,
                overrides=tuple(
                    ProjectionOverride(**item) for item in st.session_state[override_key]
                ),
            )
        st.success(
            f"Projection run {result.projection_run_id} saved with model {result.model_version}."
        )

    latest_run = database.connection.execute(
        """
        SELECT projection_runs.id, projection_runs.model_version,
               projection_runs.generated_at, projection_runs.horizon_gameweeks,
               projection_runs.assumptions_json
        FROM projection_runs
        JOIN seasons ON seasons.id = projection_runs.season_id
        WHERE seasons.code = ? AND projection_runs.start_gameweek = ?
        ORDER BY projection_runs.generated_at DESC, projection_runs.id DESC
        LIMIT 1
        """,
        (season_code, gameweek_number),
    ).fetchone()
    if latest_run is None:
        st.info("Generate the first baseline to explore player forecasts.")
        return
    st.caption(
        f"Run {latest_run['id']} · {latest_run['model_version']} · "
        f"{latest_run['generated_at']} · {latest_run['horizon_gameweeks']} GWs"
    )
    totals = database.connection.execute(
        """
        SELECT ps.source_player_id, players.web_name,
               teams.short_name AS team, ps.position,
               ROUND(SUM(projections.expected_minutes), 1) AS expected_minutes,
               ROUND(SUM(projections.expected_points), 2) AS expected_points,
               ROUND(SUM(projections.uncertainty), 2) AS uncertainty
        FROM player_gameweek_projections projections
        JOIN player_seasons ps ON ps.id = projections.player_season_id
        JOIN players ON players.id = ps.player_id
        JOIN teams ON teams.id = ps.team_id
        WHERE projections.projection_run_id = ?
        GROUP BY ps.id
        ORDER BY expected_points DESC
        """,
        (latest_run["id"],),
    ).fetchall()
    st.dataframe(
        pd.DataFrame([dict(row) for row in totals]),
        hide_index=True,
        width="stretch",
    )
    selected = st.selectbox(
        "Component detail",
        [row["source_player_id"] for row in totals],
        format_func=lambda value: player_lookup[value]["web_name"],
    )
    details = database.connection.execute(
        """
        SELECT gameweek_number, expected_minutes, appearance_points,
               goal_points, assist_points, clean_sheet_points, save_points,
               defensive_contribution_points, bonus_points, deduction_points,
               expected_points, uncertainty, override_rationale
        FROM player_gameweek_projections projections
        JOIN player_seasons ps ON ps.id = projections.player_season_id
        WHERE projections.projection_run_id = ? AND ps.source_player_id = ?
        ORDER BY gameweek_number
        """,
        (latest_run["id"], selected),
    ).fetchall()
    st.dataframe(
        pd.DataFrame([dict(row) for row in details]),
        hide_index=True,
        width="stretch",
    )
    with st.expander("Model assumptions and team strengths"):
        st.json(latest_run["assumptions_json"])


def _load_optimisation_candidates(
    database: HistoricalDatabase,
    projection_run_id: int,
) -> tuple[CandidatePlayer, ...]:
    run = database.connection.execute(
        """
        SELECT generated_at, source_ingestion_run_id
        FROM projection_runs WHERE id = ?
        """,
        (projection_run_id,),
    ).fetchone()
    if run is None:
        raise ValueError(f"Projection run {projection_run_id} is unavailable")
    rows = database.connection.execute(
        """
        WITH ranked_state AS (
            SELECT observations.player_season_id,
                   observations.price_tenths, observations.team_id,
                   ROW_NUMBER() OVER (
                       PARTITION BY observations.player_season_id
                       ORDER BY datetime(ingestion_runs.retrieved_at) DESC,
                                observations.observed_at DESC,
                                observations.observed_on DESC,
                                observations.id DESC
                   ) AS state_rank
            FROM player_gameweek_observations observations
            JOIN ingestion_runs
              ON ingestion_runs.id = observations.provenance_run_id
            WHERE ingestion_runs.status = 'completed'
              AND (
                  (? IS NOT NULL AND ingestion_runs.id <= ?)
                  OR (
                      ? IS NULL
                      AND datetime(ingestion_runs.retrieved_at)
                          <= datetime(?)
                  )
              )
        )
        SELECT ps.source_player_id, players.web_name,
               teams.source_team_id, teams.short_name, ps.position,
               ranked_state.price_tenths, projections.gameweek_number,
               projections.expected_points, projections.expected_minutes,
               projections.appearance_probability,
               projections.sixty_probability, projections.uncertainty
        FROM player_gameweek_projections projections
        JOIN player_seasons ps ON ps.id = projections.player_season_id
        JOIN players ON players.id = ps.player_id
        JOIN ranked_state
          ON ranked_state.player_season_id = ps.id
         AND ranked_state.state_rank = 1
        JOIN teams
          ON teams.id = COALESCE(ranked_state.team_id, ps.team_id)
        WHERE projections.projection_run_id = ?
        ORDER BY ps.source_player_id, projections.gameweek_number
        """,
        (
            run["source_ingestion_run_id"],
            run["source_ingestion_run_id"],
            run["source_ingestion_run_id"],
            run["generated_at"],
            projection_run_id,
        ),
    ).fetchall()
    grouped: dict[str, dict[str, object]] = {}
    for row in rows:
        player_id = str(row["source_player_id"])
        item = grouped.setdefault(
            player_id,
            {
                "web_name": row["web_name"],
                "team_id": str(row["source_team_id"]),
                "team_short_name": row["short_name"],
                "position": Position(row["position"]),
                "price_tenths": int(row["price_tenths"]),
                "values": [],
                "uncertainty_total": 0.0,
            },
        )
        appearance_probability = float(row["appearance_probability"])
        # Migrated legacy runs have no persisted probabilities. Keep them
        # viewable while every newly generated run uses the model outputs.
        if appearance_probability == 0 and float(row["expected_minutes"]) > 0:
            appearance_probability = min(1.0, float(row["expected_minutes"]) / 60.0)
        item["values"].append(
            GameweekPlayerValue(
                gameweek_number=int(row["gameweek_number"]),
                expected_points=float(row["expected_points"]),
                appearance_probability=appearance_probability,
                sixty_probability=float(row["sixty_probability"]),
            )
        )
        item["uncertainty_total"] = float(item["uncertainty_total"]) + float(row["uncertainty"])
    candidates = []
    for player_id, item in grouped.items():
        values = tuple(item["values"])
        first = values[0]
        candidates.append(
            CandidatePlayer(
                source_player_id=player_id,
                web_name=str(item["web_name"]),
                team_id=str(item["team_id"]),
                team_short_name=str(item["team_short_name"]),
                position=item["position"],
                price_tenths=int(item["price_tenths"]),
                expected_points=sum(value.expected_points for value in values),
                gameweek_expected_points=first.expected_points,
                appearance_probability=first.appearance_probability,
                uncertainty=float(item["uncertainty_total"]),
                gameweek_values=values,
            )
        )
    return tuple(sorted(candidates, key=lambda player: player.source_player_id))


def _starting_xi_explorer(
    database: HistoricalDatabase,
    rules: object,
    available: list[dict],
    season_code: str,
    gameweek_number: int,
) -> None:
    st.subheader("Best projected starting XI")
    st.caption(
        "A solver-proven optimum across all available players, subject to "
        "budget, position and three-per-club constraints."
    )
    latest_run = select_production_projection_run(
        database,
        season_code=season_code,
        start_gameweek=gameweek_number,
        minimum_horizon_gameweeks=1,
    )
    if latest_run is None:
        st.info("Generate a projection baseline before optimising an XI.")
        return
    budget = st.number_input(
        "XI budget (£m)",
        min_value=40.0,
        max_value=100.0,
        value=82.0,
        step=0.5,
    )
    if not st.button("Optimise starting XI"):
        return
    candidates = _load_optimisation_candidates(database, latest_run.run_id)
    try:
        result = optimise_starting_xi(
            candidates,
            budget_tenths=round(budget * 10),
            rules=rules,
        )
    except (OptimisationError, ValueError) as error:
        st.error(str(error))
        return
    metrics = st.columns(3)
    metrics[0].metric("Projected points", f"{result.expected_points:.1f}")
    metrics[1].metric("XI cost", f"£{result.total_cost_tenths / 10:.1f}m")
    metrics[2].metric("Proof status", result.solver_status)
    lookup = {
        player.source_player_id: {
            "web_name": player.web_name,
            "team_short_name": player.team_short_name,
            "position": player.position.value,
            "price_tenths": player.price_tenths,
        }
        for player in result.players
    }
    entry_rows = [
        {
            "source_player_id": player.source_player_id,
            "is_starter": True,
            "bench_order": None,
        }
        for player in result.players
    ]
    st.markdown(
        pitch_html(entry_rows, lookup, None, None),
        unsafe_allow_html=True,
    )
    st.success(result.proof)
    st.write("Highest-projected players outside this XI")
    st.dataframe(
        pd.DataFrame(
            {
                "Player": player.web_name,
                "Team": player.team_short_name,
                "Position": player.position.value,
                "Price £m": player.price_tenths / 10,
                "Projected points": player.expected_points,
            }
            for player in result.near_selected
        ),
        hide_index=True,
        width="stretch",
    )


def _full_squad_explorer(
    database: HistoricalDatabase,
    rules: object,
    season_code: str,
    gameweek_number: int,
) -> None:
    st.subheader("Full squad and weekly lineup")
    required_horizon = _remaining_horizon(
        gameweek_number,
        OPENING_SQUAD_HORIZON_GAMEWEEKS,
    )
    latest_run = select_production_projection_run(
        database,
        season_code=season_code,
        start_gameweek=gameweek_number,
        minimum_horizon_gameweeks=required_horizon,
    )
    if latest_run is None:
        st.info(
            "Generate a production projection covering at least "
            f"{required_horizon} Gameweeks before selecting a full squad."
        )
        return
    st.caption(
        f"Production run {latest_run.run_id} · {latest_run.model_version} · "
        f"{latest_run.horizon_gameweeks} Gameweeks"
    )
    if not st.button("Optimise £100m squad"):
        return
    candidates = appearance_qualified_candidates(
        _load_optimisation_candidates(database, latest_run.run_id)
    )
    st.caption(
        "Opening robustness guardrail: every eligible player has at least "
        f"{DEFAULT_OPENING_MINIMUM_MEAN_APPEARANCE:.0%} mean projected "
        "availability across the horizon."
    )
    try:
        recommendation = optimise_opening_squads(
            candidates,
            budget_tenths=rules.squad.budget_tenths,
            rules=rules,
            alternative_count=2,
            candidate_pool_size=8,
        )
    except (OptimisationError, ValueError) as error:
        st.error(str(error))
        return
    result = recommendation.primary
    metrics = st.columns(4)
    metrics[0].metric("GW expected", f"{result.gameweek_expected_points:.1f}")
    metrics[1].metric("Bench contribution", f"{result.expected_bench_contribution:.2f}")
    metrics[2].metric("Captain contribution", f"{result.expected_captain_contribution:.2f}")
    metrics[3].metric("Cost", f"£{result.total_cost_tenths / 10:.1f}m")
    selected_lookup = {
        player.source_player_id: {
            "web_name": player.web_name,
            "team_short_name": player.team_short_name,
            "position": player.position.value,
            "price_tenths": player.price_tenths,
        }
        for player in result.players
    }
    bench_order = {
        player_id: index for index, player_id in enumerate(result.bench_player_ids, start=1)
    }
    entries = [
        {
            "source_player_id": player.source_player_id,
            "is_starter": player.source_player_id in result.starting_player_ids,
            "bench_order": bench_order.get(player.source_player_id),
        }
        for player in result.players
    ]
    st.markdown(
        pitch_html(
            entries,
            selected_lookup,
            result.captain_id,
            result.vice_captain_id,
        ),
        unsafe_allow_html=True,
    )
    st.success(result.proof)
    st.write("Why this opening squad")
    st.write(recommendation.objective)
    for assumption in recommendation.assumptions:
        st.caption(f"• {assumption}")
    st.write("Closest alternative squad structures")
    st.dataframe(
        pd.DataFrame(
            {
                "Alternative": index,
                "Projected horizon points": alternative.horizon_expected_points,
                "GW expected points": alternative.gameweek_expected_points,
                "Cost £m": alternative.total_cost_tenths / 10,
                "Changed players": 15
                - len(
                    {player.source_player_id for player in result.players}
                    & {player.source_player_id for player in alternative.players}
                ),
            }
            for index, alternative in enumerate(recommendation.alternatives, start=1)
        ),
        hide_index=True,
        width="stretch",
    )
    st.write("Re-run triggers")
    for trigger in recommendation.transfer_triggers:
        st.warning(trigger)


def _preseason_decision(season_code: str, gameweek_number: int) -> None:
    """The validated preseason team-strength model and the squad it picks.

    Reads the written validation artifact rather than re-running it. The
    validation replays four historical seasons and solves dozens of squads;
    doing that inside a page render would make the app unusable, and a
    decision this consequential should be a deliberate command anyway.
    """

    st.subheader("Preseason team strength and opening squad")
    validation = load_preseason_validation(season_code)
    if validation is None:
        st.warning(
            "No preseason team-strength validation has been run for "
            f"{season_code}. Until one has, the opening squad rests on the "
            "flat preseason model, which gives every club the same strength "
            "before GW1 — so it cannot tell a trip to the champions from a "
            "home game against a promoted side."
        )
        st.code(
            f"fpl-history validate-preseason-strength {season_code} "
            "--horizon 8 --candidate-pool-size 8 "
            "--output data/models/"
            f"preseason-strength-validation-{season_code}.json",
            language="bash",
        )
        return

    validated = preseason_model_is_validated(validation, season_code=season_code)
    gate = (validation.get("validation") or {}).get("decision_gate") or {}
    selected = validation.get("selected_model") or {}
    live = validation.get("live_projection") or {}

    if not validated:
        st.error(
            "The carry-forward candidate did not pass its decision gate"
            + (
                f" ({', '.join(gate.get('failed_criteria') or [])})."
                if gate.get("failed_criteria")
                else "."
            )
            + " The squad below is a robustness comparison, not a validated "
            "recommendation."
        )
    elif gameweek_number != 1:
        st.info(
            "This is a preseason decision. From GW2 onward the ordinary "
            "in-season projection governs, and this tab is history."
        )

    st.markdown("#### Validated preseason model")
    columns = st.columns(4)
    columns[0].metric(
        "Model", str(selected.get("label") or "unknown").replace("_", " ")
    )
    columns[1].metric("Validation", "passed" if validated else "failed")
    columns[2].metric(
        "Projection run", str(live.get("projection_run_id") or "—")
    )
    columns[3].metric("Horizon", f"{validation.get('horizon_gameweeks')} GW")
    st.caption(
        f"Model version `{selected.get('model_version')}` · scope "
        f"{selected.get('scope')} · generated "
        f"{validation.get('generated_at')}"
    )
    usable = (validation.get("validation") or {}).get("usable_transitions") or []
    st.caption("Historical transitions used: " + (", ".join(usable) or "none"))

    aggregate = (validation.get("validation") or {}).get("aggregate") or {}
    headline = [
        {
            "model": label,
            "goals RMSE": (aggregate[label]["early_team_goals"] or {}).get(
                "goals_rmse"
            ),
            "goals MAE": (aggregate[label]["early_team_goals"] or {}).get(
                "goals_mae"
            ),
            "goals bias": (aggregate[label]["early_team_goals"] or {}).get(
                "goals_bias"
            ),
            "clean-sheet Brier": (
                aggregate[label]["early_team_goals"] or {}
            ).get("clean_sheet_brier"),
            "mean realised GW1-8 points": aggregate[label].get(
                "mean_opening_squad_realised_points"
            ),
        }
        for label in ("flat", "carry_forward", "opponent_adjusted")
        if isinstance(aggregate.get(label), dict)
    ]
    if headline:
        st.markdown("**Flat versus carry-forward, pooled over GW1–GW8**")
        st.dataframe(pd.DataFrame(headline), width="stretch", hide_index=True)
    if gate.get("criteria"):
        with st.expander("Decision gate", expanded=not validated):
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "criterion": entry["criterion"],
                            "passed": entry["passed"],
                            "what it checks": entry["description"],
                        }
                        for entry in gate["criteria"]
                    ]
                ),
                width="stretch",
                hide_index=True,
            )
            st.caption(gate.get("neutral_definition", ""))

    squad = validation.get("revised_squad") or {}
    if not squad:
        st.info("The artifact holds no revised squad.")
        return

    st.markdown("#### Revised opening squad")
    label = "Validated opening squad" if validated else "Unvalidated squad"
    st.caption(label)
    metrics = st.columns(4)
    metrics[0].metric("Cost", f"£{squad.get('total_cost_tenths', 0) / 10:.1f}m")
    metrics[1].metric("GW1 expected", squad.get("gameweek_expected_points"))
    metrics[2].metric("Eight-GW value", squad.get("decision_value"))
    metrics[3].metric(
        "Bench contribution",
        squad.get("horizon_expected_bench_contribution"),
    )
    players = squad.get("players") or []
    by_id = {player["source_player_id"]: player for player in players}
    captain = by_id.get(squad.get("captain_id"), {})
    vice = by_id.get(squad.get("vice_captain_id"), {})
    st.caption(
        f"Captain {captain.get('web_name', '—')} · "
        f"vice-captain {vice.get('web_name', '—')}"
    )
    robustness = (validation.get("robustness") or {}).get("classification") or {}
    frame = pd.DataFrame(
        [
            {
                "player": player["web_name"],
                "club": player["team"],
                "pos": player["position"],
                "price": player["price_tenths"] / 10,
                "GW1-8 xP": player["horizon_expected_points"],
                "role": (
                    "XI"
                    if player["starts_gameweek"]
                    else f"bench {player['bench_rank']}"
                )
                + (" (C)" if player["captain"] else "")
                + (" (V)" if player["vice_captain"] else ""),
                "robustness": robustness.get(
                    player["source_player_id"], "not tested"
                ),
            }
            for player in players
        ]
    )
    st.dataframe(frame, width="stretch", hide_index=True)

    comparison = validation.get("flat_comparison") or {}
    changed = comparison.get("changed_players") or []
    if changed:
        st.markdown("#### Why this differs from the flat model")
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "player": entry["web_name"],
                        "club": entry["club"],
                        "pos": entry["position"],
                        "GW1 fixture": _fixture_label(entry),
                        "flat GW1-8 xP": (
                            entry["models"].get("flat") or {}
                        ).get("horizon_expected_points"),
                        "carry-forward GW1-8 xP": (
                            entry["models"].get("carry_forward") or {}
                        ).get("horizon_expected_points"),
                        "change": entry.get("horizon_points_change"),
                        "in flat squad": (
                            entry["models"].get("flat") or {}
                        ).get("in_squad"),
                        "in revised squad": (
                            entry["models"].get("carry_forward") or {}
                        ).get("in_squad"),
                        "attributed to": entry.get("change_attributed_to"),
                        "robustness": robustness.get(
                            entry["source_player_id"], "not tested"
                        ),
                    }
                    for entry in changed
                ]
            ),
            width="stretch",
            hide_index=True,
        )
    separation = comparison.get("team_strength_separation") or {}
    if separation:
        with st.expander("Team-strength change"):
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "model": name,
                            "distinct attack multipliers": entry.get(
                                "distinct_attack_multipliers"
                            ),
                            "attack spread": entry.get("attack_spread"),
                            "established minus promoted": entry.get(
                                "established_minus_promoted_attack"
                            ),
                        }
                        for name, entry in separation.items()
                    ]
                ),
                width="stretch",
                hide_index=True,
            )
    focus = comparison.get("focus_players") or []
    if focus:
        with st.expander("Truffert, O'Shea and Muñoz"):
            for entry in focus:
                flat = entry["models"].get("flat") or {}
                carry = entry["models"].get("carry_forward") or {}
                st.markdown(
                    f"**{entry['web_name']}** ({entry['club']}, "
                    f"{entry['position']}) — {_fixture_label(entry)}. "
                    f"Opponent expected goals {flat.get('opponent_expected_goals')} "
                    f"→ {carry.get('opponent_expected_goals')}; clean sheet "
                    f"{flat.get('clean_sheet_probability')} → "
                    f"{carry.get('clean_sheet_probability')}; GW1–8 "
                    f"{flat.get('horizon_expected_points')} → "
                    f"{carry.get('horizon_expected_points')}. "
                    f"In squad {flat.get('in_squad')} → {carry.get('in_squad')}."
                )

    alternatives = validation.get("alternatives") or []
    if alternatives:
        st.markdown("#### Near-optimal alternatives")
        for index, alternative in enumerate(alternatives, start=1):
            gap = alternative.get("decision_value_gap")
            with st.expander(
                f"Alternative {index} — {gap} points below the primary"
            ):
                st.dataframe(
                    pd.DataFrame(
                        [
                            {
                                "player": player["web_name"],
                                "club": player["team"],
                                "pos": player["position"],
                                "price": player["price_tenths"] / 10,
                                "role": (
                                    "XI"
                                    if player["starts_gameweek"]
                                    else f"bench {player['bench_rank']}"
                                ),
                            }
                            for player in alternative.get("players", [])
                        ]
                    ),
                    width="stretch",
                    hide_index=True,
                )

    warnings = validation.get("warnings") or []
    if warnings:
        with st.expander("Warnings and exclusions"):
            for warning in warnings:
                st.write(f"- {warning}")


def _fixture_label(entry: dict) -> str:
    values = entry.get("models") or {}
    side = values.get("carry_forward") or values.get("flat") or {}
    opponent = side.get("opponent")
    venue = side.get("venue")
    if not opponent:
        return "—"
    return f"{'home to' if venue == 'home' else 'away at'} {opponent}"


def _transfer_explorer(
    database: HistoricalDatabase,
    rules: object,
    latest: object,
    season_code: str,
    gameweek_number: int,
) -> None:
    st.subheader("Transfer routes")
    if latest is None:
        st.info("Save your current squad before comparing transfer routes.")
        return
    planning_horizon = _weekly_planning_horizon(
        database,
        rules,
        season_code,
        gameweek_number,
    )
    required_horizon = planning_horizon.required_horizon_gameweeks
    if planning_horizon.reasons:
        st.caption("Planning horizon extended: " + "; ".join(planning_horizon.reasons))
    latest_run = select_production_projection_run(
        database,
        season_code=season_code,
        start_gameweek=gameweek_number,
        minimum_horizon_gameweeks=required_horizon,
    )
    if latest_run is None:
        st.info(
            "Generate a production projection covering at least "
            f"{required_horizon} Gameweeks before comparing transfers."
        )
        return
    if not st.button("Compare roll and configured multi-transfer routes"):
        return
    candidates = _load_optimisation_candidates(database, latest_run.run_id)
    snapshot = latest.snapshot
    current = CurrentSquad(
        player_ids=frozenset(entry.source_player_id for entry in snapshot.entries),
        selling_prices_tenths={
            entry.source_player_id: entry.selling_price_tenths for entry in snapshot.entries
        },
        bank_tenths=snapshot.bank_tenths,
        free_transfers=snapshot.free_transfers,
    )
    try:
        future_transfer_needs = _load_future_transfer_needs()
        recommendation = recommend_transfers(
            candidates,
            current,
            rules=rules,
            candidate_pool_size=4,
            future_transfer_needs=future_transfer_needs,
        )
    except (OptimisationError, ValueError) as error:
        st.error(str(error))
        return
    st.caption(recommendation.search_scope)
    if future_transfer_needs is None:
        st.warning(
            "Saved-transfer option value is not calibrated yet; small positive "
            "transfer gains may be too eager. Run the transfer-policy evaluation."
        )
    st.dataframe(
        pd.DataFrame(
            {
                "Action": route.explanation,
                "Transfers": route.transfer_count,
                "Hit": route.points_hit,
                "Horizon gain": route.horizon_points_gain,
                "Bank £m": route.bank_tenths / 10,
                "Next FT": route.next_free_transfers,
                "Route score": route.route_score,
            }
            for route in recommendation.routes
        ),
        hide_index=True,
        width="stretch",
    )
    st.success(f"Primary recommendation: {recommendation.primary.explanation}")


def _chip_explorer(
    database: HistoricalDatabase,
    rules: object,
    latest: object,
    season_code: str,
    gameweek_number: int,
) -> None:
    st.subheader("Chip comparison")
    if latest is None:
        st.info("Save your current squad before comparing chips.")
        return
    planning_horizon = _weekly_planning_horizon(
        database,
        rules,
        season_code,
        gameweek_number,
    )
    required_horizon = planning_horizon.required_horizon_gameweeks
    latest_run = select_production_projection_run(
        database,
        season_code=season_code,
        start_gameweek=gameweek_number,
        minimum_horizon_gameweeks=required_horizon,
    )
    if latest_run is None:
        st.info(
            "Generate a production projection covering at least "
            f"{required_horizon} Gameweeks before comparing chip timing."
        )
        return
    if planning_horizon.reasons:
        st.caption("Planning horizon extended: " + "; ".join(planning_horizon.reasons))
    snapshot = latest.snapshot
    available_chips = [
        chip_name for chip_name, remaining in snapshot.remaining_chips.items() if remaining > 0
    ]
    if not available_chips:
        st.info("The saved manager state has no chips remaining.")
        return
    chip_name = st.selectbox(
        "Chip",
        available_chips,
        format_func=lambda value: value.replace("_", " ").title(),
    )
    inferred_uses = _inferred_chip_gameweeks(
        database,
        season_code,
        chip_name,
    )
    previous_uses_text = st.text_input(
        "Previous use Gameweeks",
        value=", ".join(str(gameweek) for gameweek in inferred_uses),
        help=(
            "Inferred from saved manager-state changes. Correct this list if "
            "older snapshots are missing."
        ),
    )
    if not st.button("Compare chip timing across the planning horizon"):
        return
    try:
        previous_uses = tuple(
            sorted({int(value.strip()) for value in previous_uses_text.split(",") if value.strip()})
        )
    except ValueError:
        st.error("Previous use Gameweeks must be comma-separated integers.")
        return
    candidates = _load_optimisation_candidates(
        database,
        latest_run.run_id,
    )
    current_ids = frozenset(entry.source_player_id for entry in snapshot.entries)
    budget_tenths = (
        sum(entry.selling_price_tenths for entry in snapshot.entries) + snapshot.bank_tenths
    )
    try:
        candidate_gameweeks = tuple(
            sorted(
                {
                    value.gameweek_number
                    for player in candidates
                    for value in player.gameweek_values
                }
                or {gameweek_number}
            )
        )
        timing = recommend_chip_timing(
            Chip(chip_name),
            candidates,
            candidate_gameweeks=candidate_gameweeks,
            previous_chip_gameweeks=previous_uses,
            budget_tenths=budget_tenths,
            rules=rules,
            current_player_ids=current_ids,
        )
    except (OptimisationError, ValueError) as error:
        st.error(str(error))
        return
    recommendation = timing.recommendation
    st.metric("Recommended Gameweek", f"GW{timing.recommended_gameweek}")
    st.dataframe(
        pd.DataFrame(
            {
                "Gameweek": option.gameweek_number,
                "Gross gain": option.gross_incremental_points,
                "Best later opportunity": option.future_opportunity_cost,
                "Net versus waiting": option.net_value_versus_best_later,
            }
            for option in timing.options
        ),
        hide_index=True,
        width="stretch",
    )
    st.write(timing.explanation)
    if not timing.horizon_reaches_set_expiry:
        st.warning(
            "Do not treat this as a season-wide chip recommendation: later "
            "Gameweeks in the active chip set are outside the projection."
        )
    if recommendation.captain_id is not None:
        captain = next(
            player for player in candidates if player.source_player_id == recommendation.captain_id
        )
        st.caption(f"Captain: {captain.web_name}")
    if recommendation.squad is not None:
        st.dataframe(
            pd.DataFrame(
                {
                    "Player": player.web_name,
                    "Team": player.team_short_name,
                    "Position": player.position.value,
                    "Starter": (
                        player.source_player_id in recommendation.squad.starting_player_ids
                    ),
                }
                for player in recommendation.squad.players
            ),
            hide_index=True,
        width="stretch",
        )


def _inferred_chip_gameweeks(
    database: HistoricalDatabase,
    season_code: str,
    chip_name: str,
) -> tuple[int, ...]:
    rows = database.connection.execute(
        """
        WITH ranked AS (
            SELECT gameweeks.number,
                   manager_snapshots.remaining_chips_json,
                   ROW_NUMBER() OVER (
                       PARTITION BY gameweeks.number
                       ORDER BY datetime(manager_snapshots.captured_at) DESC,
                                manager_snapshots.id DESC
                   ) AS snapshot_rank
            FROM manager_snapshots
            JOIN seasons ON seasons.id = manager_snapshots.season_id
            JOIN gameweeks ON gameweeks.id = manager_snapshots.gameweek_id
            WHERE seasons.code = ?
        )
        SELECT number, remaining_chips_json
        FROM ranked
        WHERE snapshot_rank = 1
        ORDER BY number
        """,
        (season_code,),
    ).fetchall()
    uses = []
    previous_remaining: int | None = None
    for row in rows:
        remaining = int(json.loads(row["remaining_chips_json"]).get(chip_name, 0))
        if previous_remaining is not None and remaining < previous_remaining:
            uses.extend([int(row["number"])] * (previous_remaining - remaining))
        previous_remaining = remaining
    return tuple(uses)


def _weekly_cycle(
    database: HistoricalDatabase,
    rules: object,
    latest: object,
    available: list[dict],
    season_code: str,
    gameweek_number: int,
) -> None:
    st.subheader("Two-pass weekly cycle")
    workflow = WeeklyWorkflowRepository(database)
    player_lookup = {row["source_player_id"]: row for row in available}
    planning_horizon = _weekly_planning_horizon(
        database,
        rules,
        season_code,
        gameweek_number,
    )
    required_horizon = planning_horizon.required_horizon_gameweeks
    if planning_horizon.reasons:
        st.caption("Planning horizon extended: " + "; ".join(planning_horizon.reasons))
    latest_projection = select_production_projection_run(
        database,
        season_code=season_code,
        start_gameweek=gameweek_number,
        minimum_horizon_gameweeks=required_horizon,
    )
    with st.expander("Export decision-focused v3 research package", expanded=False):
        st.caption(
            "The package identifies the selected XI, bench, armbands, projection assumptions, "
            "bounded alternatives and a compact official player directory."
        )
        package_mode = st.selectbox(
            "Research mode", ("preseason", "provisional", "final"), key="v3-package-mode"
        )
        package_window = st.text_input(
            "Research-window start (timezone-aware ISO-8601)",
            value=datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0).isoformat(),
            key="v3-package-window",
        )
        package_limit = st.number_input(
            "Alternatives", min_value=1, max_value=20, value=15, step=1, key="v3-package-limit"
        )
        if latest_projection is None:
            st.info("Generate a projection baseline before exporting a research package.")
        elif st.button("Generate v3 research package"):
            try:
                package = generate_team_news_research_package(
                    database,
                    season_code=season_code,
                    gameweek_number=gameweek_number,
                    projection_run_id=latest_projection.run_id,
                    research_mode=package_mode,
                    research_window_start=package_window,
                    alternatives_limit=int(package_limit),
                )
            except (ValueError, WorkflowError) as error:
                st.error(str(error))
            else:
                st.success(
                    f"Package {package['input_package_id']} generated "
                    f"({len(package['selected_squad'])} selected players, "
                    f"{len(package['alternatives'])} alternatives)."
                )
                st.download_button(
                    "Download research package JSON",
                    data=json.dumps(package, indent=2),
                    file_name=f"team-news-{season_code}-gw{gameweek_number}-v3.json",
                    mime="application/json",
                )
    with st.expander("News evidence and review", expanded=True):
        st.caption(
            "Paste the strict JSON produced by the team-news research prompt. "
            "Every item enters the human review queue before it can affect projections."
        )
        latest_package = database.connection.execute(
            """
            SELECT package_id
            FROM team_news_input_packages
            WHERE season_id = (SELECT id FROM seasons WHERE code = ?)
              AND gameweek_id = (
                  SELECT id FROM gameweeks
                  WHERE season_id = (SELECT id FROM seasons WHERE code = ?)
                    AND number = ?
              )
            ORDER BY created_at DESC, package_id DESC
            LIMIT 1
            """,
            (season_code, season_code, gameweek_number),
        ).fetchone()
        if latest_package is None:
            st.warning(
                "Generate a v3 research package above before importing a result. "
                "The ChatGPT response must preserve that package's exact ID and hash."
            )
        else:
            st.info(
                f"Expected package: {latest_package['package_id']}. "
                "Do not use the example IDs from the schema template."
            )
        structured_payload = st.text_area(
            "Structured team-news JSON",
            value="",
            placeholder=(
                "Paste the complete JSON returned by ChatGPT here, including "
                "input_package_id and input_package_hash."
            ),
            height=180,
        )
        if st.button("Import structured news"):
            try:
                evidence_ids = ingest_structured_news(
                    workflow,
                    season_code=season_code,
                    gameweek_number=gameweek_number,
                    payload=structured_payload,
                )
            except (ValueError, WorkflowError) as error:
                st.error(str(error))
            else:
                st.success(f"Added {len(evidence_ids)} item(s) to the review queue.")
                st.rerun()
        evidence_player = st.selectbox(
            "Affected player",
            ["", *player_lookup],
            format_func=lambda value: (
                "General team evidence" if not value else player_lookup[value]["web_name"]
            ),
            key="evidence-player",
        )
        evidence_type = st.selectbox(
            "Evidence type",
            (
                "injury",
                "suspension",
                "training",
                "manager_quote",
                "predicted_lineup",
                "tactical_role",
                "transfer",
                "other",
            ),
        )
        evidence_summary = st.text_input("Evidence summary")
        evidence_source = st.text_input("Source URL (optional)")
        evidence_confidence = st.selectbox("Confidence", ("low", "medium", "high"), index=1)
        if st.button("Add evidence"):
            try:
                workflow.add_news_evidence(
                    season_code=season_code,
                    gameweek_number=gameweek_number,
                    evidence_type=evidence_type,
                    summary=evidence_summary,
                    confidence=evidence_confidence,
                    evidence_at=datetime.now(UTC),
                    source_player_id=evidence_player or None,
                    source_url=evidence_source or None,
                )
            except WorkflowError as error:
                st.error(str(error))
            else:
                st.success("Evidence added to the review queue.")
                st.rerun()
        evidence_rows = database.connection.execute(
            """
            SELECT news_evidence.id, ps.source_player_id, players.web_name,
                   news_evidence.research_run_id, news_evidence.input_package_id,
                   evidence_type, summary,
                   confidence, review_status, expected_minutes_adjustment,
                   rationale, schema_version, prompt_version, priority,
                   selected_player_status, source_tier, published_at, expires_at,
                   adjustment_support, decision_question
            FROM news_evidence
            JOIN seasons ON seasons.id = news_evidence.season_id
            JOIN gameweeks ON gameweeks.id = news_evidence.gameweek_id
            LEFT JOIN player_seasons ps
              ON ps.id = news_evidence.player_season_id
            LEFT JOIN players ON players.id = ps.player_id
            WHERE seasons.code = ? AND gameweeks.number = ?
            ORDER BY news_evidence.id DESC
            """,
            (season_code, gameweek_number),
        ).fetchall()
        filters = st.multiselect(
            "Review filters",
            (
                "critical players",
                "unreviewed evidence",
                "conflicting evidence",
                "unsupported adjustments",
                "expiring evidence",
            ),
            key="v3-review-filters",
        )
        if filters:
            filtered_rows = []
            now = datetime.now(UTC).isoformat()
            for row in evidence_rows:
                if "critical players" in filters and row["priority"] != "critical":
                    continue
                if "unreviewed evidence" in filters and row["review_status"] != "pending":
                    continue
                if "conflicting evidence" in filters:
                    conflict = database.connection.execute(
                        "SELECT 1 FROM news_evidence WHERE id = ? "
                        "AND COALESCE(conflicting_evidence_json, '[]') <> '[]'",
                        (row["id"],),
                    ).fetchone()
                    if conflict is None:
                        continue
                if (
                    "unsupported adjustments" in filters
                    and row["adjustment_support"] != "unsupported"
                ):
                    continue
                if "expiring evidence" in filters and not row["expires_at"]:
                    continue
                if "expiring evidence" in filters and row["expires_at"] <= now:
                    continue
                filtered_rows.append(row)
            evidence_rows = filtered_rows
        if evidence_rows:
            st.caption(
                "Schema/prompt versions are retained per item. Coverage is separate from "
                "evidence: an empty evidence list does not prove a player was checked."
            )
            st.dataframe(
                pd.DataFrame([dict(row) for row in evidence_rows]),
                hide_index=True,
        width="stretch",
            )
        pending = [row for row in evidence_rows if row["review_status"] == "pending"]
        if pending:
            st.info(
                "Review means: keep or discard this evidence for the current Gameweek. "
                "Accept credible, relevant reports; reject items that are unreliable or "
                "not decision-relevant. Leave the minutes adjustment at 0.00 unless the "
                "source directly supports a change in expected minutes."
            )
            if all(row["adjustment_support"] != "supported_numeric" for row in pending):
                st.caption(
                    "All pending items are informational and have no model-supported "
                    "numeric adjustment."
                )
                if st.button("Accept all as informational (0-minute adjustments)"):
                    for row in pending:
                        workflow.review_evidence(
                            int(row["id"]),
                            status="accepted",
                            rationale=(
                                "Accepted as informational; no model-supported "
                                "minutes adjustment."
                            ),
                            expected_minutes_adjustment=None,
                        )
                    st.rerun()
            pending_id = st.selectbox(
                "Evidence to review",
                [row["id"] for row in pending],
                format_func=lambda value: next(
                    row["summary"] for row in pending if row["id"] == value
                ),
            )
            selected_pending = next(row for row in pending if row["id"] == pending_id)
            review_action = st.radio(
                "Review decision",
                ("accepted", "rejected", "accepted_with_modifier"),
                format_func=lambda value: {
                    "accepted": "Accept as informational",
                    "rejected": "Reject",
                    "accepted_with_modifier": "Accept with model modifier",
                }[value],
                horizontal=True,
            )
            if selected_pending["decision_question"]:
                st.caption(f"Decision question: {selected_pending['decision_question']}")
            if selected_pending["adjustment_support"] != "supported_numeric":
                st.caption(
                    "This item is informational rather than a model-supported numeric "
                    "adjustment; keep the value at 0.00."
                )
            minutes_adjustment = None
            modifier_type = None
            modifier_operation = None
            modifier_value = None
            modifier_start = gameweek_number
            modifier_end = gameweek_number
            if review_action == "accepted_with_modifier":
                st.caption(
                    "A modifier changes model inputs over an explicit Gameweek range. "
                    "Use this only when the source supports a concrete, reviewable implication."
                )
                modifier_type = st.selectbox(
                    "Modifier type",
                    (
                        "expected_minutes",
                        "expected_minutes_delta",
                        "appearance_probability",
                        "appearance_probability_delta",
                        "starting_probability",
                        "starting_probability_delta",
                        "sixty_probability",
                        "sixty_probability_delta",
                        "availability",
                    ),
                )
                default_operation = "delta" if modifier_type.endswith("_delta") else "set"
                operation_options = (
                    ("delta", "set", "multiplier")
                    if modifier_type.endswith("_delta")
                    else ("set", "delta", "multiplier")
                )
                if modifier_type == "availability":
                    operation_options = ("set", "unavailable", "delta", "multiplier")
                modifier_operation = st.selectbox(
                    "Operation",
                    operation_options,
                    index=operation_options.index(default_operation)
                    if default_operation in operation_options
                    else 0,
                )
                probability_modifier = (
                    "probability" in modifier_type or modifier_type == "availability"
                )
                modifier_value = st.number_input(
                    "Modifier value",
                    min_value=-1.0 if probability_modifier else -90.0,
                    max_value=1.0 if probability_modifier else 90.0,
                    value=0.0 if modifier_operation in {"delta", "unavailable"} else 1.0,
                    step=0.05 if probability_modifier else 5.0,
                )
                modifier_start = st.number_input(
                    "First applicable Gameweek",
                    min_value=1,
                    max_value=38,
                    value=gameweek_number,
                    step=1,
                )
                modifier_end = st.number_input(
                    "Last applicable Gameweek",
                    min_value=1,
                    max_value=38,
                    value=gameweek_number,
                    step=1,
                )
            else:
                minutes_adjustment = st.number_input(
                    "Expected-minutes adjustment (supported numeric evidence only)",
                    min_value=-90.0,
                    max_value=90.0,
                    value=0.0,
                    step=5.0,
                    disabled=(
                        review_action != "accepted"
                        or selected_pending["adjustment_support"] != "supported_numeric"
                    ),
                )
            review_rationale = st.text_input(
                "Review rationale (optional)",
                placeholder="Leave blank to use an automatic note.",
            )
            if st.button("Record evidence review"):
                try:
                    modifier = None
                    if review_action == "accepted_with_modifier":
                        if not selected_pending["source_player_id"]:
                            raise WorkflowError(
                                "A model modifier requires a resolved player identity"
                            )
                        modifier = ReviewedProjectionModifier(
                            source_player_id=selected_pending["source_player_id"],
                            modifier_type=modifier_type,
                            operation=modifier_operation,
                            value=float(modifier_value),
                            start_gameweek=int(modifier_start),
                            end_gameweek=int(modifier_end),
                            evidence_ids=(int(pending_id),),
                            rationale=review_rationale or "Accepted model modifier.",
                            reviewed_by="user",
                            reviewed_at=datetime.now(UTC),
                            research_run_id=selected_pending["research_run_id"],
                            input_package_id=selected_pending["input_package_id"],
                        )
                    workflow.review_evidence(
                        pending_id,
                        status="rejected" if review_action == "rejected" else "accepted",
                        rationale=review_rationale,
                        expected_minutes_adjustment=(
                            minutes_adjustment
                            if (
                                review_action == "accepted"
                                and selected_pending["adjustment_support"] == "supported_numeric"
                            )
                            else None
                        ),
                        modifier=modifier,
                    )
                except (WorkflowError, ValueError) as error:
                    st.error(str(error))
                else:
                    st.rerun()

        if (
            evidence_rows
            and not pending
            and st.button(
                "Generate comparable pre-news and final projections",
                type="primary",
            )
        ):
            try:
                pair = create_news_projection_pair(
                    database,
                    rules,
                    season_code=season_code,
                    gameweek_number=gameweek_number,
                    pre_news_projection_run_id=(
                        None if latest_projection is None else latest_projection.run_id
                    ),
                )
            except WorkflowError as error:
                st.error(str(error))
            else:
                st.success(
                    f"News pair {pair.pair_id} saved: "
                    f"pre-news run {pair.pre_news_projection_run_id}, "
                    f"final run {pair.post_news_projection_run_id}."
                )
                st.rerun()

        pairs = database.connection.execute(
            """
            SELECT pairs.id, pairs.created_at,
                   pairs.pre_news_projection_run_id,
                   pairs.post_news_projection_run_id,
                   pairs.input_package_id, pairs.research_run_id,
                   evaluations.points_mae_change,
                   evaluations.minutes_mae_change
            FROM news_projection_pairs pairs
            LEFT JOIN news_projection_evaluations evaluations
              ON evaluations.news_projection_pair_id = pairs.id
            JOIN seasons ON seasons.id = pairs.season_id
            JOIN gameweeks ON gameweeks.id = pairs.gameweek_id
            WHERE seasons.code = ? AND gameweeks.number = ?
            ORDER BY pairs.id DESC
            """,
            (season_code, gameweek_number),
        ).fetchall()
        if pairs:
            st.write("Pre/post-news projection pairs")
            st.dataframe(
                pd.DataFrame([dict(row) for row in pairs]),
                hide_index=True,
        width="stretch",
            )
            latest_pair = pairs[0]
            with st.expander("Apply reviewed research and compare the decision", expanded=True):
                st.caption(
                    "This reruns the optimiser using the same source snapshot, model "
                    "configuration and full projection horizon as the baseline."
                )
                decision_type = st.selectbox(
                    "Decision to rerun",
                    options=("opening_squad", "transfers", "weekly_xi"),
                    format_func=lambda value: {
                        "opening_squad": "Opening squad",
                        "transfers": "Transfers from current squad",
                        "weekly_xi": "Weekly XI and captaincy",
                    }[value],
                    key="research_decision_type",
                )
                if st.button("Rerun decision with reviewed research"):
                    try:
                        rerun = generate_revised_projection(
                            database,
                            rules,
                            baseline_projection_run_id=int(
                                latest_pair["pre_news_projection_run_id"]
                            ),
                            decision_type=decision_type,
                            input_package_id=latest_pair["input_package_id"],
                            research_run_id=latest_pair["research_run_id"],
                        )
                        if decision_type == "opening_squad":
                            comparison = compare_opening_squad_decision(
                                database, rules,
                                baseline_projection_run_id=rerun.baseline_projection_run_id,
                                revised_projection_run_id=rerun.revised_projection_run_id,
                                modifier_ids=rerun.modifier_ids,
                            )
                        elif decision_type == "weekly_xi":
                            comparison = compare_weekly_xi_decision(
                                database, rules,
                                baseline_projection_run_id=rerun.baseline_projection_run_id,
                                revised_projection_run_id=rerun.revised_projection_run_id,
                                modifier_ids=rerun.modifier_ids,
                            )
                        else:
                            snapshot = database.connection.execute(
                                """
                                SELECT manager_snapshots.*
                                FROM manager_snapshots
                                JOIN seasons ON seasons.id = manager_snapshots.season_id
                                WHERE seasons.code = ?
                                ORDER BY manager_snapshots.captured_at DESC,
                                         manager_snapshots.id DESC
                                LIMIT 1
                                """,
                                (season_code,),
                            ).fetchone()
                            if snapshot is None:
                                raise WorkflowError(
                                    "Transfer comparison requires a saved current squad snapshot"
                                )
                            entries = database.connection.execute(
                                """
                                SELECT ps.source_player_id, entries.selling_price_tenths
                                FROM manager_squad_entries entries
                                JOIN player_seasons ps ON ps.id = entries.player_season_id
                                WHERE entries.manager_snapshot_id = ?
                                """,
                                (snapshot["id"],),
                            ).fetchall()
                            current = CurrentSquad(
                                player_ids=frozenset(row["source_player_id"] for row in entries),
                                selling_prices_tenths={
                                    row["source_player_id"]: int(row["selling_price_tenths"])
                                    for row in entries
                                },
                                bank_tenths=int(snapshot["bank_tenths"]),
                                free_transfers=int(snapshot["free_transfers"]),
                                available_chips=tuple(
                                    json.loads(snapshot["remaining_chips_json"]).keys()
                                ),
                            )
                            comparison = compare_transfer_decision(
                                database, rules,
                                baseline_projection_run_id=rerun.baseline_projection_run_id,
                                revised_projection_run_id=rerun.revised_projection_run_id,
                                current_squad=current,
                                modifier_ids=rerun.modifier_ids,
                            )
                    except (ValueError, WorkflowError, OptimisationError) as error:
                        st.error(str(error))
                    else:
                        st.success(
                            f"Revised projection run {rerun.revised_projection_run_id} "
                            f"and comparison {comparison.comparison_id} saved."
                        )
                        metrics = st.columns(4)
                        metrics[0].metric(
                            "Baseline under baseline beliefs",
                            f"{comparison.baseline_objective:.2f}",
                        )
                        metrics[1].metric(
                            "Baseline under revised beliefs",
                            f"{comparison.baseline_revalued_objective:.2f}",
                        )
                        metrics[2].metric(
                            "Revised recommendation",
                            f"{comparison.revised_objective:.2f}",
                        )
                        metrics[3].metric(
                            "Value of changing decision",
                            f"{comparison.decision_improvement:+.2f}",
                        )
                        st.write(f"Robustness: **{comparison.robustness}**")
                        st.json(comparison.changed_players)
                        if comparison.explanations:
                            st.dataframe(
                                pd.DataFrame(list(comparison.explanations)),
                                hide_index=True,
        width="stretch",
                            )
                        recommendation_row = database.connection.execute(
                            """
                            SELECT baseline_recommendation_json,
                                   revised_recommendation_json
                            FROM research_decision_comparisons
                            WHERE id = ?
                            """,
                            (comparison.comparison_id,),
                        ).fetchone()
                        if recommendation_row is not None:
                            revised_recommendation = json.loads(
                                recommendation_row["revised_recommendation_json"]
                            )
                            if decision_type == "opening_squad":
                                revised_candidates = {
                                    player.source_player_id: player
                                    for player in load_projection_candidates(
                                        database, comparison.revised_projection_run_id
                                    )
                                }
                                rows = []
                                starting = set(
                                    revised_recommendation.get("starting_player_ids", [])
                                )
                                for player_id in revised_recommendation.get("players", []):
                                    player = revised_candidates.get(player_id)
                                    if player is None:
                                        continue
                                    rows.append(
                                        {
                                            "Player": player.web_name,
                                            "Team": player.team_short_name,
                                            "Position": player.position.value,
                                            "Price (£m)": player.price_tenths / 10,
                                            "Status": (
                                                "Starter"
                                                if player_id in starting
                                                else "Bench"
                                            ),
                                            "Captain": player_id
                                            == revised_recommendation.get("captain_id"),
                                            "Vice-captain": player_id
                                            == revised_recommendation.get("vice_captain_id"),
                                        }
                                    )
                                if rows:
                                    st.subheader("Suggested team after reviewed research")
                                    st.dataframe(
                                        pd.DataFrame(rows),
                                        hide_index=True,
        width="stretch",
                                    )
                            elif decision_type == "transfers":
                                st.subheader("Suggested transfers after reviewed research")
                                st.write(
                                    "Transfers in:",
                                    revised_recommendation.get("transfers_in", []),
                                )
                                st.write(
                                    "Transfers out:",
                                    revised_recommendation.get("transfers_out", []),
                                )
                            else:
                                st.subheader("Suggested XI after reviewed research")
                                st.write(
                                    "Starting players:",
                                    revised_recommendation.get("players", []),
                                )
                                st.write(
                                    "Captain:", revised_recommendation.get("captain_id")
                                )
                                st.write(
                                    "Vice-captain:",
                                    revised_recommendation.get("vice_captain_id"),
                                )
        coverage_rows = database.connection.execute(
            """
            SELECT coverage.source_player_id, coverage.priority, coverage.status,
                   coverage.areas_checked_json, coverage.latest_source_checked_at,
                   coverage.notes, runs.research_run_id
            FROM team_news_coverage coverage
            JOIN team_news_research_runs runs ON runs.id = coverage.research_result_id
            JOIN seasons ON seasons.id = runs.season_id
            JOIN gameweeks ON gameweeks.id = runs.gameweek_id
            WHERE seasons.code = ? AND gameweeks.number = ?
            ORDER BY CASE coverage.priority WHEN 'critical' THEN 0
                     WHEN 'starting_xi' THEN 1 ELSE 2 END,
                     coverage.source_player_id
            """,
            (season_code, gameweek_number),
        ).fetchall()
        if coverage_rows:
            st.write("v3 coverage (checked status is independent of evidence)")
            st.dataframe(
                pd.DataFrame([dict(row) for row in coverage_rows]),
                hide_index=True,
        width="stretch",
            )
        if coverage_rows and pairs:
            latest_pair = pairs[0]
            unfinished = database.connection.execute(
                """
                SELECT COUNT(*) FROM fixtures
                JOIN gameweeks ON gameweeks.id = fixtures.gameweek_id
                JOIN seasons ON seasons.id = fixtures.season_id
                WHERE seasons.code = ? AND gameweeks.number = ?
                  AND fixtures.finished = 0
                """,
                (season_code, gameweek_number),
            ).fetchone()[0]
            if (
                latest_pair["points_mae_change"] is None
                and unfinished == 0
                and st.button("Evaluate latest news projection pair")
            ):
                try:
                    evaluation = evaluate_news_projection_pair(
                        database,
                        int(latest_pair["id"]),
                    )
                except WorkflowError as error:
                    st.error(str(error))
                else:
                    st.success(
                        "News uplift scored. Points MAE change: "
                        f"{evaluation.points_mae_change:+.4f}; minutes MAE "
                        f"change: {evaluation.minutes_mae_change:+.4f}."
                    )
                    st.rerun()

    projection = database.connection.execute(
        """
        SELECT projection_runs.id FROM projection_runs
        JOIN seasons ON seasons.id = projection_runs.season_id
        WHERE seasons.code = ? AND projection_runs.start_gameweek = ?
        ORDER BY projection_runs.generated_at DESC, projection_runs.id DESC
        LIMIT 1
        """,
        (season_code, gameweek_number),
    ).fetchone()
    if latest is None or projection is None:
        st.info(
            "A saved manager snapshot and projection run are required "
            "before recording the weekly decision."
        )
        return
    st.write("Record provisional or final recommendation")
    mode = st.radio("Run mode", ("provisional", "final"), horizontal=True)
    action_summary = st.text_input("Recommended action")
    forecast_points = st.number_input(
        "Forecast points",
        min_value=0.0,
        max_value=250.0,
        step=0.1,
    )
    trigger_text = st.text_area("Decision triggers (one per line)")
    if st.button("Save weekly decision run", type="primary"):
        try:
            run_id = workflow.create_decision_run(
                manager_snapshot_id=latest.snapshot_id,
                projection_run_id=projection["id"],
                mode=mode,
                recommendation={
                    "action": action_summary,
                    "expected_points": forecast_points,
                },
                decision_triggers=tuple(
                    line.strip() for line in trigger_text.splitlines() if line.strip()
                ),
            )
        except WorkflowError as error:
            st.error(str(error))
        else:
            st.success(f"Saved {mode} weekly run {run_id}.")
            st.rerun()

    runs = database.connection.execute(
        """
        SELECT weekly_decision_runs.id,
               weekly_decision_runs.mode,
               weekly_decision_runs.created_at,
               weekly_decision_runs.frozen_at,
               weekly_decision_runs.recommendation_json
        FROM weekly_decision_runs
        JOIN seasons ON seasons.id = weekly_decision_runs.season_id
        JOIN gameweeks ON gameweeks.id = weekly_decision_runs.gameweek_id
        WHERE seasons.code = ? AND gameweeks.number = ?
        ORDER BY weekly_decision_runs.id DESC
        """,
        (season_code, gameweek_number),
    ).fetchall()
    if not runs:
        return
    st.dataframe(
        pd.DataFrame([dict(row) for row in runs]),
        hide_index=True,
        width="stretch",
    )
    final_runs = [row for row in runs if row["mode"] == "final"]
    if not final_runs:
        return
    final_id = final_runs[0]["id"]
    action_exists = database.connection.execute(
        "SELECT 1 FROM actual_actions WHERE weekly_decision_run_id = ?",
        (final_id,),
    ).fetchone()
    if action_exists is None:
        st.write("After the deadline: record actual action")
        actual_summary = st.text_input("Actual action taken")
        followed = st.checkbox("Followed recommendation", value=True)
        deviation = st.text_input("Deviation reason")
        if st.button("Record actual action"):
            try:
                workflow.record_actual_action(
                    final_id,
                    action={"summary": actual_summary},
                    followed_recommendation=followed,
                    deviation_reason=deviation or None,
                )
            except WorkflowError as error:
                st.error(str(error))
            else:
                st.rerun()
        return
    evaluation_exists = database.connection.execute(
        "SELECT 1 FROM weekly_evaluations WHERE weekly_decision_run_id = ?",
        (final_id,),
    ).fetchone()
    if evaluation_exists is None:
        realised = st.number_input("Realised points", min_value=0.0, max_value=250.0, step=1.0)
        notes = st.text_area("Post-Gameweek review notes")
        if st.button("Score the weekly decision"):
            try:
                workflow.evaluate(
                    final_id,
                    realised_points=realised,
                    review_notes=notes or None,
                )
            except WorkflowError as error:
                st.error(str(error))
            else:
                st.success("Weekly decision scored.")
                st.rerun()


def _sidebar(database: HistoricalDatabase) -> None:
    st.sidebar.header("FPL Decision Engine")
    latest = database.connection.execute(
        """
        SELECT retrieved_at, status, row_count
        FROM ingestion_runs ORDER BY retrieved_at DESC, id DESC LIMIT 1
        """
    ).fetchone()
    if latest is None:
        st.sidebar.warning("No public data collected")
    else:
        st.sidebar.success(f"Data status: {latest['status']}")
        st.sidebar.caption(
            f"Last collection: {latest['retrieved_at']} · {latest['row_count']} rows"
        )
    st.sidebar.caption("Private manager state stays in the local SQLite database.")


def _data_health(
    database: HistoricalDatabase,
    season_code: str,
    gameweek_number: int,
    deadline: str | None,
) -> None:
    summary = database.season_summary(season_code)
    columns = st.columns(4)
    columns[0].metric("Players", summary["players"])
    columns[1].metric("Fixtures", summary["fixtures"])
    columns[2].metric("Price snapshots", summary["gameweek_snapshots"])
    columns[3].metric("Season stats", summary["season_stats_observations"])
    st.write(f"GW{gameweek_number} deadline: {deadline or 'not supplied'}")
    issues = database.connection.execute("PRAGMA foreign_key_check").fetchall()
    if issues:
        st.error(f"{len(issues)} database relationship issue(s) found.")
    else:
        st.success("Database relationships are valid.")
    health = build_model_health_report(database, season_code)
    st.write("Model health")
    if health.versions:
        st.dataframe(
            pd.DataFrame(
                {
                    "Model": version.model_version,
                    "Horizon step": version.horizon_step,
                    "Samples": version.samples,
                    "MAE": version.mean_absolute_error,
                    "Bias": version.bias,
                    "RMSE": version.root_mean_square_error,
                }
                for version in health.versions
            ),
            hide_index=True,
        width="stretch",
        )
    else:
        st.caption("No completed player forecasts are available to score yet.")
    st.caption(
        f"Weekly decisions scored: {health.weekly_decisions_scored} · "
        f"MAE: {health.weekly_mean_absolute_error} · Bias: {health.weekly_bias}"
    )
    st.caption(
        f"News pairs scored: {health.news_pairs_scored}; "
        f"points MAE change: {health.news_points_mae_change}; "
        f"minutes MAE change: {health.news_minutes_mae_change}"
    )
    st.write("Forward model qualification")
    candidates = database.connection.execute(
        """
        SELECT registrations.candidate_key, registrations.model_version,
               registrations.status, registrations.registered_at,
               registrations.evaluated_at
        FROM model_candidate_registrations registrations
        JOIN seasons ON seasons.id = registrations.season_id
        WHERE seasons.code = ?
        ORDER BY registrations.id
        """,
        (season_code,),
    ).fetchall()
    if candidates:
        st.dataframe(
            pd.DataFrame([dict(row) for row in candidates]),
            hide_index=True,
        width="stretch",
        )
        st.caption(
            "Declared challengers are captured alongside the incumbent but do not "
            "drive advice until their immutable forward gates pass."
        )
    else:
        st.caption("No forward model candidates are registered for this season.")
    prospective = build_prospective_capture_status(database, season_code)
    capture = prospective["summary"]
    if capture["no_missed_required_evidence_to_date"]:
        st.success("No required forward evidence has been missed to date.")
    else:
        st.error(
            f"{capture['incomplete_gameweeks']} Gameweek(s) have incomplete, "
            "irrecoverable forward evidence."
        )
    latest_backtest = database.connection.execute(
        """
        SELECT backtests.id
        FROM projection_backtest_runs backtests
        JOIN seasons ON seasons.id = backtests.season_id
        WHERE seasons.code = ? AND backtests.status = 'completed'
        ORDER BY backtests.created_at DESC, backtests.id DESC
        LIMIT 1
        """,
        (season_code,),
    ).fetchone()
    st.write("Historical walk-forward validation")
    if latest_backtest is None:
        st.caption(
            "No completed historical projection backtest is available for "
            "this season. Run `fpl-history backtest-projections` first."
        )
    else:
        report = load_backtest_report(database, int(latest_backtest["id"]))
        score_columns = st.columns(6)
        score_columns[0].metric("Generated", report.generated_prediction_count)
        score_columns[1].metric("Scored", report.prediction_count)
        score_columns[2].metric("Missing outcomes", report.missing_outcome_count)
        score_columns[3].metric("Points MAE", f"{report.overall.points_mae:.3f}")
        score_columns[4].metric("Points bias", f"{report.overall.points_bias:+.3f}")
        score_columns[5].metric("Minutes MAE", f"{report.overall.minutes_mae:.2f}")
        st.caption(
            f"{report.model_version} · {report.evidence_policy} · "
            f"origins GW{report.origin_gameweek_start}–"
            f"{report.origin_gameweek_end} · "
            f"{report.horizon_gameweeks}-Gameweek horizon"
        )
        st.caption(
            "Expected player-minutes per match: "
            f"{report.expected_minutes_per_match:.1f} / "
            f"{report.regulation_minutes_per_match:.0f} regulation target · "
            "actual "
            f"{report.actual_minutes_per_match:.1f}"
        )
        score_rows = [
            {
                "Breakdown": metric.group,
                "Value": metric.value,
                "Samples": metric.samples,
                "Points MAE": metric.points_mae,
                "Points bias": metric.points_bias,
                "Points RMSE": metric.points_rmse,
                "Minutes MAE": metric.minutes_mae,
            }
            for metric in (
                *report.by_position,
                *report.by_horizon,
                *report.by_participation,
                *report.by_fixture_count,
                *report.top_n,
            )
        ]
        st.dataframe(
            pd.DataFrame(score_rows),
            hide_index=True,
        width="stretch",
        )
        with st.expander("Backtest assumptions and limitations"):
            st.json(
                {
                    "model_config": report.as_dict()["model_config"],
                    "source_ingestion_run_id": report.source_ingestion_run_id,
                    "data_fingerprint": report.data_fingerprint,
                    "limitations": report.limitations,
                }
            )
    _refresh_button(database, season_code, season_code.replace("-", "/"))


def _refresh_button(database: HistoricalDatabase, season_code: str, season_name: str) -> None:
    if st.button("Refresh public FPL data"):
        with st.spinner("Collecting and validating the latest public FPL data…"):
            try:
                result = LiveSnapshotCollector(database).collect(
                    season_code=season_code,
                    season_name=season_name,
                )
            except Exception as error:
                st.error(f"Refresh failed: {error}")
            else:
                st.success(
                    f"Collected {result.players} players and {result.fixtures} fixtures "
                    f"for GW{result.gameweek_number}."
                )
                st.rerun()


def _option_index(options: list[str], value: str | None) -> int:
    return options.index(value) if value in options else 0


main()
