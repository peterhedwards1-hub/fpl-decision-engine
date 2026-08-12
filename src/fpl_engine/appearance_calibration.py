"""Fit isotonic appearance-reliability maps from historical origins.

The two-stage estimator is well calibrated in aggregate at a preseason origin
but systematically overconfident in the tail that decision-making leans on:
the band it calls certain appears about four times in five. Because the exact
squad valuation prices autosubs off those probabilities, that overconfidence
propagates straight into bench value.

An isotonic map is the conservative correction. It is monotone, so it cannot
reorder players, and it is fitted per configuration because the raw
distributions differ — a map fitted for one minutes model does not transfer to
another.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .history.database import HistoricalDatabase

ARTIFACT_KIND = "appearance-isotonic-v1"


@dataclass(frozen=True)
class CalibrationFit:
    knots_x: tuple[float, ...]
    knots_y: tuple[float, ...]
    samples: int
    raw_brier: float
    calibrated_brier: float


def _pool_adjacent_violators(
    pairs: list[tuple[float, float]]
) -> list[tuple[float, float]]:
    """Isotonic regression by pool-adjacent-violators, weighted by count."""

    blocks: list[list[float]] = []  # [sum_y, weight, x_right]
    for x, y in pairs:
        blocks.append([y, 1.0, x])
        while len(blocks) > 1 and (
            blocks[-2][0] / blocks[-2][1] > blocks[-1][0] / blocks[-1][1]
        ):
            last = blocks.pop()
            blocks[-1][0] += last[0]
            blocks[-1][1] += last[1]
            blocks[-1][2] = last[2]
    return [(block[2], block[0] / block[1]) for block in blocks]


def fit_appearance_calibration(
    database: HistoricalDatabase,
    backtest_run_ids: tuple[int, ...],
    *,
    max_knots: int = 40,
) -> CalibrationFit:
    """Fit a monotone reliability map from stored backtest predictions."""

    if not backtest_run_ids:
        raise ValueError("At least one backtest run is required")
    placeholders = ",".join("?" * len(backtest_run_ids))
    rows = database.connection.execute(
        f"""
        SELECT appearance_probability AS p, (actual_minutes > 0) AS y
        FROM projection_backtest_predictions
        WHERE backtest_run_id IN ({placeholders})
          AND actual_minutes IS NOT NULL
          AND fixture_count = 1
        ORDER BY appearance_probability
        """,
        backtest_run_ids,
    ).fetchall()
    if not rows:
        raise ValueError("The requested backtest runs contain no scored rows")

    pairs = [(float(row["p"]), float(row["y"])) for row in rows]
    isotonic = _pool_adjacent_violators(pairs)

    # Thin to at most ``max_knots`` so the artifact stays readable, keeping the
    # endpoints and spreading the rest evenly across the fitted blocks.
    if len(isotonic) > max_knots:
        step = (len(isotonic) - 1) / (max_knots - 1)
        indexes = sorted({round(index * step) for index in range(max_knots)})
        isotonic = [isotonic[index] for index in indexes]
    knots_x = tuple(x for x, _ in isotonic)
    knots_y = tuple(y for _, y in isotonic)

    def apply(value: float) -> float:
        if value <= knots_x[0]:
            return knots_y[0]
        if value >= knots_x[-1]:
            return knots_y[-1]
        for index in range(1, len(knots_x)):
            if value <= knots_x[index]:
                left, right = knots_x[index - 1], knots_x[index]
                if right == left:
                    return knots_y[index]
                weight = (value - left) / (right - left)
                return knots_y[index - 1] + weight * (knots_y[index] - knots_y[index - 1])
        return knots_y[-1]

    raw = sum((p - y) ** 2 for p, y in pairs) / len(pairs)
    calibrated = sum((apply(p) - y) ** 2 for p, y in pairs) / len(pairs)
    return CalibrationFit(
        knots_x=knots_x,
        knots_y=knots_y,
        samples=len(pairs),
        raw_brier=round(raw, 6),
        calibrated_brier=round(calibrated, 6),
    )


def write_calibration_artifact(
    fit: CalibrationFit,
    output_path: str | Path,
    *,
    fitted_for: str,
    provenance: dict[str, Any],
) -> Path:
    """Persist a fit with the provenance needed to judge whether it applies."""

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "kind": ARTIFACT_KIND,
                "fitted_for": fitted_for,
                "fitted_at": datetime.now(UTC).isoformat(),
                "samples": fit.samples,
                "in_sample_raw_brier": fit.raw_brier,
                "in_sample_calibrated_brier": fit.calibrated_brier,
                "provenance": provenance,
                "knots": {"x": list(fit.knots_x), "y": list(fit.knots_y)},
            },
            indent=1,
        )
        + "\n",
        encoding="utf-8",
    )
    return path
