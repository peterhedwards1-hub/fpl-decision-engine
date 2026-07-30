from pathlib import Path

from fpl_engine.config import load_season_rules
from fpl_engine.domain import Player, Position, Squad
from fpl_engine.simulation import (
    DistributionForecastOutcome,
    FixtureSimulationInput,
    PlayerSimulationInput,
    SquadSimulationInput,
    evaluate_simulation_calibration,
    simulate_squads,
)

RULES = load_season_rules(Path("config/seasons/2026-27.json"))


def _simulation_inputs():
    positions = (
        Position.GK,
        Position.GK,
        *(Position.DEF for _ in range(5)),
        *(Position.MID for _ in range(5)),
        *(Position.FWD for _ in range(3)),
    )
    players = tuple(
        Player(index, f"P{index}", (index - 1) // 3 + 1, position, 50)
        for index, position in enumerate(positions, start=1)
    )
    inputs = tuple(
        PlayerSimulationInput(
            player=player,
            appearance_probability=0.92,
            sixty_probability_given_appearance=0.8,
            conditional_minutes=75,
            goal_share=1 / 3,
            assist_share=1 / 3,
            saves_per_90=3.0 if player.position == Position.GK else 0.0,
            defensive_contributions_per_90=(
                7.0 if player.position == Position.DEF else 3.0
            ),
        )
        for player in players
    )
    fixtures = tuple(
        FixtureSimulationInput(
            fixture_id=str(team),
            home_team_id=team,
            away_team_id=team + 10,
            home_expected_goals=1.5,
            away_expected_goals=1.1,
        )
        for team in range(1, 6)
    )
    squad = Squad(
        players=players,
        starting_player_ids=frozenset(
            {1, 3, 4, 5, 6, 8, 9, 10, 11, 13, 14}
        ),
        bench_player_ids=(2, 7, 12, 15),
        captain_id=13,
        vice_captain_id=10,
    )
    return fixtures, inputs, squad


def test_joint_simulation_is_seeded_and_scores_identical_squads_together() -> None:
    fixtures, players, squad = _simulation_inputs()

    result = simulate_squads(
        fixtures,
        players,
        (
            SquadSimulationInput("A", squad),
            SquadSimulationInput("B", squad),
        ),
        rules=RULES,
        iterations=500,
        seed=17,
    )
    repeat = simulate_squads(
        fixtures,
        players,
        (SquadSimulationInput("A", squad),),
        rules=RULES,
        iterations=500,
        seed=17,
    )

    assert result.distributions["A"] == repeat.distributions["A"]
    assert result.distributions["A"].mean > 0
    assert result.distributions["A"].standard_deviation > 0
    assert result.pairwise_win_probabilities["A>B"] == 0.5


def test_simulation_calibration_reports_proper_scores_and_coverage() -> None:
    report = evaluate_simulation_calibration(
        tuple(
            DistributionForecastOutcome(
                samples=tuple(range(20, 120)),
                actual_points=50 + index,
            )
            for index in range(10)
        )
    )

    assert report.forecasts == 10
    assert report.mean_crps > 0
    assert 0 <= report.coverage_80 <= 1
    assert sum(report.pit_bins) == 10
