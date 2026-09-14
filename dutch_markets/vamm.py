"""Virtual AMM: constant-product (x*y=k) engine with no real underlying asset."""
import math
from dataclasses import dataclass


@dataclass
class VAMM:
    x: float  # virtual base-asset reserve
    y: float  # virtual quote (USD) reserve

    @property
    def k(self) -> float:
        return self.x * self.y

    @property
    def price(self) -> float:
        """Quote per unit of base."""
        return self.y / self.x

    def swap_quote_for_base(self, quote_in: float) -> float:
        """Spend quote_in USD to buy base (open/add to a long). Returns base received."""
        if quote_in <= 0:
            return 0.0
        new_y = self.y + quote_in
        new_x = self.k / new_y
        base_out = self.x - new_x
        self.x, self.y = new_x, new_y
        return base_out

    def swap_base_for_quote(self, base_in: float) -> float:
        """Sell base_in units of base (open/add to a short). Returns quote received."""
        if base_in <= 0:
            return 0.0
        new_x = self.x + base_in
        new_y = self.k / new_x
        quote_out = self.y - new_y
        self.x, self.y = new_x, new_y
        return quote_out

    def rerate(self, new_price: float) -> None:
        """Re-center the curve on new_price after an auction fill, preserving depth (k)."""
        k = self.k
        self.x = math.sqrt(k / new_price)
        self.y = math.sqrt(k * new_price)
