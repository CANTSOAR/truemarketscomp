"""Blind, simultaneous, stake-weighted median voting -- and an explicit model of
what a colluding, stake-weighted minority/majority can and can't do to the result.

This exists to test the doc's central security claim directly: that controlling
a validator vote requires >50% of stake, and that below that threshold an honest
majority protects the median even against a coordinated lie.
"""
from __future__ import annotations

import numpy as np


class ValidatorPool:
    def __init__(self, n_validators: int, total_stake: float,
                 noise_std: float = 0.02, rng: np.random.Generator | None = None):
        self.n_validators = n_validators
        self.total_stake = total_stake
        self.noise_std = noise_std
        self.rng = rng or np.random.default_rng()
        # unequal stake split across validators -- more realistic than assuming
        # every validator holds the same weight
        weights = self.rng.dirichlet(np.ones(n_validators))
        self.stakes = weights * total_stake

    def _honest_votes(self, true_value: float) -> np.ndarray:
        """Each validator's independent, blind, noisy belief about the challenge-
        time fair value -- this is the Schelling-point estimate the vote relies on."""
        return true_value * (1 + self.rng.normal(0, self.noise_std, self.n_validators))

    @staticmethod
    def _weighted_median(values: np.ndarray, weights: np.ndarray) -> float:
        order = np.argsort(values)
        sorted_values = values[order]
        cum_weight = np.cumsum(weights[order])
        cutoff = cum_weight[-1] / 2.0
        idx = np.searchsorted(cum_weight, cutoff)
        return float(sorted_values[min(idx, len(sorted_values) - 1)])

    def _corrupt_indices(self, target_frac: float) -> tuple[np.ndarray, float]:
        """Cheapest way to assemble >= target_frac of stake: buy out the largest
        holders first. With a small number of validators and lumpy (unequal)
        stakes, this routinely *overshoots* the target -- e.g. targeting 49% can
        land at 56% because the next-cheapest validator's stake doesn't divide
        evenly. That overshoot is real and worth keeping visible (a concentrated
        stake distribution can hand an attacker more control than they paid for),
        so this returns the actual achieved fraction alongside the indices.
        """
        order = np.argsort(-self.stakes)
        cum = np.cumsum(self.stakes[order])
        target = target_frac * self.total_stake
        n_corrupt = int(np.searchsorted(cum, target)) + 1
        n_corrupt = min(n_corrupt, self.n_validators)
        corrupt_idx = order[:n_corrupt]
        actual_frac = cum[n_corrupt - 1] / self.total_stake
        return corrupt_idx, actual_frac

    def vote(self, true_value: float, corrupt_stake_frac: float = 0.0,
              fraudulent_value: float | None = None) -> float:
        """Runs one blind simultaneous vote and returns the stake-weighted median.

        `corrupt_stake_frac` of total stake (assembled from the largest
        stakeholders first, i.e. the cheapest way to reach that much weight)
        votes `fraudulent_value` in lockstep instead of an honest, independent
        belief -- collusion happens outside the protocol, so "blind and
        simultaneous" can't stop coordinated voting, only stop *copying* an
        already-revealed vote. Everyone else votes their own honest estimate.
        """
        median, _ = self.vote_with_diagnostics(true_value, corrupt_stake_frac, fraudulent_value)
        return median

    def vote_with_diagnostics(self, true_value: float, corrupt_stake_frac: float = 0.0,
                                fraudulent_value: float | None = None) -> tuple[float, float]:
        """Same as vote(), but also returns the *actual* corrupt stake fraction
        achieved (see _corrupt_indices) -- 0.0 if no corruption was requested."""
        votes = self._honest_votes(true_value)
        actual_frac = 0.0
        if corrupt_stake_frac > 0 and fraudulent_value is not None:
            corrupt_idx, actual_frac = self._corrupt_indices(corrupt_stake_frac)
            votes = votes.copy()
            votes[corrupt_idx] = fraudulent_value
        return self._weighted_median(votes, self.stakes), actual_frac
