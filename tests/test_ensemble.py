import pytest

from fpl_engine.ensemble import OOFEnsembleRow, fit_constrained_ensemble


def test_constrained_ensemble_recovers_nonnegative_simplex_weights() -> None:
    rows = tuple(
        OOFEnsembleRow(
            season_code="2026-27",
            gameweek=gameweek,
            actual=float(gameweek),
            predictions={
                "strong": float(gameweek) + (0.1 if gameweek % 2 else -0.1),
                "weak": float(gameweek) + 3.0,
            },
            trained_through={
                "strong": ("2026-27", gameweek - 1),
                "weak": ("2026-27", gameweek - 1),
            },
        )
        for gameweek in range(2, 20)
    )

    ensemble = fit_constrained_ensemble(rows)

    assert sum(ensemble.weights) == pytest.approx(1.0)
    assert all(weight >= 0 for weight in ensemble.weights)
    assert ensemble.weights[ensemble.model_names.index("strong")] > 0.95
    assert ensemble.training_rmse < ensemble.individual_rmse["weak"]


def test_constrained_ensemble_rejects_non_oof_predictions() -> None:
    row = OOFEnsembleRow(
        season_code="2026-27",
        gameweek=2,
        actual=3.0,
        predictions={"a": 2.0, "b": 4.0},
        trained_through={
            "a": ("2026-27", 2),
            "b": ("2026-27", 1),
        },
    )

    with pytest.raises(ValueError, match="leakage"):
        fit_constrained_ensemble((row,))
