"""Participants in the simulation: dumb traders, a challenger, an arbitrageur."""
from __future__ import annotations

import numpy as np

from .vamm import VAMM


class CoinflipTrader:
    """A trader with zero information edge: a coin flip decides direction, size is random.

    Exists to isolate the *mechanism's* behavior from any trading skill -- if the
    vAMM still tracks the true price reasonably well with only noise traders and
    the challenge/auction loop for correction, that's the mechanism doing the work,
    not smart money.
    """

    def __init__(self, trader_id: int, collateral: float = 10_000.0,
                 trade_prob: float = 0.3, notional_mean: float = 500.0,
                 rng: np.random.Generator | None = None):
        self.id = trader_id
        self.collateral = collateral
        self.trade_prob = trade_prob
        self.notional_mean = notional_mean
        self.rng = rng or np.random.default_rng()

        self.size = 0.0        # signed base units: +long, -short
        self.cost_basis = 0.0  # net USD paid to reach current size
        self.liquidated = False

    def equity(self, price: float) -> float:
        if self.liquidated:
            return 0.0
        return self.collateral + self.size * price - self.cost_basis

    def maybe_trade(self, vamm: VAMM, fee_bps: float = 0.0):
        """Returns ((direction, notional), fee) if a trade happened, else None.

        fee_bps is a taker fee charged in USD straight out of collateral -- the
        answer to "where does the challenger bounty come from" (see Market.reserve):
        it's not a funding fee (the idea explicitly rules those out), it's a
        one-time execution fee, same as most fee-funded insurance funds elsewhere.
        """
        if self.liquidated or self.rng.random() > self.trade_prob:
            return None
        notional = self.rng.exponential(self.notional_mean)
        fee = notional * fee_bps
        self.collateral -= fee
        if self.rng.random() < 0.5:
            base_out = vamm.swap_quote_for_base(notional)
            self.size += base_out
            self.cost_basis += notional
            trade = ("long", notional)
        else:
            base_in = notional / vamm.price
            quote_out = vamm.swap_base_for_quote(base_in)
            self.size -= base_in
            self.cost_basis -= quote_out
            trade = ("short", notional)
        return trade, fee

    def check_liquidation(self, price: float) -> float:
        """Simplified isolated margin: wipe the account once equity hits zero.

        Returns the bad debt realized this call (0.0 if none): a violent single-tick
        re-rating can push equity past zero into deep negative territory before this
        check ever runs, leaving a shortfall no one's collateral covers.
        """
        if self.liquidated:
            return 0.0
        eq = self.equity(price)
        if eq <= 0:
            self.liquidated = True
            self.collateral = 0.0
            self.size = 0.0
            self.cost_basis = 0.0
            return -eq
        return 0.0


class Challenger:
    """Watches for vAMM/true-price divergence and pauses the market to force a re-rating."""

    def __init__(self, threshold: float = 0.05, base_bond_pct: float = 0.05,
                 max_bond_pct: float = 0.10, rng: np.random.Generator | None = None):
        self.threshold = threshold
        self.base_bond_pct = base_bond_pct
        self.max_bond_pct = max_bond_pct
        self.rng = rng or np.random.default_rng()

    @staticmethod
    def divergence(vamm_price: float, true_price: float) -> float:
        return abs(vamm_price - true_price) / true_price

    def bond_pct(self, divergence: float) -> float:
        """Dynamic bond: scales up with how far the price has drifted, capped at max_bond_pct."""
        extra = min(divergence, 0.20) / 0.20 * (self.max_bond_pct - self.base_bond_pct)
        return self.base_bond_pct + extra

    def should_challenge(self, vamm_price: float, true_price: float) -> bool:
        return self.divergence(vamm_price, true_price) > self.threshold


class Arbitrageur:
    """Fills the Dutch auction the moment it crosses their (noisy) estimate of true value.

    The protocol itself is oracle-less; this agent stands in for the outside world's
    knowledge of "real" value, imperfectly observed with noise_std latency/error.
    """

    def __init__(self, noise_std: float = 0.005, rng: np.random.Generator | None = None):
        self.noise_std = noise_std
        self.rng = rng or np.random.default_rng()

    def estimate_true_price(self, true_price: float) -> float:
        return true_price * (1 + self.rng.normal(0, self.noise_std))

    @staticmethod
    def wants_to_fill(auction_price: float, direction: str, true_price_estimate: float) -> bool:
        if direction == "up":    # auction started high, decaying down -> arb buys once cheap enough
            return auction_price <= true_price_estimate
        else:                    # direction == "down" -> auction started low, decaying up -> arb sells once rich enough
            return auction_price >= true_price_estimate
