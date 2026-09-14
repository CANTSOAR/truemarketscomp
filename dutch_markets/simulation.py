"""Ties the true-price process, the vAMM, and the agents together tick by tick."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .agents import Arbitrageur, Challenger, CoinflipTrader
from .market import Market, Phase
from .vamm import VAMM


class Simulation:
    def __init__(
        self,
        n_traders: int = 30,
        ticks: int = 3000,
        initial_price: float = 100.0,
        initial_depth: float = 5_000.0,   # virtual base reserve at t=0 -- deeper = less price impact per trade
        true_price_mu: float = 0.0,       # per-tick drift of the "real world" reference, kept ~0 by default
        true_price_sigma: float = 0.004,  # per-tick volatility of the "real world" reference
        trader_collateral: float = 10_000.0,
        trade_prob: float = 0.35,
        notional_mean: float = 600.0,
        fee_bps: float = 0.0004,          # taker fee on notional, funds the reserve (bounty + bad-debt insurance)
        challenge_threshold: float = 0.05,
        base_bond_pct: float = 0.05,
        max_bond_pct: float = 0.10,
        auction_range_pct: float = 0.30,
        auction_duration: int = 12,
        bounty_pct: float = 0.10,
        max_auction_retries: int = 3,
        retry_widen_factor: float = 1.75,
        arb_noise_std: float = 0.005,
        seed: int = 0,
    ):
        self.rng = np.random.default_rng(seed)

        self.vamm = VAMM(x=initial_depth, y=initial_depth * initial_price)
        self.market = Market(
            self.vamm, challenge_threshold, auction_range_pct, auction_duration,
            bounty_pct, max_auction_retries, retry_widen_factor,
        )

        self.traders = [
            CoinflipTrader(i, trader_collateral, trade_prob, notional_mean, self.rng)
            for i in range(n_traders)
        ]
        self.challenger = Challenger(challenge_threshold, base_bond_pct, max_bond_pct, self.rng)
        self.arbitrageur = Arbitrageur(arb_noise_std, self.rng)

        self.true_price = initial_price
        self.mu = true_price_mu
        self.sigma = true_price_sigma
        self.ticks = ticks
        self.fee_bps = fee_bps

        self.total_fees = 0.0
        self.total_bad_debt = 0.0
        self.bad_debt_events: list[dict] = []

        self.history: list[dict] = []

    def tvl(self) -> float:
        return sum(t.collateral for t in self.traders if not t.liquidated)

    def _step_true_price(self) -> None:
        shock = self.rng.normal()
        self.true_price *= np.exp((self.mu - 0.5 * self.sigma ** 2) + self.sigma * shock)

    def _record(self, tick: int) -> None:
        price = self.vamm.price
        self.history.append({
            "tick": tick,
            "true_price": self.true_price,
            "vamm_price": price,
            "phase": self.market.phase.name,
            "divergence": Challenger.divergence(price, self.true_price),
            "n_liquidated": sum(t.liquidated for t in self.traders),
            "total_equity": sum(t.equity(price) for t in self.traders),
            "tvl": self.tvl(),
            "reserve": self.market.reserve,
            "unfunded_bounty": self.market.unfunded_bounty,
            "unfunded_bad_debt": self.market.unfunded_bad_debt,
        })

    def run(self) -> pd.DataFrame:
        for tick in range(self.ticks):
            self._step_true_price()

            if self.market.phase is Phase.FLOATING:
                for trader in self.traders:
                    result = trader.maybe_trade(self.vamm, self.fee_bps)
                    if result is not None:
                        _, fee = result
                        self.market.collect_fee(fee)
                        self.total_fees += fee

                    shortfall = trader.check_liquidation(self.vamm.price)
                    if shortfall > 0:
                        self.market.absorb_bad_debt(shortfall)
                        self.total_bad_debt += shortfall
                        self.bad_debt_events.append({
                            "tick": tick, "trader_id": trader.id, "shortfall": shortfall,
                        })

                if self.challenger.should_challenge(self.vamm.price, self.true_price):
                    self.market.trigger_challenge(self.challenger, self.true_price, self.tvl(), tick)
            else:  # Phase.AUCTION -- trading frozen, only the auction clock moves
                self.market.step_auction(self.arbitrageur, self.true_price, tick)

            self._record(tick)

        return pd.DataFrame(self.history)

    def events_df(self) -> pd.DataFrame:
        return pd.DataFrame(self.market.events)

    def bad_debt_df(self) -> pd.DataFrame:
        return pd.DataFrame(self.bad_debt_events)
