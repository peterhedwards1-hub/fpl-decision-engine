"""Purchase prices and the sale values the FPL half-profit rule implies.

A squad's spending power is not the market value of its players. FPL returns
the purchase price plus a configured share of any profit — `calculate_selling_price`
applies the season's rule — and the full current price when a player has lost
value. Treating the market price as the sale value overstates the bank after
every price rise, which makes transfer routes, budgets and replayed season
scores unreachable.

The ledger is the state that makes this computable: what each owned player cost
when they entered the squad, carried for as long as they are owned.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .config import SeasonRules
from .rules import calculate_selling_price


@dataclass(frozen=True)
class PurchaseLedger:
    """What each owned player cost, and what they would now sell for.

    Transitions are explicit because the chips differ. A Wildcard is an ordinary
    transfer transition with no hit: players kept through it retain their
    original purchase price, and only new arrivals are repriced. A Free Hit does
    not change ownership at all, so its temporary squad must leave the ledger
    untouched — see `unchanged_through_free_hit`.
    """

    purchase_prices_tenths: Mapping[str, int]

    def __post_init__(self) -> None:
        if any(price <= 0 for price in self.purchase_prices_tenths.values()):
            raise ValueError("Purchase prices must be positive tenths")
        object.__setattr__(
            self,
            "purchase_prices_tenths",
            dict(self.purchase_prices_tenths),
        )

    @property
    def player_ids(self) -> frozenset[str]:
        return frozenset(self.purchase_prices_tenths)

    @classmethod
    def from_entry_prices(
        cls,
        entry_prices_tenths: Mapping[str, int],
    ) -> PurchaseLedger:
        """Open a ledger for a squad bought at the supplied prices."""

        return cls(purchase_prices_tenths=dict(entry_prices_tenths))

    def purchase_price(self, player_id: str) -> int:
        try:
            return self.purchase_prices_tenths[player_id]
        except KeyError as error:
            raise ValueError(
                f"Player {player_id!r} is not owned, so has no purchase price"
            ) from error

    def selling_price(
        self,
        player_id: str,
        current_tenths: int,
        rules: SeasonRules,
    ) -> int:
        return calculate_selling_price(
            self.purchase_price(player_id),
            current_tenths,
            rules,
        )

    def selling_prices(
        self,
        current_prices_tenths: Mapping[str, int],
        rules: SeasonRules,
    ) -> dict[str, int]:
        """Sale value of every owned player at the supplied market prices."""

        missing = self.player_ids - set(current_prices_tenths)
        if missing:
            raise ValueError(
                "Current prices are missing for owned players: "
                + ", ".join(sorted(missing))
            )
        return {
            player_id: self.selling_price(
                player_id, current_prices_tenths[player_id], rules
            )
            for player_id in sorted(self.purchase_prices_tenths)
        }

    def squad_value_tenths(
        self,
        current_prices_tenths: Mapping[str, int],
        rules: SeasonRules,
    ) -> int:
        return sum(self.selling_prices(current_prices_tenths, rules).values())

    def after_transfers(
        self,
        *,
        sold: frozenset[str] | set[str],
        bought: Mapping[str, int],
    ) -> PurchaseLedger:
        """Apply a set of transfers, repricing only the players who arrive.

        A player sold and later repurchased is bought at the price ruling then,
        because they leave the ledger on the way out.
        """

        unknown = set(sold) - self.player_ids
        if unknown:
            raise ValueError(
                "Cannot sell players who are not owned: " + ", ".join(sorted(unknown))
            )
        retained = self.player_ids - set(sold)
        already_owned = retained & set(bought)
        if already_owned:
            raise ValueError(
                "Cannot buy players who are already owned: "
                + ", ".join(sorted(already_owned))
            )
        return PurchaseLedger(
            purchase_prices_tenths={
                **{
                    player_id: self.purchase_prices_tenths[player_id]
                    for player_id in retained
                },
                **dict(bought),
            }
        )

    def unchanged_through_free_hit(self) -> PurchaseLedger:
        """A Free Hit squad is temporary, so ownership and prices are unaffected.

        Returning the same ledger is the whole point: the temporary squad must
        not be allowed to reprice anything for the Gameweek that follows.
        """

        return self

    def as_dict(self) -> dict[str, Any]:
        return {
            "purchase_prices_tenths": dict(
                sorted(self.purchase_prices_tenths.items())
            ),
        }
