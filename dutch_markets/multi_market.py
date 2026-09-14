"""Multi-market platform: enforces the platform-wide concurrent-challenge cap
(new_idea.md section 7's aggregate invariant) and hosts the aggregate-attack
economics test -- does a corrupted validator majority attacking K markets at
once actually stay unprofitable under the cap?
"""
from __future__ import annotations

import numpy as np

from .dispute import PegMarket
from .validators import ValidatorPool


class Platform:
    def __init__(self, n_markets: int, tvl_per_market: float,
                 max_concurrent_challenges: int, price: float = 100.0, **market_kwargs):
        self.markets = [
            PegMarket(i, tvl_per_market, price, **market_kwargs) for i in range(n_markets)
        ]
        self.max_concurrent_challenges = max_concurrent_challenges

    def n_open_challenges(self) -> int:
        return sum(1 for m in self.markets if m.challenge is not None)

    def can_open_new_challenge(self) -> bool:
        return self.n_open_challenges() < self.max_concurrent_challenges


def corruption_sweep(true_value: float, corrupt_stake_fracs, fraud_multiplier: float = 1.30,
                      n_validators: int = 101, total_stake: float = 2_000_000.0,
                      noise_std: float = 0.02, n_trials: int = 500, seed: int = 0):
    """For each requested corrupt-stake fraction, runs many blind votes where the
    corrupt slice colludes on a fixed fraudulent value, and reports how much the
    resulting median actually got dragged toward that lie. Tests whether the >50%
    threshold the doc assumes is actually where control flips. Uses a larger
    validator count than the platform's likely real size purely so the requested
    and achieved corrupt-stake fractions stay close (see ValidatorPool._corrupt_indices
    for why a small validator set can overshoot a target).
    """
    rng = np.random.default_rng(seed)
    fraudulent_value = true_value * fraud_multiplier
    rows = []
    for frac in corrupt_stake_fracs:
        medians, actual_fracs = [], []
        for _ in range(n_trials):
            pool = ValidatorPool(n_validators, total_stake, noise_std, rng)
            median, actual_frac = pool.vote_with_diagnostics(true_value, frac, fraudulent_value)
            medians.append(median)
            actual_fracs.append(actual_frac)
        medians = np.array(medians)
        # 0 = median tracks true value (attack failed), 1 = median tracks the lie (attack succeeded)
        capture_frac = (medians.mean() - true_value) / (fraudulent_value - true_value)
        rows.append({
            "requested_corrupt_frac": frac,
            "actual_corrupt_frac": float(np.mean(actual_fracs)),
            "mean_median": medians.mean(),
            "capture_frac": capture_frac,
        })
    return rows


def aggregate_attack_profit(market_tvl: float, n_markets_attacked: int,
                             attack_intensity: float, cost_to_corrupt: float,
                             corrupt_stake_frac: float = 0.51,
                             n_validators: int = 21, total_stake: float | None = None,
                             noise_std: float = 0.02, n_trials: int = 500, seed: int = 0):
    """Simulates one corrupted-majority event: pay cost_to_corrupt once, then push
    a fraudulent resolution through on n_markets_attacked markets simultaneously
    (all within the platform's concurrent-challenge cap). Returns the distribution
    of net profit (extracted value minus the one-time cost of corruption).
    """
    if total_stake is None:
        total_stake = cost_to_corrupt / corrupt_stake_frac
    rng = np.random.default_rng(seed)
    true_value = market_tvl
    fraudulent_value = true_value * (1 + attack_intensity)

    nets = []
    for _ in range(n_trials):
        pool = ValidatorPool(n_validators, total_stake, noise_std, rng)
        total_profit = 0.0
        for _ in range(n_markets_attacked):
            median = pool.vote(true_value, corrupt_stake_frac, fraudulent_value)
            realized_move_pct = (median - true_value) / true_value
            total_profit += market_tvl * realized_move_pct
        nets.append(total_profit - cost_to_corrupt)
    return np.array(nets)
