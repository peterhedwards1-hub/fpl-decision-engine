"""What previous-division minutes may and may not be used for.

Two things have to hold whatever the data turn out to look like. Championship
scoring must be unable to reach a Premier League scoring projection, and a
player must never inherit another footballer's season because two people share
a name. Both are asserted here rather than left to the calling code.
"""

from __future__ import annotations

import json

import pytest

from fpl_engine.domain import Position
from fpl_engine.optimisation import CandidatePlayer, GameweekPlayerValue
from fpl_engine.promoted_roles import (
    DEFAULT_ROLE_PRIOR_MATCHES,
    ChampionshipPlayerRole,
    RoleDataError,
    apply_role_estimates,
    load_role_document,
    match_roles,
    role_coverage,
    shrink_role_evidence,
)


def _role(
    name: str = "Regular",
    *,
    club: str = "Promoted FC",
    starts: int = 44,
    substitutes: int = 2,
    minutes: int = 3900,
    team_matches: int = 46,
    code: str | None = None,
) -> ChampionshipPlayerRole:
    return ChampionshipPlayerRole(
        club_name=club,
        player_name=name,
        official_fpl_code=code,
        appearances=starts + substitutes,
        starts=starts,
        substitute_appearances=substitutes,
        minutes=minutes,
        team_matches=team_matches,
    )


def _document(players: list[dict]) -> dict:
    return {
        "format": "fpl-decision-engine/championship-player-roles/v1",
        "season_code": "2025-26",
        "source": {
            "name": "synthetic",
            "url": "https://example.invalid/roles",
        },
        "players": players,
    }


def _player(**overrides) -> dict:
    payload = {
        "club_name": "Promoted FC",
        "player_name": "Regular",
        "appearances": 46,
        "starts": 44,
        "substitute_appearances": 2,
        "minutes": 3900,
        "team_matches": 46,
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------------
# The scoring firewall
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field", ["goals", "assists", "clean_sheets", "total_points", "expected_goals"]
)
def test_a_role_file_carrying_scoring_is_refused(tmp_path, field) -> None:
    """Not ignored — refused.

    A file with a goals column was built for a different purpose. Loading it
    and quietly dropping the column would leave the next reader believing the
    conversion had been considered and rejected, when in fact nobody looked.
    """

    path = tmp_path / "roles.json"
    path.write_text(json.dumps(_document([_player(**{field: 12})])), encoding="utf-8")

    with pytest.raises(RoleDataError, match="may not carry scoring fields"):
        load_role_document(path)


def test_the_storage_table_has_no_column_for_scoring(tmp_path) -> None:
    """The firewall is structural, not a convention someone has to remember."""

    from fpl_engine.history.database import HistoricalDatabase

    with HistoricalDatabase(tmp_path / "fpl.sqlite3") as database:
        database.initialise()
        columns = {
            str(row["name"])
            for row in database.connection.execute(
                "PRAGMA table_info(championship_player_roles)"
            )
        }

    assert columns.isdisjoint(
        {
            "goals",
            "assists",
            "clean_sheets",
            "points",
            "total_points",
            "bonus",
            "expected_goals",
        }
    )
    assert {"appearances", "starts", "substitute_appearances", "minutes"} <= columns


def test_inconsistent_appearance_arithmetic_is_refused(tmp_path) -> None:
    path = tmp_path / "roles.json"
    path.write_text(
        json.dumps(_document([_player(starts=40, substitute_appearances=2)])),
        encoding="utf-8",
    )

    with pytest.raises(RoleDataError, match="but 46 appearances"):
        load_role_document(path)


def test_a_player_cannot_appear_more_often_than_the_club_played(tmp_path) -> None:
    path = tmp_path / "roles.json"
    path.write_text(
        json.dumps(
            json.loads(
                json.dumps(
                    _document(
                        [
                            _player(
                                appearances=50,
                                starts=48,
                                substitute_appearances=2,
                                team_matches=46,
                            )
                        ]
                    )
                )
            )
        ),
        encoding="utf-8",
    )

    with pytest.raises(RoleDataError, match="appeared in 50 of 46"):
        load_role_document(path)


# --------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------


def test_the_same_name_at_two_clubs_never_cross_matches() -> None:
    roles = (_role("Smith", club="Promoted FC"),)
    players = (
        {
            "source_player_id": "wrong",
            "club_name": "Established FC",
            "name": "Smith",
            "official_fpl_code": None,
        },
    )

    matches, unresolved = match_roles(roles, players)

    assert matches == {}
    assert unresolved[0]["reason"] == "no Premier League player matched"


def test_an_ambiguous_name_matches_nobody() -> None:
    """A wrong match fabricates a role for a real footballer.

    The two failure modes are not comparable: a missed match leaves a player on
    the prior they already had, and a wrong match attributes somebody else's
    season to them.
    """

    roles = (_role("Smith"),)
    players = (
        {
            "source_player_id": "one",
            "club_name": "Promoted FC",
            "name": "Smith",
            "official_fpl_code": None,
        },
        {
            "source_player_id": "two",
            "club_name": "Promoted FC",
            "name": "Smith",
            "official_fpl_code": None,
        },
    )

    matches, unresolved = match_roles(roles, players)

    assert matches == {}
    assert "ambiguous identity" in unresolved[0]["reason"]


def test_an_official_code_beats_a_name() -> None:
    roles = (_role("Smyth", code="12345"),)
    players = (
        {
            "source_player_id": "right",
            "club_name": "Promoted FC",
            "name": "Smith",
            "official_fpl_code": "12345",
        },
    )

    matches, _ = match_roles(roles, players)

    assert matches["right"].method == "official_fpl_code"


def test_accents_and_punctuation_do_not_prevent_a_match() -> None:
    roles = (_role("Muñoz"),)
    players = (
        {
            "source_player_id": "one",
            "club_name": "Promoted FC",
            "name": "Munoz",
            "official_fpl_code": None,
        },
    )

    matches, _ = match_roles(roles, players)

    assert matches["one"].method == "club_and_exact_name"


# --------------------------------------------------------------------------
# Shrinkage
# --------------------------------------------------------------------------


def _estimate(role: ChampionshipPlayerRole, **overrides):
    payload = {
        "source_player_id": "player",
        "position": Position.MID,
        "prior_appearance_probability": 0.5,
        "prior_sixty_probability": 0.4,
        "prior_expected_minutes": 40.0,
    }
    payload.update(overrides)
    return shrink_role_evidence(role, **payload)


def test_a_full_season_of_starts_moves_the_estimate_toward_the_evidence() -> None:
    estimate = _estimate(_role(starts=44, substitutes=2, minutes=3900))

    assert estimate.appearance_probability > 0.5
    assert estimate.start_probability > 0.5
    assert estimate.expected_minutes > 40.0
    # Never all the way: the prior still counts for its declared matches.
    assert estimate.appearance_probability < 46 / 46


def test_a_fringe_season_moves_the_estimate_the_other_way() -> None:
    estimate = _estimate(_role(starts=2, substitutes=6, minutes=400))

    assert estimate.appearance_probability < 0.5
    assert estimate.start_probability < 0.5
    assert estimate.expected_minutes < 40.0


def test_thin_evidence_barely_moves_anything() -> None:
    """Shrinkage has to be a function of how much was actually seen."""

    thin = _estimate(
        _role(starts=4, substitutes=0, minutes=360, team_matches=4)
    )
    thick = _estimate(_role(starts=46, substitutes=0, minutes=4140))

    assert abs(thin.appearance_probability - 0.5) < abs(
        thick.appearance_probability - 0.5
    )
    assert thin.shrinkage_weight < thick.shrinkage_weight
    assert thin.shrinkage_weight == pytest.approx(
        4 / (4 + DEFAULT_ROLE_PRIOR_MATCHES)
    )


def test_a_substitute_specialist_is_not_credited_with_sixty_minutes() -> None:
    """Reading starts separately from appearances is the point of the exercise."""

    starter = _estimate(_role(starts=46, substitutes=0, minutes=4140))
    substitute = _estimate(_role(starts=0, substitutes=46, minutes=690))

    assert substitute.appearance_probability > 0.5
    assert substitute.sixty_probability < starter.sixty_probability
    assert substitute.sixty_probability < substitute.appearance_probability


def test_applying_an_estimate_rescales_points_by_availability_only() -> None:
    player = CandidatePlayer(
        source_player_id="player",
        web_name="Player",
        team_id="1",
        team_short_name="PRO",
        position=Position.MID,
        price_tenths=45,
        expected_points=10.0,
        gameweek_expected_points=5.0,
        appearance_probability=0.5,
        gameweek_values=(
            GameweekPlayerValue(1, 5.0, 0.5, 0.4),
            GameweekPlayerValue(2, 5.0, 0.5, 0.4),
        ),
    )
    estimate = _estimate(_role(starts=46, substitutes=0, minutes=4140))

    adjusted = apply_role_estimates((player,), {"player": estimate})[0]

    ratio = estimate.appearance_probability / 0.5
    assert adjusted.appearance_probability == pytest.approx(
        estimate.appearance_probability
    )
    assert adjusted.expected_points == pytest.approx(10.0 * ratio)
    assert all(
        value.expected_points == pytest.approx(5.0 * ratio)
        for value in adjusted.gameweek_values
    )


def test_a_player_without_evidence_is_returned_untouched() -> None:
    player = CandidatePlayer(
        source_player_id="other",
        web_name="Other",
        team_id="1",
        team_short_name="PRO",
        position=Position.MID,
        price_tenths=45,
        expected_points=10.0,
        appearance_probability=0.5,
    )

    assert apply_role_estimates((player,), {}) == (player,)


# --------------------------------------------------------------------------
# The coverage gate
# --------------------------------------------------------------------------


def test_thin_coverage_refuses_the_treatment_rather_than_half_applying_it() -> None:
    """Half a cohort is worse than none.

    Moving some promoted players and leaving their direct competitors on the
    positional prior changes the ordering between them for a reason that is
    about data availability rather than football.
    """

    coverage = role_coverage(
        eligible_promoted_players=90, matched_players=10, unresolved=[]
    )

    assert coverage["sufficient"] is False
    assert "insufficient" in coverage["verdict"]


def test_full_coverage_permits_the_treatment() -> None:
    coverage = role_coverage(
        eligible_promoted_players=90, matched_players=85, unresolved=[]
    )

    assert coverage["sufficient"] is True


def test_no_evidence_at_all_is_reported_as_zero_coverage() -> None:
    coverage = role_coverage(
        eligible_promoted_players=0, matched_players=0, unresolved=[]
    )

    assert coverage["sufficient"] is False
    assert coverage["coverage"] == 0.0
