"""Critique harness for the 'Forced-Position Dutch Auction' proposal.

The proposal's claim: racing multiple arbitrageurs to be first to fill a decaying
auction, each acting the instant it's profitable given their own private belief,
converges the fill price to true value -- because anyone who fills too early
"loses" and anyone who waits too long gets sniped. This module tests that claim
directly instead of taking it on faith.

The suspicion: in a *first-past-the-post* race, the bot that fills first is
mechanically the one whose private belief is most extreme in the direction that
makes filling look attractive soonest -- not the bot with the most accurate
belief. That's the classic auction-theory winner's curse, and it gets *worse*
with more competing bots, not better.
"""
from __future__ import annotations

import numpy as np

from .agents import Arbitrageur
from .auction import DutchAuction


def race_to_fill(true_price, frozen_price, direction, auction_range_pct,
                  auction_duration, n_bots, noise_std, rng, shade=0.0):
    """One auction, n_bots competing bots, each with a private belief fixed for
    the whole auction (drawn once, not re-rolled each tick). `shade` is an optional
    margin bots require beyond their raw belief before they'll fill (the rational
    response to a winner's curse, if they know to apply it).

    Returns (fill_price, winning_bot_estimate) or (None, None) if nobody ever
    crosses within auction_duration ticks.
    """
    auc = DutchAuction(frozen_price, direction, auction_range_pct, auction_duration)
    bots = [Arbitrageur(noise_std, rng) for _ in range(n_bots)]
    estimates = [b.estimate_true_price(true_price) for b in bots]
    if direction == "up":
        triggers = [e * (1 - shade) for e in estimates]
    else:
        triggers = [e * (1 + shade) for e in estimates]

    for _ in range(auction_duration + 1):
        price_now = auc.price_now()
        ready = [t for t in triggers if Arbitrageur.wants_to_fill(price_now, direction, t)]
        if ready:
            # First bot triggered this tick is whichever has the most extreme belief
            # in the direction that clears the fewest ticks of decay -- for "up" (price
            # falling) that's the highest bidder; for "down" (price rising) the lowest.
            winner_trigger = max(ready) if direction == "up" else min(ready)
            return price_now, winner_trigger
        if auc.step():
            break
    return None, None


def run_experiment(true_price=40.0, frozen_price=30.0, direction="up",
                    auction_range_pct=0.5, auction_duration=40,
                    n_bots_list=(1, 2, 5, 10, 25, 50), noise_std=0.03,
                    shade=0.0, n_trials=2000, seed=0):
    """Sweeps number of competing bots and reports the fill-price bias relative
    to true_price -- the key diagnostic for whether competition helps or hurts."""
    rng = np.random.default_rng(seed)
    rows = []
    for n_bots in n_bots_list:
        fills = []
        for _ in range(n_trials):
            fill_price, _ = race_to_fill(
                true_price, frozen_price, direction, auction_range_pct,
                auction_duration, n_bots, noise_std, rng, shade,
            )
            if fill_price is not None:
                fills.append(fill_price)
        fills = np.array(fills)
        rows.append({
            "n_bots": n_bots,
            "n_filled": len(fills),
            "n_trials": n_trials,
            "mean_fill_price": fills.mean() if len(fills) else np.nan,
            "bias_pct": (fills.mean() / true_price - 1) * 100 if len(fills) else np.nan,
            "std_fill_price": fills.std() if len(fills) else np.nan,
        })
    return rows
