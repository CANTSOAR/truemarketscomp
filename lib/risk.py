import logging


class RiskManager:
    def __init__(self, max_inventory: int, max_loss: float):
        self.max_open_usd = float(max_inventory) * 100  # e.g. 5 → $500 max open
        self.max_loss = max_loss
        self.kill_switch_engaged = False

        self._open_usd = 0.0      # current unrealised exposure
        self._total_spent = 0.0   # cumulative buy spend
        self._total_received = 0.0  # cumulative sell revenue
        self.realized_pnl = 0.0

    def check_trade_allowed(self, side: str, qty_usd: float) -> bool:
        if self.kill_switch_engaged:
            logging.error("Kill switch engaged — all trading halted.")
            return False

        if self.realized_pnl <= -self.max_loss:
            logging.error(f"Kill switch: realized loss ${-self.realized_pnl:.2f} exceeds limit ${self.max_loss:.2f}.")
            self.kill_switch_engaged = True
            return False

        if side == "buy" and self._open_usd + qty_usd > self.max_open_usd:
            logging.warning(
                f"Risk block: buying ${qty_usd} would push open position to "
                f"${self._open_usd + qty_usd:.2f} (limit ${self.max_open_usd:.2f})."
            )
            return False

        return True

    def update_state(self, side: str, qty_usd: float, price: float):
        if side == "buy":
            self._open_usd += qty_usd
            self._total_spent += qty_usd
        else:
            self._open_usd = max(0.0, self._open_usd - qty_usd)
            self._total_received += qty_usd
            self.realized_pnl = self._total_received - self._total_spent

        logging.info(
            f"Risk state: open=${self._open_usd:.2f} | "
            f"spent=${self._total_spent:.2f} | received=${self._total_received:.2f} | "
            f"PnL=${self.realized_pnl:.4f}"
        )
