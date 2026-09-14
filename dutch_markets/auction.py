"""Dutch auction used to discover a re-rating price without an oracle."""


class DutchAuction:
    """Linear price decay from a wide starting bound back toward (and past) the frozen price.

    direction="up"   -- vAMM was too low: start high, decay down. Arb buys when cheap.
    direction="down" -- vAMM was too high: start low, decay up. Arb sells when rich.

    price_range_pct bounds how far the auction searches. If the true price has moved
    further than that from the frozen price, the auction can expire with no fill --
    a deliberate edge case worth observing (e.g. after a large/fast true-price move).
    """

    def __init__(self, frozen_price: float, direction: str,
                 price_range_pct: float = 0.30, duration_ticks: int = 20):
        self.frozen_price = frozen_price
        self.direction = direction
        self.duration_ticks = duration_ticks
        self.t = 0

        if direction == "up":
            self.start_price = frozen_price * (1 + price_range_pct)
            self.end_price = frozen_price * (1 - price_range_pct * 0.2)
        else:
            self.start_price = frozen_price * (1 - price_range_pct)
            self.end_price = frozen_price * (1 + price_range_pct * 0.2)

    def price_now(self) -> float:
        frac = min(self.t / self.duration_ticks, 1.0)
        return self.start_price + (self.end_price - self.start_price) * frac

    def step(self) -> bool:
        """Advance one tick. Returns True once the auction has expired."""
        self.t += 1
        return self.t >= self.duration_ticks
