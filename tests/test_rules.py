from pathlib import Path

from fpl_engine import (
    Player,
    PlayerGameweekStats,
    Position,
    Squad,
    calculate_player_points,
    load_season_rules,
    validate_squad,
)


RULES = load_season_rules(Path("config/seasons/2026-27.json"))


def make_legal_squad() -> Squad:
    players: list[Player] = []
    player_id = 1
    prices = {
        Position.GK: 45,
        Position.DEF: 45,
        Position.MID: 60,
        Position.FWD: 65,
    }
    counts = {
        Position.GK: 2,
        Position.DEF: 5,
        Position.MID: 5,
        Position.FWD: 3,
    }

    for position, count in counts.items():
        for _ in range(count):
            players.append(
                Player(
                    player_id=player_id,
                    name=f"Player {player_id}",
                    team_id=((player_id - 1) % 5) + 1,
                    position=position,
                    price_tenths=prices[position],
                )
            )
            player_id += 1

    starters = frozenset({1, 3, 4, 5, 6, 8, 9, 10, 11, 13, 14})
    return Squad(
        players=tuple(players),
        starting_player_ids=starters,
        captain_id=8,
        vice_captain_id=13,
    )


def test_legal_squad_and_formation_pass_validation() -> None:
    assert validate_squad(make_legal_squad(), RULES) == ()


def test_squad_validation_reports_multiple_failures() -> None:
    legal = make_legal_squad()
    expensive_duplicate = Player(1, "Duplicate", 1, Position.MID, 500)
    invalid = Squad(players=legal.players[:-1] + (expensive_duplicate,))

    codes = {error.code for error in validate_squad(invalid, RULES)}

    assert "duplicate_player" in codes
    assert "position_count" in codes
    assert "club_limit" in codes
    assert "budget" in codes


def test_invalid_two_defender_formation_is_rejected() -> None:
    legal = make_legal_squad()
    invalid = Squad(
        players=legal.players,
        starting_player_ids=frozenset({1, 3, 4, 8, 9, 10, 11, 12, 13, 14, 15}),
        captain_id=8,
        vice_captain_id=13,
    )

    assert "formation" in {error.code for error in validate_squad(invalid, RULES)}


def test_defender_points_include_goal_clean_sheet_and_contributions() -> None:
    player = Player(1, "Defender", 1, Position.DEF, 50)
    stats = PlayerGameweekStats(
        minutes=90,
        goals=1,
        assists=1,
        clean_sheet=True,
        bonus=3,
        defensive_contributions=10,
    )

    assert calculate_player_points(player, stats, RULES) == 20


def test_goalkeeper_points_include_saves_and_penalty_save() -> None:
    player = Player(1, "Goalkeeper", 1, Position.GK, 50)
    stats = PlayerGameweekStats(
        minutes=90,
        clean_sheet=True,
        saves=7,
        penalties_saved=1,
        bonus=2,
    )

    assert calculate_player_points(player, stats, RULES) == 15


def test_goals_conceded_deduction_uses_complete_pairs() -> None:
    player = Player(1, "Defender", 1, Position.DEF, 50)

    three_conceded = PlayerGameweekStats(minutes=90, goals_conceded=3)
    four_conceded = PlayerGameweekStats(minutes=90, goals_conceded=4)

    assert calculate_player_points(player, three_conceded, RULES) == 1
    assert calculate_player_points(player, four_conceded, RULES) == 0
