"""Single-market challenge -> dispute -> validator-resolution state machine, per
new_idea.md: markets stay open through a challenge, at most one challenge is open
per market at a time, and a disputed challenge always resolves to the validator
median (accuracy vs. the claim only decides who gets paid, not whether the price
updates).
"""
from __future__ import annotations

from enum import Enum, auto


class ChallengeStatus(Enum):
    OPEN = auto()       # filed, waiting for someone to match the bond (or the cutoff)
    DISPUTED = auto()   # bond matched, sent to validators
    RESOLVED = auto()


class DisputePool:
    """Crowdfunded matching bond: real risk only once it actually fills and gets
    used to back a dispute -- not before."""

    def __init__(self, target_amount: float):
        self.target_amount = target_amount
        self.deposits: dict[str, float] = {}

    @property
    def total(self) -> float:
        return sum(self.deposits.values())

    @property
    def is_full(self) -> bool:
        return self.total >= self.target_amount

    def deposit(self, depositor_id: str, amount: float) -> None:
        self.deposits[depositor_id] = self.deposits.get(depositor_id, 0.0) + amount

    def payouts(self, won: bool, reward_pool: float) -> dict[str, float]:
        """If the dispute won, each depositor gets their own stake back *plus* a
        proportional share of reward_pool (the opposing side's forfeited bond) --
        not just a share of reward_pool alone, which would merely refund the pool
        without the profit that's supposed to make disputing worthwhile.
        If it lost, every depositor's stake is forfeited (already staked, gone)."""
        if not won or self.total <= 0:
            return {depositor: 0.0 for depositor in self.deposits}
        return {d: amt + (amt / self.total) * reward_pool for d, amt in self.deposits.items()}


class Challenge:
    def __init__(self, claimant_id, claimed_value: float, price_at_challenge: float,
                 bond_amount: float, filed_tick: int):
        self.claimant_id = claimant_id
        self.claimed_value = claimed_value
        self.price_at_challenge = price_at_challenge
        self.bond_amount = bond_amount
        self.filed_tick = filed_tick
        self.status = ChallengeStatus.OPEN
        self.dispute_pool: DisputePool | None = None
        self.dispute_vote_deadline: int | None = None
        self.resolution: dict | None = None


class PegMarket:
    """One pegged market. `price` is driven externally each tick (organic trading);
    this class only owns the challenge/dispute/resolution lifecycle on top of it.
    """

    def __init__(self, market_id, tvl: float, price: float,
                 bond_pct: float = 0.10, challenge_threshold_pct: float = 0.05,
                 accuracy_band_pct: float = 0.05,
                 dispute_window: int = 6, vote_window: int = 24,
                 resolution_delay: int = 6, undisputed_delay: int = 48):
        self.market_id = market_id
        self.tvl = tvl
        self.price = price
        self.bond_pct = bond_pct
        self.challenge_threshold_pct = challenge_threshold_pct
        self.accuracy_band_pct = accuracy_band_pct
        self.dispute_window = dispute_window
        self.vote_window = vote_window
        self.resolution_delay = resolution_delay
        self.undisputed_delay = undisputed_delay

        self.challenge: Challenge | None = None

    def can_challenge(self, claimed_value: float) -> bool:
        if self.challenge is not None:
            return False  # no concurrent challenges on the same market
        return abs(claimed_value - self.price) / self.price >= self.challenge_threshold_pct

    def file_challenge(self, claimant_id, claimed_value: float, tick: int) -> Challenge:
        assert self.can_challenge(claimed_value), "challenge ineligible or already open"
        bond = self.bond_pct * self.tvl
        self.challenge = Challenge(claimant_id, claimed_value, self.price, bond, tick)
        return self.challenge

    def open_dispute_pool(self, tick: int) -> DisputePool:
        ch = self.challenge
        ch.dispute_pool = DisputePool(target_amount=ch.bond_amount)
        return ch.dispute_pool

    def match_dispute(self, tick: int) -> None:
        """A matching bond has been fully assembled (directly or via a filled
        dispute pool) -- escalate to the validator vote."""
        ch = self.challenge
        ch.status = ChallengeStatus.DISPUTED
        ch.dispute_vote_deadline = tick + self.vote_window

    def resolve_with_vote(self, median_vote: float, tick: int) -> dict:
        ch = self.challenge
        accurate = abs(median_vote - ch.claimed_value) / max(ch.claimed_value, 1e-9) <= self.accuracy_band_pct
        self.price = median_vote  # always resolve to the median -- see new_idea.md section 5
        ch.status = ChallengeStatus.RESOLVED
        ch.resolution = {
            "tick": tick, "outcome": "disputed", "median_vote": median_vote,
            "accurate": accurate, "challenger_wins": accurate,
        }
        result = ch.resolution
        self.challenge = None
        return result

    def resolve_undisputed(self, tick: int) -> dict:
        ch = self.challenge
        self.price = ch.claimed_value
        ch.status = ChallengeStatus.RESOLVED
        ch.resolution = {"tick": tick, "outcome": "undisputed", "resolved_price": ch.claimed_value}
        result = ch.resolution
        self.challenge = None
        return result
