from dataclasses import replace
from pathlib import Path

from fpl_engine import (
    Chip,
    Player,
    PlayerGameweekStats,
    Position,
    Squad,
    allocate_bonus_points,
    calculate_player_points,
    calculate_selling_price,
    calculate_team_score,
    calculate_transfer_cost,
    load_season_rules,
    next_free_transfer_count,
    resolve_automatic_substitutions,
    validate_chip_use,
    validate_squad,
)


RULES = load_season_rules(Path("config/seasons/2026-27.json"))


def test_historical_projection_rules_inherit_pre_2025_scoring() -> None:
    rules = load_season_rules(Path("config/seasons/2024-25.json"))

    assert rules.season == "2024-25"
    assert rules.scoring.defensive_contribution_points == 0
    assert rules.transfers.maximum_free_transfers == 5
    assert rules.chips.banked_transfers_preserved is True


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
        bench_player_ids=(2, 7, 12, 15),
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

    two_conceded_in_45 = PlayerGameweekStats(minutes=45, goals_conceded=2)
    three_conceded = PlayerGameweekStats(minutes=90, goals_conceded=3)
    four_conceded = PlayerGameweekStats(minutes=90, goals_conceded=4)

    assert calculate_player_points(player, two_conceded_in_45, RULES) == 0
    assert calculate_player_points(player, three_conceded, RULES) == 1
    assert calculate_player_points(player, four_conceded, RULES) == 0


def test_selling_price_returns_half_of_profit_rounded_down() -> None:
    assert calculate_selling_price(50, 49, RULES) == 49
    assert calculate_selling_price(50, 50, RULES) == 50
    assert calculate_selling_price(50, 51, RULES) == 50
    assert calculate_selling_price(50, 52, RULES) == 51
    assert calculate_selling_price(50, 53, RULES) == 51
    assert calculate_selling_price(50, 54, RULES) == 52


def test_transfer_cost_and_free_transfer_bank_obey_cap_and_chips() -> None:
    assert calculate_transfer_cost(3, 1, RULES) == 8
    assert calculate_transfer_cost(3, 1, RULES, active_chip=Chip.WILDCARD) == 0
    assert next_free_transfer_count(1, 0, RULES) == 2
    assert next_free_transfer_count(5, 0, RULES) == 5
    assert next_free_transfer_count(5, 1, RULES) == 5
    assert next_free_transfer_count(3, 5, RULES) == 1
    assert (
        next_free_transfer_count(4, 12, RULES, active_chip=Chip.FREE_HIT)
        == 4
    )


def test_chip_availability_and_cooldown_are_configured() -> None:
    assert {
        error.code
        for error in validate_chip_use(Chip.FREE_HIT, 1, RULES)
    } == {"chip_unavailable"}
    assert {
        error.code
        for error in validate_chip_use(
            Chip.FREE_HIT,
            20,
            RULES,
            previous_gameweek_chip=Chip.FREE_HIT,
        )
    } == {"chip_cooldown"}
    assert {
        error.code
        for error in validate_chip_use(
            Chip.BENCH_BOOST,
            10,
            RULES,
            already_used_in_half=frozenset({Chip.BENCH_BOOST}),
        )
    } == {"chip_already_used"}
    three_gameweek_gap = replace(
        RULES,
        chips=replace(
            RULES.chips,
            minimum_gap_gameweeks={Chip.FREE_HIT.value: 3},
        ),
    )
    assert {
        error.code
        for error in validate_chip_use(
            Chip.FREE_HIT,
            20,
            three_gameweek_gap,
            last_used_gameweek=17,
        )
    } == {"chip_cooldown"}
    assert not validate_chip_use(
        Chip.FREE_HIT,
        21,
        three_gameweek_gap,
        last_used_gameweek=17,
    )


def test_bonus_allocation_handles_ties_at_each_rank() -> None:
    assert allocate_bonus_points({1: 30, 2: 30, 3: 20, 4: 10}) == {
        1: 3,
        2: 3,
        3: 1,
        4: 0,
    }
    assert allocate_bonus_points({1: 30, 2: 20, 3: 20, 4: 10}) == {
        1: 3,
        2: 2,
        3: 2,
        4: 0,
    }


def test_autosubs_skip_an_ineligible_higher_priority_forward() -> None:
    legal = make_legal_squad()
    three_defender_squad = Squad(
        players=legal.players,
        starting_player_ids=frozenset(
            {1, 3, 4, 5, 8, 9, 10, 11, 12, 13, 14}
        ),
        bench_player_ids=(2, 15, 6, 7),
        captain_id=8,
        vice_captain_id=13,
    )
    minutes = {player.player_id: 90 for player in legal.players}
    minutes[3] = 0

    resolved = resolve_automatic_substitutions(
        three_defender_squad, minutes, RULES
    )

    assert 15 not in resolved.scoring_player_ids
    assert 6 in resolved.scoring_player_ids
    assert resolved.substitutions == ((3, 6),)


def test_team_score_applies_goalkeeper_autosub_and_vice_captain() -> None:
    squad = make_legal_squad()
    minutes = {player.player_id: 90 for player in squad.players}
    minutes[1] = 0
    minutes[8] = 0
    points = {player.player_id: 2 for player in squad.players}
    points[13] = 7

    score = calculate_team_score(squad, points, minutes, RULES)

    assert (1, 2) in score.substitutions
    assert score.effective_captain_id == 13
    assert score.total_points == 34


def test_scoring_chips_apply_bench_and_triple_captain_points() -> None:
    squad = make_legal_squad()
    minutes = {player.player_id: 90 for player in squad.players}
    points = {player.player_id: 2 for player in squad.players}
    points[8] = 10

    normal = calculate_team_score(squad, points, minutes, RULES)
    triple = calculate_team_score(
        squad,
        points,
        minutes,
        RULES,
        active_chip=Chip.TRIPLE_CAPTAIN,
    )
    bench_boost = calculate_team_score(
        squad,
        points,
        minutes,
        RULES,
        active_chip=Chip.BENCH_BOOST,
    )

    assert normal.total_points == 40
    assert triple.total_points == 50
    assert bench_boost.total_points == 48
