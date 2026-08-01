from __future__ import annotations

from pathlib import Path

import pytest

from fpl_engine.config import load_season_rules
from fpl_engine.pricing import PurchaseLedger
from fpl_engine.rules import calculate_selling_price

RULES = load_season_rules(Path("config/seasons/2026-27.json"))


def selling_price_tenths(purchase: int, current: int) -> int:
    return calculate_selling_price(purchase, current, RULES)


@pytest.mark.parametrize(
    ("purchase", "current", "expected"),
    (
        # No movement: the price you paid is the price you get.
        (100, 100, 100),
        # A fall is taken in full.
        (100, 96, 96),
        (100, 50, 50),
        # Profit is halved and rounded down to the nearest £0.1m, so an odd
        # number of tenths keeps the loose tenth with the game, not the manager.
        (100, 102, 101),
        (100, 103, 101),
        (100, 104, 102),
        (100, 105, 102),
        (45, 51, 48),
        (120, 131, 125),
    ),
)
def test_half_profit_rule(purchase: int, current: int, expected: int) -> None:
    assert selling_price_tenths(purchase, current) == expected


def test_a_rise_is_never_worth_its_full_value() -> None:
    for rise in range(1, 40):
        purchase = 80
        sale = selling_price_tenths(purchase, purchase + rise)
        assert purchase <= sale < purchase + rise


def test_the_configured_rule_is_used_rather_than_a_hardcoded_half() -> None:
    # 2026/27 returns 1 tenth per 2 tenths of profit. The ledger must read that
    # from the season rules, not assume it.
    assert RULES.selling_prices.profit_step_tenths == 2
    assert RULES.selling_prices.profit_return_tenths == 1
    ledger = PurchaseLedger.from_entry_prices({"a": 100})
    assert ledger.selling_price("a", 107, RULES) == calculate_selling_price(
        100, 107, RULES
    )


def test_a_ledger_prices_the_squad_it_holds() -> None:
    ledger = PurchaseLedger.from_entry_prices({"a": 100, "b": 50, "c": 75})

    prices = ledger.selling_prices({"a": 108, "b": 47, "c": 75}, RULES)

    assert prices == {"a": 104, "b": 47, "c": 75}
    assert ledger.squad_value_tenths({"a": 108, "b": 47, "c": 75}, RULES) == 226
    with pytest.raises(ValueError, match="missing for owned players"):
        ledger.selling_prices({"a": 108}, RULES)
    with pytest.raises(ValueError, match="not owned"):
        ledger.purchase_price("d")


def test_a_transfer_reprices_only_the_arrival() -> None:
    ledger = PurchaseLedger.from_entry_prices({"a": 100, "b": 50})

    after = ledger.after_transfers(sold={"b"}, bought={"c": 62})

    # "a" keeps the price it was bought at, so its later sale still halves the
    # profit rather than starting again from the current market price.
    assert after.purchase_prices_tenths == {"a": 100, "c": 62}
    assert after.selling_prices({"a": 110, "c": 62}, RULES) == {"a": 105, "c": 62}


def test_holding_through_a_rise_beats_selling_and_rebuying() -> None:
    held = PurchaseLedger.from_entry_prices({"a": 100})
    # The same player, sold at 110 and bought straight back at that price.
    rebought = held.after_transfers(sold={"a"}, bought={}).after_transfers(
        sold=set(), bought={"a": 110}
    )

    assert held.selling_prices({"a": 120}, RULES) == {"a": 110}
    # Repurchase resets the basis, so the second rise is halved from 110.
    assert rebought.selling_prices({"a": 120}, RULES) == {"a": 115}


def test_a_ledger_refuses_impossible_transitions() -> None:
    ledger = PurchaseLedger.from_entry_prices({"a": 100})

    with pytest.raises(ValueError, match="not owned"):
        ledger.after_transfers(sold={"z"}, bought={})
    with pytest.raises(ValueError, match="already owned"):
        ledger.after_transfers(sold=set(), bought={"a": 105})
    with pytest.raises(ValueError, match="positive"):
        PurchaseLedger.from_entry_prices({"a": 0})


def test_a_wildcard_is_an_ordinary_repricing_of_arrivals_only() -> None:
    ledger = PurchaseLedger.from_entry_prices({"a": 100, "b": 50, "c": 75})

    # Unlimited transfers, but "a" is kept, so it holds its original basis.
    wildcarded = ledger.after_transfers(
        sold={"b", "c"}, bought={"d": 55, "e": 80}
    )

    assert wildcarded.purchase_prices_tenths == {"a": 100, "d": 55, "e": 80}
    assert wildcarded.selling_prices({"a": 112, "d": 55, "e": 80}, RULES)["a"] == 106


def test_a_free_hit_leaves_the_ledger_untouched() -> None:
    ledger = PurchaseLedger.from_entry_prices({"a": 100, "b": 50})

    after = ledger.unchanged_through_free_hit()

    # The temporary squad must not reprice anything for the following Gameweek.
    assert after.purchase_prices_tenths == ledger.purchase_prices_tenths
    assert after is ledger
