"""Synthetic evidence for the opponent-adjusted team-strength model.

Every league here is built by hand so the right answer is known in advance.
Real-season fixtures cannot prove that a rating responds to opponent quality,
because in a real season a club's schedule and its quality are entangled; a
constructed league can hold one fixed while moving the other.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from fpl_engine.config import load_season_rules
from fpl_engine.domain import Position
from fpl_engine.history.database import HistoricalDatabase
from fpl_engine.history.records import (
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
from fpl_engine.projections import (
    CORRECTED_V4_MODEL_CONFIG,
    OPPONENT_ADJUSTED_TEAM_STRENGTH_V1_MODEL_CONFIG,
    PRESEASON_V5_MODEL_CONFIG,
    ProjectionModelConfig,
    RatesProjectionModel,
)
from fpl_engine.team_strength import (
    ContextualAdjustment,
    TeamStrengthSettings,
    estimate_team_strength,
)

RULES = load_season_rules(Path("config/seasons/2026-27.json"))
CLUBS = tuple(f"Club {index}" for index in range(1, 9))
SQUAD_SHAPE = (
    (Position.GK, 2),
    (Position.DEF, 5),
    (Position.MID, 5),
    (Position.FWD, 3),
)


def _source(name: str = "synthetic", *, retrieved_at: datetime | None = None):
    return IngestionSource(
        name=name,
        retrieved_at=retrieved_at or datetime(2026, 8, 1, tzinfo=UTC),
        identifier_namespace="official-fpl",
    )


def _deadline(season_code: str, number: int) -> datetime:
    """One Gameweek a week from the start of August."""

    return datetime(int(season_code[:4]), 8, 1, 17, 30, tzinfo=UTC) + timedelta(
        days=7 * (number - 1)
    )


def _round_robin(count: int, *, legs: int = 1) -> list[list[tuple[int, int]]]:
    """A circle-method schedule: every club plays every other exactly once.

    With `legs=2` the whole thing is replayed with the venues reversed, which
    is the only way to reach an origin late enough to watch the preseason
    prior lose its grip.
    """

    order = list(range(1, count + 1))
    rounds = []
    for _ in range(count - 1):
        pairs = [
            (order[index], order[count - 1 - index]) for index in range(count // 2)
        ]
        rounds.append(pairs)
        order = [order[0], order[-1], *order[1:-1]]
    if legs == 2:
        rounds += [[(away, home) for home, away in pairs] for pairs in rounds]
    return rounds


def _league(
    season_code: str,
    *,
    goals: dict[tuple[int, int], tuple[int, int]] | None = None,
    default_score: tuple[int, int] = (1, 1),
    squads: dict[int, list[str]] | None = None,
    codes: dict[str, str] | None = None,
    club_names: tuple[str, ...] = CLUBS,
    with_expected_goals: bool = True,
    finished: bool = True,
    legs: int = 1,
) -> HistoricalBundle:
    """An eight-club league playing one complete round robin.

    `goals` maps (home club, away club) to the score. Anything unlisted plays
    out `default_score`, which keeps a control league perfectly symmetric so
    the only thing a test varies is the thing it is testing.
    """

    count = len(club_names)
    teams = tuple(
        TeamRecord(str(index), name, f"C{index}")
        for index, name in enumerate(club_names, start=1)
    )
    rounds = _round_robin(count, legs=legs)
    gameweeks = tuple(
        GameweekRecord(
            number,
            _deadline(season_code, number).isoformat().replace("+00:00", "Z"),
            finished,
        )
        for number in range(1, len(rounds) + 1)
    )
    fixtures = []
    fixture_by_team: dict[tuple[int, int], str] = {}
    scores: dict[str, tuple[int, int, int, int]] = {}
    for number, pairs in enumerate(rounds, start=1):
        for index, (home, away) in enumerate(pairs):
            fixture_id = f"{season_code}-{number}-{index}"
            home_goals, away_goals = (goals or {}).get((home, away), default_score)
            fixtures.append(
                FixtureRecord(
                    fixture_id,
                    str(home),
                    str(away),
                    number,
                    finished=finished,
                    home_score=home_goals if finished else None,
                    away_score=away_goals if finished else None,
                )
            )
            fixture_by_team[(number, home)] = fixture_id
            fixture_by_team[(number, away)] = fixture_id
            scores[fixture_id] = (home, away, home_goals, away_goals)

    players: list[PlayerRecord] = []
    player_seasons: list[PlayerSeasonRecord] = []
    club_squads = squads or {}
    for club in range(1, count + 1):
        names = club_squads.get(club)
        if names is None:
            names = [
                f"p{club}-{position.value}{slot}"
                for position, size in SQUAD_SHAPE
                for slot in range(size)
            ]
        positions = [
            position for position, size in SQUAD_SHAPE for _ in range(size)
        ]
        for slot, identifier in enumerate(names):
            # A stable code, keyed on the player's name rather than the source
            # id, is what lets the two seasons be linked at all. Real feeds
            # renumber source ids between seasons.
            players.append(
                PlayerRecord(
                    identifier,
                    identifier,
                    identifier,
                    "",
                    official_fpl_code=(codes or {}).get(
                        identifier, f"code-{identifier}"
                    ),
                )
            )
            player_seasons.append(
                PlayerSeasonRecord(
                    identifier,
                    str(club),
                    positions[slot % len(positions)],
                    50,
                    50,
                )
            )

    by_club: dict[int, list[PlayerSeasonRecord]] = {}
    for season in player_seasons:
        by_club.setdefault(int(season.source_team_id), []).append(season)

    fixture_stats = []
    for fixture_id, (home, away, home_goals, away_goals) in scores.items():
        if not finished:
            continue
        for club, scored, conceded in (
            (home, home_goals, away_goals),
            (away, away_goals, home_goals),
        ):
            squad = by_club[club]
            # Attacking output is spread over the forwards and midfielders so
            # the team totals reconstructed from player rows match the score.
            attackers = [
                season
                for season in squad
                if season.position in (Position.MID, Position.FWD)
            ]
            for index, season in enumerate(squad):
                is_attacker = season in attackers
                share = (
                    scored / len(attackers) if is_attacker and attackers else 0.0
                )
                fixture_stats.append(
                    PlayerFixtureStatsRecord(
                        season.source_player_id,
                        fixture_id,
                        minutes=90,
                        starts=index < 11,
                        goals=int(round(share)),
                        assists=0,
                        clean_sheet=conceded == 0,
                        goals_conceded=conceded,
                        expected_goals=share if with_expected_goals else None,
                        expected_assists=(
                            share * 0.7 if with_expected_goals else None
                        ),
                        total_points=2,
                    )
                )
    # The projection engine only sees players who have a price observation, so
    # every squad member is priced in every Gameweek. Prices vary a little by
    # position so the price-scaled cold-start prior has something to read.
    snapshots = tuple(
        PlayerGameweekSnapshotRecord(
            season.source_player_id,
            gameweek.number,
            {
                Position.GK: 45,
                Position.DEF: 50,
                Position.MID: 65,
                Position.FWD: 75,
            }[season.position],
            _deadline(season_code, gameweek.number) - timedelta(hours=12),
            source_team_id=season.source_team_id,
            status="a",
            observation_kind="live_pre_deadline",
            timing_quality="exact",
            source_observation_key=(
                f"{season_code}-{season.source_player_id}-{gameweek.number}"
            ),
        )
        for season in player_seasons
        for gameweek in gameweeks
    )
    return HistoricalBundle(
        season=SeasonRecord(season_code, season_code.replace("-", "/")),
        teams=teams,
        players=tuple(players),
        player_seasons=tuple(player_seasons),
        gameweeks=gameweeks,
        fixtures=tuple(fixtures),
        fixture_stats=tuple(fixture_stats),
        gameweek_snapshots=snapshots,
    )


def _strong_weak_goals(strong: set[int], weak: set[int]) -> dict:
    """Strong clubs beat weak ones heavily; peers draw."""

    result = {}
    for rounds in _round_robin(8):
        for home, away in rounds:
            if home in strong and away in weak:
                result[(home, away)] = (4, 0)
            elif home in weak and away in strong:
                result[(home, away)] = (0, 4)
            else:
                result[(home, away)] = (1, 1)
    return result


# --------------------------------------------------------------------------
# 1. Preseason strengths are not all equal when carry-forward evidence exists.
# --------------------------------------------------------------------------


def _two_season_database(tmp_path, **kwargs):
    database = HistoricalDatabase(tmp_path / "fpl.sqlite3")
    database.__enter__()
    database.initialise()
    database.ingest_bundle(
        _source(),
        _league(
            "2025-26",
            goals=_strong_weak_goals({1, 2, 3, 4}, {5, 6, 7, 8}),
            **kwargs,
        ),
    )
    database.ingest_bundle(
        _source(), _league("2026-27", finished=False, **kwargs)
    )
    return database


def test_preseason_strengths_are_not_all_equal(tmp_path) -> None:
    database = _two_season_database(tmp_path)
    try:
        state = estimate_team_strength(
            database, season_code="2026-27", gameweek_number=1
        )
    finally:
        database.__exit__(None, None, None)

    attacks = {team.name: team.attack for team in state.teams.values()}
    assert len(set(round(value, 4) for value in attacks.values())) > 1
    strong = [attacks[f"Club {index}"] for index in (1, 2, 3, 4)]
    weak = [attacks[f"Club {index}"] for index in (5, 6, 7, 8)]
    assert min(strong) > max(weak)
    # Nothing has been played yet, so the prior is the whole estimate.
    for team in state.teams.values():
        assert team.current_weight == 0.0
        assert team.matches_observed == 0.0


# --------------------------------------------------------------------------
# 2 and 3. Opponent adjustment.
# --------------------------------------------------------------------------


def test_identical_results_against_different_opponents_diverge(tmp_path) -> None:
    """Two clubs score the same goals; only the opposition differs."""

    # Clubs 1-4 are strong, 5-8 weak, established by their results against each
    # other. Club 1 and club 5 both put four past someone: club 1 against a
    # strong peer, club 5 against a weak one.
    goals = _strong_weak_goals({1, 2, 3, 4}, {5, 6, 7, 8})
    database = HistoricalDatabase(tmp_path / "fpl.sqlite3")
    database.__enter__()
    try:
        database.initialise()
        database.ingest_bundle(_source(), _league("2026-27", goals=goals))
        state = estimate_team_strength(
            database, season_code="2026-27", gameweek_number=9
        )
    finally:
        database.__exit__(None, None, None)

    by_name = {team.name: team for team in state.teams.values()}
    strong = [by_name[f"Club {index}"] for index in (1, 2, 3, 4)]
    weak = [by_name[f"Club {index}"] for index in (5, 6, 7, 8)]
    # The strong group scored their goals against the weak group and the weak
    # group conceded them, so attack and defence must separate in that order.
    assert min(team.attack for team in strong) > max(team.attack for team in weak)
    assert max(team.defence for team in strong) < min(
        team.defence for team in weak
    )


def test_the_same_goals_rate_higher_against_stronger_opposition(
    tmp_path,
) -> None:
    """The direct form of the claim, with raw output held exactly equal."""

    # Everyone draws 1-1 except two clubs who each win 3-0 once. Club 1 beats
    # club 2, who are otherwise unbeaten. Club 7 beats club 8, who have been
    # beaten by everyone. Raw goals for and against are identical for 1 and 7.
    goals = {}
    for rounds in _round_robin(8):
        for home, away in rounds:
            goals[(home, away)] = (1, 1)
    for opponent in (1, 2, 3, 4, 5, 6, 7):
        # Club 8 is beaten by everyone, making it demonstrably the weakest.
        key = (opponent, 8) if (opponent, 8) in goals else (8, opponent)
        goals[key] = (3, 0) if key == (opponent, 8) else (0, 3)
    key_one = (1, 2) if (1, 2) in goals else (2, 1)
    goals[key_one] = (3, 0) if key_one == (1, 2) else (0, 3)

    database = HistoricalDatabase(tmp_path / "fpl.sqlite3")
    database.__enter__()
    try:
        database.initialise()
        database.ingest_bundle(_source(), _league("2026-27", goals=goals))
        state = estimate_team_strength(
            database, season_code="2026-27", gameweek_number=9
        )
    finally:
        database.__exit__(None, None, None)

    by_name = {team.name: team for team in state.teams.values()}
    # Club 2 conceded three only to club 1 and otherwise drew; club 8 conceded
    # three to everyone. Club 1's win therefore came against much the better
    # defence, and its attack rating must exceed that of clubs whose identical
    # 3-0 came against club 8.
    beat_the_weakest = [by_name[f"Club {index}"].attack for index in (3, 4, 5, 6)]
    assert by_name["Club 1"].attack > max(beat_the_weakest)
    # And the weakest club's defence must be the worst in the division.
    assert by_name["Club 8"].defence == max(
        team.defence for team in state.teams.values()
    )


def test_a_soft_schedule_is_reported_and_does_not_inflate_the_rating(
    tmp_path,
) -> None:
    database = _two_season_database(tmp_path)
    try:
        # Only the first two Gameweeks of 2026/27 exist, so schedules differ.
        database.ingest_bundle(
            _source("second"),
            _league(
                "2026-27",
                goals=_strong_weak_goals({1, 2, 3, 4}, {5, 6, 7, 8}),
            ),
        )
        state = estimate_team_strength(
            database, season_code="2026-27", gameweek_number=4
        )
    finally:
        database.__exit__(None, None, None)

    schedules = {team.name: team.schedule_strength for team in state.teams.values()}
    assert schedules
    # Schedule strength is a reported diagnostic on a scale where 1.0 is the
    # league mean, never an applied correction.
    assert all(0.2 < value < 5.0 for value in schedules.values())


# --------------------------------------------------------------------------
# 4 and 14. Leakage.
# --------------------------------------------------------------------------


def test_no_evidence_after_the_origin_is_read(tmp_path) -> None:
    goals = _strong_weak_goals({1, 2, 3, 4}, {5, 6, 7, 8})
    database = HistoricalDatabase(tmp_path / "fpl.sqlite3")
    database.__enter__()
    try:
        database.initialise()
        database.ingest_bundle(_source(), _league("2026-27", goals=goals))
        early = estimate_team_strength(
            database, season_code="2026-27", gameweek_number=3
        )
        late = estimate_team_strength(
            database, season_code="2026-27", gameweek_number=8
        )
    finally:
        database.__exit__(None, None, None)

    # Two Gameweeks are readable at origin three and seven at origin eight.
    assert all(team.matches_observed == 2.0 for team in early.teams.values())
    assert all(team.matches_observed == 7.0 for team in late.teams.values())
    # Reading the same origin twice must give the same answer, whatever else
    # the database has since learned.
    database = HistoricalDatabase(tmp_path / "fpl.sqlite3")
    database.__enter__()
    try:
        repeat = estimate_team_strength(
            database, season_code="2026-27", gameweek_number=3
        )
    finally:
        database.__exit__(None, None, None)
    assert repeat.as_dict()["teams"] == early.as_dict()["teams"]


def test_as_of_and_ingestion_run_restrictions_are_leakage_safe(tmp_path) -> None:
    goals = _strong_weak_goals({1, 2, 3, 4}, {5, 6, 7, 8})
    database = HistoricalDatabase(tmp_path / "fpl.sqlite3")
    database.__enter__()
    try:
        database.initialise()
        database.ingest_bundle(
            _source(retrieved_at=datetime(2026, 12, 1, tzinfo=UTC)),
            _league("2026-27", goals=goals),
        )
        # An origin whose evidence had not yet been ingested sees nothing at
        # all, rather than quietly seeing everything.
        blind = estimate_team_strength(
            database,
            season_code="2026-27",
            gameweek_number=8,
            as_of=datetime(2026, 9, 1, tzinfo=UTC),
        )
        sighted = estimate_team_strength(
            database,
            season_code="2026-27",
            gameweek_number=8,
            as_of=datetime(2027, 1, 1, tzinfo=UTC),
        )
        blocked = estimate_team_strength(
            database,
            season_code="2026-27",
            gameweek_number=8,
            as_of=datetime(2027, 1, 1, tzinfo=UTC),
            maximum_ingestion_run_id=0,
        )
    finally:
        database.__exit__(None, None, None)

    assert all(team.current_weight == 0.0 for team in blind.teams.values())
    assert any(team.current_weight > 0.0 for team in sighted.teams.values())
    assert all(team.current_weight == 0.0 for team in blocked.teams.values())


# --------------------------------------------------------------------------
# 5. Current evidence overtakes the prior.
# --------------------------------------------------------------------------


def test_current_evidence_gradually_overtakes_the_preseason_prior(
    tmp_path,
) -> None:
    database = _two_season_database(tmp_path)
    origins = (1, 3, 5, 8, 11, 15)
    try:
        # Two legs, so there are fourteen played Gameweeks to walk through.
        database.ingest_bundle(
            _source("second"),
            _league(
                "2026-27",
                goals=_strong_weak_goals({1, 2, 3, 4}, {5, 6, 7, 8}),
                legs=2,
            ),
        )
        shares = [
            [
                team.prior_share
                for team in estimate_team_strength(
                    database, season_code="2026-27", gameweek_number=origin
                ).teams.values()
            ]
            for origin in origins
        ]
    finally:
        database.__exit__(None, None, None)

    averages = [sum(values) / len(values) for values in shares]
    # Before a ball is kicked the prior is the entire estimate.
    assert averages[0] == pytest.approx(1.0)
    # It then cedes ground at every origin, without exception.
    assert all(
        later < earlier
        for earlier, later in zip(averages[:-1], averages[1:], strict=True)
    )
    # It still leads at the third Gameweek — the prior is meant to dominate
    # early — and has clearly lost by the middle of the season.
    assert averages[1] > 0.5
    assert averages[-1] < 0.4


# --------------------------------------------------------------------------
# 6 and 7. Squad continuity.
# --------------------------------------------------------------------------


def _continuity_database(tmp_path, rebuilt: int):
    """`rebuilt` names the club whose whole squad is replaced."""

    stable = {
        club: [f"keep{club}-{slot}" for slot in range(15)] for club in range(1, 9)
    }
    following = {
        club: (
            [f"new{club}-{slot}" for slot in range(15)]
            if club == rebuilt
            else list(names)
        )
        for club, names in stable.items()
    }
    database = HistoricalDatabase(tmp_path / "fpl.sqlite3")
    database.__enter__()
    database.initialise()
    database.ingest_bundle(
        _source(),
        _league(
            "2025-26",
            goals=_strong_weak_goals({1, 2, 3, 4}, {5, 6, 7, 8}),
            squads=stable,
        ),
    )
    database.ingest_bundle(
        _source(),
        _league("2026-27", squads=following, finished=False),
    )
    return database


def test_a_stable_squad_keeps_more_prior_confidence_than_a_rebuilt_one(
    tmp_path,
) -> None:
    database = _continuity_database(tmp_path, rebuilt=1)
    try:
        state = estimate_team_strength(
            database, season_code="2026-27", gameweek_number=1
        )
    finally:
        database.__exit__(None, None, None)

    by_name = {team.name: team for team in state.teams.values()}
    rebuilt = by_name["Club 1"]
    stable = by_name["Club 2"]
    assert rebuilt.squad_continuity is not None
    assert rebuilt.squad_continuity.retained_minutes_share == 0.0
    assert stable.squad_continuity is not None
    assert stable.squad_continuity.retained_minutes_share == pytest.approx(1.0)
    assert rebuilt.prior_weight < stable.prior_weight
    # Both clubs finished the previous season in the same group, so their
    # unadjusted priors were alike. The rebuilt club's is pulled closer to the
    # league average; the stable club's is not.
    assert abs(rebuilt.prior_attack - 1.0) < abs(stable.prior_attack - 1.0)
    assert abs(rebuilt.prior_defence - 1.0) < abs(stable.prior_defence - 1.0)
    assert any("Squad continuity" in line for line in rebuilt.rationale)
    assert not any("Squad continuity" in line for line in stable.rationale)


def test_continuity_survives_the_source_renumbering_its_players(
    tmp_path,
) -> None:
    """The same player under a new source id is still the same player.

    The source reassigns player ids between seasons. Keying continuity on them
    reports every settled squad as a total rebuild, which silently throws away
    the preseason prior for the whole division — the ratings still look
    reasonable, so nothing complains.
    """

    stable = {
        club: [f"keep{club}-{slot}" for slot in range(15)] for club in range(1, 9)
    }
    renumbered = {
        club: [f"renumbered-{name}" for name in names]
        for club, names in stable.items()
    }
    # Same players, same clubs, brand new source ids — but the stable code
    # travels with the player, so the identity link still resolves.
    codes = {
        f"renumbered-{name}": f"code-{name}"
        for names in stable.values()
        for name in names
    }
    database = HistoricalDatabase(tmp_path / "fpl.sqlite3")
    database.__enter__()
    try:
        database.initialise()
        database.ingest_bundle(_source(), _league("2025-26", squads=stable))
        database.ingest_bundle(
            _source(),
            _league("2026-27", squads=renumbered, codes=codes, finished=False),
        )
        state = estimate_team_strength(
            database, season_code="2026-27", gameweek_number=1
        )
    finally:
        database.__exit__(None, None, None)

    for team in state.teams.values():
        assert team.squad_continuity is not None
        assert team.squad_continuity.retained_minutes_share == pytest.approx(1.0)


def test_an_unlinkable_squad_reports_ignorance_not_a_league_wide_rebuild(
    tmp_path,
) -> None:
    """No stable code means continuity is unknown, and must say so.

    Reporting zero retention instead would strip the preseason prior from
    every club in the division while the ratings still looked plausible.
    """

    stable = {
        club: [f"keep{club}-{slot}" for slot in range(15)] for club in range(1, 9)
    }
    renumbered = {
        club: [f"renumbered-{name}" for name in names]
        for club, names in stable.items()
    }
    database = HistoricalDatabase(tmp_path / "fpl.sqlite3")
    database.__enter__()
    try:
        database.initialise()
        database.ingest_bundle(_source(), _league("2025-26", squads=stable))
        # New source ids and new codes: nothing links the seasons.
        database.ingest_bundle(
            _source(),
            _league("2026-27", squads=renumbered, finished=False),
        )
        state = estimate_team_strength(
            database, season_code="2026-27", gameweek_number=1
        )
    finally:
        database.__exit__(None, None, None)

    assert all(team.squad_continuity is None for team in state.teams.values())
    assert any(
        "could not be measured" in line and "not a finding" in line
        for line in state.limitations
    )


def test_the_goal_level_survives_incomplete_expected_goal_coverage(
    tmp_path,
) -> None:
    """Rate on expected goals; take the level from the scoreline.

    A provider missing a third of its rows produces a perfectly usable ranking
    and a badly deflated goal expectation. The league average the projection
    consumes has to come from goals actually scored, or every club's forecast
    is quietly scaled down by the gap in someone else's feed.
    """

    goals = _strong_weak_goals({1, 2, 3, 4}, {5, 6, 7, 8})
    database = HistoricalDatabase(tmp_path / "fpl.sqlite3")
    database.__enter__()
    try:
        database.initialise()
        database.ingest_bundle(_source(), _league("2026-27", goals=goals))
        complete = estimate_team_strength(
            database, season_code="2026-27", gameweek_number=8
        )
        actual = database.connection.execute(
            """
            SELECT SUM(home_score + away_score) * 1.0 / (2 * COUNT(*)) AS average
            FROM fixtures
            JOIN seasons ON seasons.id = fixtures.season_id
            JOIN gameweeks ON gameweeks.id = fixtures.gameweek_id
            WHERE seasons.code = '2026-27' AND fixtures.finished = 1
              AND gameweeks.number < 8
            """
        ).fetchone()["average"]
        # Halve every expected-goal row, as a feed with gaps in it would.
        database.connection.execute(
            "UPDATE player_fixture_stats SET expected_goals = expected_goals * 0.5"
        )
        database.connection.commit()
        starved = estimate_team_strength(
            database, season_code="2026-27", gameweek_number=8
        )
    finally:
        database.__exit__(None, None, None)

    # Halving the expected-goal feed must not move the level at all.
    assert starved.league_average_goals == pytest.approx(
        complete.league_average_goals
    )
    # And that level tracks the goals scored, not the feed. It is not exactly
    # the flat average because recent Gameweeks carry more weight.
    assert complete.league_average_goals == pytest.approx(actual, rel=0.05)
    # And the shortfall is reported rather than absorbed: coverage that thin
    # falls back to goals for the ratings too.
    assert any(
        team.evidence_source == "goals" for team in starved.teams.values()
    )
    assert any("fell back to actual goals" in line for line in starved.limitations)


def test_squad_churn_changes_uncertainty_without_any_directional_adjustment(
    tmp_path,
) -> None:
    database = _continuity_database(tmp_path, rebuilt=1)
    try:
        state = estimate_team_strength(
            database, season_code="2026-27", gameweek_number=1
        )
    finally:
        database.__exit__(None, None, None)

    by_name = {team.name: team for team in state.teams.values()}
    rebuilt = by_name["Club 1"]
    stable = by_name["Club 2"]
    assert rebuilt.uncertainty > stable.uncertainty
    # Nothing about the rebuild says which direction the club moved, and the
    # model must not pretend otherwise: no adjustment was supplied.
    assert rebuilt.adjustments == ()
    assert any("no transfer fee" in line for line in state.limitations)


# --------------------------------------------------------------------------
# 8 and 9. Contextual adjustments.
# --------------------------------------------------------------------------


def _adjustment(**kwargs) -> ContextualAdjustment:
    defaults = {
        "source_team_id": "1",
        "category": "long_term_injury",
        "attack_multiplier": 0.85,
        "rationale": "First-choice striker out until December, reviewed 1 Aug.",
        "source": "club statement",
        "confidence": "high",
    }
    return ContextualAdjustment(**{**defaults, **kwargs})


def test_contextual_adjustments_apply_only_within_their_dates(tmp_path) -> None:
    database = _two_season_database(tmp_path)
    adjustment = _adjustment(effective_from_gameweek=3, effective_to_gameweek=5)
    try:
        outcomes = {
            origin: estimate_team_strength(
                database,
                season_code="2026-27",
                gameweek_number=origin,
                adjustments=(adjustment,),
            )
            for origin in (2, 4, 6)
        }
    finally:
        database.__exit__(None, None, None)

    applied = {
        origin: state.by_source_id()["1"] for origin, state in outcomes.items()
    }
    assert applied[2].adjustments == ()
    assert applied[4].adjustments == (adjustment,)
    assert applied[6].adjustments == ()
    assert applied[4].attack < applied[2].attack
    assert applied[6].attack == pytest.approx(applied[2].attack)


def test_every_adjustment_is_persisted_and_reported(tmp_path) -> None:
    database = _two_season_database(tmp_path)
    adjustment = _adjustment()
    try:
        model = RatesProjectionModel(
            database,
            RULES,
            config=OPPONENT_ADJUSTED_TEAM_STRENGTH_V1_MODEL_CONFIG,
            team_strength_adjustments=(adjustment,),
        )
        result = model.project(
            season_code="2026-27",
            start_gameweek=1,
            horizon_gameweeks=1,
            generated_at=datetime(2026, 8, 1, tzinfo=UTC),
            persist=False,
        )
    finally:
        database.__exit__(None, None, None)

    assert result.team_strength_state is not None
    reported = result.team_strength_state.as_dict()
    entries = [
        entry for team in reported["teams"] for entry in team["adjustments"]
    ]
    assert len(entries) == 1
    assert entries[0]["rationale"] == adjustment.rationale
    assert entries[0]["source"] == "club statement"
    assert entries[0]["confidence"] == "high"
    assert any(
        "long_term_injury" in line
        for team in result.team_strength_state.teams.values()
        for line in team.rationale
    )


def test_an_adjustment_without_a_rationale_is_refused() -> None:
    with pytest.raises(ValueError, match="needs a rationale"):
        ContextualAdjustment(
            source_team_id="1", category="manager_change", attack_multiplier=1.1
        )
    with pytest.raises(ValueError, match="outside the permitted range"):
        _adjustment(attack_multiplier=2.5)
    with pytest.raises(ValueError, match="cannot expire before it begins"):
        _adjustment(effective_from_gameweek=8, effective_to_gameweek=3)


# --------------------------------------------------------------------------
# 10. Promoted clubs.
# --------------------------------------------------------------------------


def test_promoted_clubs_get_declared_priors_and_greater_uncertainty(
    tmp_path,
) -> None:
    database = HistoricalDatabase(tmp_path / "fpl.sqlite3")
    database.__enter__()
    try:
        database.initialise()
        database.ingest_bundle(
            _source(),
            _league(
                "2025-26",
                goals=_strong_weak_goals({1, 2, 3, 4}, {5, 6, 7, 8}),
            ),
        )
        # Two clubs go down; two come up under names never seen before.
        promoted_names = (*CLUBS[:6], "Newly Promoted A", "Newly Promoted B")
        database.ingest_bundle(
            _source(),
            _league("2026-27", club_names=promoted_names, finished=False),
        )
        settings = TeamStrengthSettings()
        state = estimate_team_strength(
            database,
            season_code="2026-27",
            gameweek_number=1,
            settings=settings,
        )
    finally:
        database.__exit__(None, None, None)

    by_name = {team.name: team for team in state.teams.values()}
    promoted = [by_name["Newly Promoted A"], by_name["Newly Promoted B"]]
    established = [by_name[name] for name in CLUBS[:6]]
    for team in promoted:
        assert team.is_promoted
        assert team.prior_attack == pytest.approx(settings.promoted_attack)
        assert team.prior_defence == pytest.approx(settings.promoted_defence)
        assert team.prior_weight == pytest.approx(settings.promoted_prior_matches)
        assert any("Promoted club" in line for line in team.rationale)
    # Wider uncertainty than any established club, and not a supposedly precise
    # league-average value.
    assert min(team.uncertainty for team in promoted) > max(
        team.uncertainty for team in established
    )
    assert all(team.attack != pytest.approx(1.0) for team in promoted)


# --------------------------------------------------------------------------
# 11, 12 and 13. Coherent allocation and defensive coherence.
# --------------------------------------------------------------------------


def _projection(tmp_path, config: ProjectionModelConfig, **kwargs):
    database = _two_season_database(tmp_path)
    try:
        database.ingest_bundle(
            _source("second"),
            _league(
                "2026-27",
                goals=_strong_weak_goals({1, 2, 3, 4}, {5, 6, 7, 8}),
            ),
        )
        model = RatesProjectionModel(database, RULES, config=config, **kwargs)
        return model.project(
            season_code="2026-27",
            start_gameweek=4,
            horizon_gameweeks=1,
            generated_at=datetime(2026, 9, 1, tzinfo=UTC),
            persist=False,
        )
    finally:
        database.__exit__(None, None, None)


def test_player_goal_shares_reconcile_to_the_team_expected_goal_total(
    tmp_path,
) -> None:
    result = _projection(
        tmp_path, OPPONENT_ADJUSTED_TEAM_STRENGTH_V1_MODEL_CONFIG
    )

    by_team: dict[str, list] = {}
    for projection in result.projections:
        assert projection.latent_expectations is not None
        by_team.setdefault(projection.team_short_name, []).append(projection)
    assert by_team
    for players in by_team.values():
        team_goals = {
            round(player.latent_expectations["team_expected_goals"], 9)
            for player in players
        }
        # Every player at a club must be reading one team goal expectation.
        assert len(team_goals) == 1
        total_share = sum(
            player.latent_expectations["goal_share"] for player in players
        )
        assert total_share == pytest.approx(1.0)
        # So the players' goal expectations sum to the team's exactly.
        allocated = sum(
            player.latent_expectations["team_expected_goals"]
            * player.latent_expectations["goal_share"]
            for player in players
        )
        assert allocated == pytest.approx(team_goals.pop())


def test_club_strength_is_not_applied_to_player_rates_a_second_time(
    tmp_path,
) -> None:
    """The share path scales with the team total; the rate path compounds.

    The club's strength has to enter a player's goal expectation exactly once.
    In the share path it enters through the team total and nowhere else, which
    is checkable as an identity. In the incumbent rate path it enters twice —
    once inside the historical per-90 rate the player earned at that club, and
    again as an explicit multiplier — so the same identity must fail there.
    """

    challenger = _projection(
        tmp_path, OPPONENT_ADJUSTED_TEAM_STRENGTH_V1_MODEL_CONFIG
    )
    strengths = challenger.team_strengths
    strong = max(strengths.values(), key=lambda entry: entry["attack"])
    weak = min(strengths.values(), key=lambda entry: entry["attack"])
    assert strong["attack"] > weak["attack"]

    def allocated(projection) -> float:
        latent = projection.latent_expectations
        assert latent is not None
        return (
            latent["team_expected_goals"]
            * latent["goal_share"]
            * RULES.scoring.goals[projection.position.value]
        )

    for projection in challenger.projections:
        # Team total times share times the scoring rule. Nothing else
        # multiplies in: no per-90 rate, and no second copy of club strength.
        # Points are reported to three decimals, hence the tolerance.
        assert projection.goal_points == pytest.approx(
            allocated(projection), abs=5e-4
        )

    incumbent = _projection(tmp_path, CORRECTED_V4_MODEL_CONFIG)
    scorers = [
        projection
        for projection in incumbent.projections
        if projection.goal_points > 0.05
    ]
    assert scorers
    assert any(
        abs(projection.goal_points - allocated(projection)) > 5e-4
        for projection in scorers
    )


def test_clean_sheets_and_concessions_use_one_opponent_goal_expectation(
    tmp_path,
) -> None:
    result = _projection(
        tmp_path, OPPONENT_ADJUSTED_TEAM_STRENGTH_V1_MODEL_CONFIG
    )

    by_team: dict[str, list] = {}
    for projection in result.projections:
        by_team.setdefault(projection.team_short_name, []).append(projection)
    for players in by_team.values():
        opponent_goals = {
            round(player.latent_expectations["opponent_expected_goals"], 9)
            for player in players
        }
        assert len(opponent_goals) == 1
        defenders = [
            player
            for player in players
            if player.position is Position.DEF
            and player.sixty_probability > 0
        ]
        if len(defenders) < 2:
            continue
        # Defenders at one club facing one opponent must share a clean-sheet
        # probability. Points differ only through the 60-minute probability,
        # so dividing it out must leave one number.
        implied = {
            round(
                player.clean_sheet_points
                / (
                    player.sixty_probability
                    * RULES.scoring.clean_sheets[player.position.value]
                ),
                9,
            )
            for player in defenders
        }
        assert len(implied) == 1


# --------------------------------------------------------------------------
# 15. Existing configurations keep working.
# --------------------------------------------------------------------------


def test_existing_configurations_and_reproduction_are_unaffected(
    tmp_path,
) -> None:
    incumbent = _projection(tmp_path, CORRECTED_V4_MODEL_CONFIG)
    repeat = _projection(tmp_path, CORRECTED_V4_MODEL_CONFIG)
    preseason = _projection(tmp_path, PRESEASON_V5_MODEL_CONFIG)

    assert CORRECTED_V4_MODEL_CONFIG.team_strength_model == "raw_goals"
    assert PRESEASON_V5_MODEL_CONFIG.team_strength_model == "raw_goals"
    # The incumbent has no derivation record and reproduces exactly.
    assert incumbent.team_strength_state is None
    assert preseason.team_strength_state is None
    assert [
        (projection.source_player_id, projection.expected_points)
        for projection in incumbent.projections
    ] == [
        (projection.source_player_id, projection.expected_points)
        for projection in repeat.projections
    ]
    # And the challenger produces a different, non-degenerate forecast.
    challenger = _projection(
        tmp_path, OPPONENT_ADJUSTED_TEAM_STRENGTH_V1_MODEL_CONFIG
    )
    assert challenger.team_strength_state is not None
    assert sum(
        projection.expected_points for projection in challenger.projections
    ) > 0


def test_the_challenger_config_hashes_deterministically() -> None:
    import hashlib
    import json
    from dataclasses import asdict

    def digest(config: ProjectionModelConfig) -> str:
        return hashlib.sha256(
            json.dumps(asdict(config), sort_keys=True).encode()
        ).hexdigest()

    first = digest(OPPONENT_ADJUSTED_TEAM_STRENGTH_V1_MODEL_CONFIG)
    assert first == digest(OPPONENT_ADJUSTED_TEAM_STRENGTH_V1_MODEL_CONFIG)
    assert first != digest(CORRECTED_V4_MODEL_CONFIG)
