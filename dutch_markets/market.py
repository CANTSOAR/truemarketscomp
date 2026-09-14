"""Orchestrates the three-phase lifecycle: Floating -> Challenge/Auction -> Floating."""
from __future__ import annotations

from enum import Enum, auto

from .agents import Arbitrageur, Challenger
from .auction import DutchAuction
from .vamm import VAMM


class Phase(Enum):
    FLOATING = auto()
    AUCTION = auto()


class Market:
    """A single self-contained market. The "Challenge" is instantaneous (bond posted,
    trading frozen, auction launched in the same tick) so only FLOATING and AUCTION
    are modeled as distinct states.

    `reserve` is the protocol's fee-funded pool (see Simulation's fee_bps): it pays
    the challenger bounty on a fill, and doubles as an insurance fund absorbing bad
    debt from violent re-ratings (see absorb_bad_debt). If it's ever insufficient,
    the shortfall is tracked in unfunded_bounty / unfunded_bad_debt rather than
    silently made whole -- that's the honest answer to "where does the money come
    from": fee revenue, until it isn't enough.
    """

    def __init__(self, vamm: VAMM, challenge_threshold: float = 0.05,
                 auction_range_pct: float = 0.30, auction_duration: int = 20,
                 bounty_pct: float = 0.10, max_auction_retries: int = 3,
                 retry_widen_factor: float = 1.75):
        self.vamm = vamm
        self.challenge_threshold = challenge_threshold
        self.auction_range_pct = auction_range_pct
        self.auction_duration = auction_duration
        self.bounty_pct = bounty_pct
        self.max_auction_retries = max_auction_retries
        self.retry_widen_factor = retry_widen_factor

        self.phase = Phase.FLOATING
        self.active_auction: DutchAuction | None = None
        self.events: list[dict] = []

        self.reserve = 0.0
        self.unfunded_bounty = 0.0
        self.unfunded_bad_debt = 0.0
        self._pending_bond_amount = 0.0
        self._auction_retries = 0

    def collect_fee(self, fee: float) -> None:
        self.reserve += fee

    def absorb_bad_debt(self, shortfall: float) -> None:
        if shortfall <= 0:
            return
        covered = min(shortfall, self.reserve)
        self.reserve -= covered
        self.unfunded_bad_debt += shortfall - covered

    def trigger_challenge(self, challenger: Challenger, true_price: float, tvl: float, tick: int) -> None:
        divergence = challenger.divergence(self.vamm.price, true_price)
        bond_pct = challenger.bond_pct(divergence)
        direction = "up" if self.vamm.price < true_price else "down"

        self.active_auction = DutchAuction(
            self.vamm.price, direction, self.auction_range_pct, self.auction_duration
        )
        self.phase = Phase.AUCTION
        self._auction_retries = 0
        self._pending_bond_amount = bond_pct * tvl
        self.events.append({
            "tick": tick, "type": "challenge",
            "vamm_price": self.vamm.price, "true_price": true_price,
            "divergence": divergence, "bond_pct": bond_pct,
            "bond_amount": self._pending_bond_amount, "direction": direction,
        })

    def step_auction(self, arbitrageur: Arbitrageur, true_price: float, tick: int) -> None:
        auc = self.active_auction
        price_now = auc.price_now()
        estimate = arbitrageur.estimate_true_price(true_price)

        if arbitrageur.wants_to_fill(price_now, auc.direction, estimate):
            self.vamm.rerate(price_now)
            # Bounty is a cut of the *reserve*, not of the (much larger, TVL-scaled)
            # bond -- sizing it off the bond would let payouts run far ahead of the
            # fee income that's supposed to fund them. Sizing off the reserve makes
            # the payout self-limiting by construction: it can never exceed what fee
            # revenue has actually accumulated.
            bounty = self.bounty_pct * self.reserve
            paid = min(bounty, self.reserve)
            self.reserve -= paid
            self.unfunded_bounty += bounty - paid
            self.events.append({
                "tick": tick, "type": "fill",
                "fill_price": price_now, "true_price": true_price,
                "auction_age": auc.t, "retries": self._auction_retries,
                "bounty_paid": paid, "bounty_shortfall": bounty - paid,
            })
            self.phase = Phase.FLOATING
            self.active_auction = None
            return

        expired = auc.step()
        if not expired:
            return

        if self._auction_retries < self.max_auction_retries:
            # No arbitrageur crossed the range -- the true price probably moved
            # further than price_range_pct allowed. Widen and try again rather than
            # settling on a price nobody was willing to trade at.
            self._auction_retries += 1
            widened_range = self.auction_range_pct * (self.retry_widen_factor ** self._auction_retries)
            self.active_auction = DutchAuction(
                auc.frozen_price, auc.direction, widened_range, self.auction_duration
            )
            self.events.append({
                "tick": tick, "type": "retry",
                "retry_number": self._auction_retries, "widened_range_pct": widened_range,
            })
        else:
            # Circuit breaker: give up widening, settle at the last boundary reached,
            # and resume floating still mispriced. A rare, deliberately visible failure
            # mode rather than an infinite freeze.
            settle_price = auc.price_now()
            self.vamm.rerate(settle_price)
            self.events.append({
                "tick": tick, "type": "circuit_breaker_settle",
                "settle_price": settle_price, "true_price": true_price,
                "retries": self._auction_retries,
            })
            self.phase = Phase.FLOATING
            self.active_auction = None
