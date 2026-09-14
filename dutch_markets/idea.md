On your platform, every single market operates as a self-contained, oracle-less sandbox driven entirely by game theory and free-market incentives. There are no external price feeds, no centralized market makers, and no continuous funding fees.

The lifecycle of any market on your platform flows through a continuous loop of three distinct phases:

### 1. The Floating Phase (Unrestricted Free Trading)

* **The Mechanism:** Assets trade on a Virtual Automated Market Maker (vAMM) curve ($x \cdot y = k$). Traders deposit stablecoin collateral into a vault and go long or short by trading against the virtual curve.
* **The Cost:** It costs **zero** funding fees to hold a position. The price is determined purely by internal order flow. If the real-world value of the asset moves externally, the internal vAMM price will naturally drift away from reality unless traders manually arbitrage it.

### 2. The Challenge Phase (Predatory Protection)

* **The Trigger:** When the internal vAMM price drifts too far from the real-world value, any participant (a Challenger) can pause the market.
* **Skin in the Game:** To prevent spam, the Challenger must lock up a substantial dynamic bond (e.g., 5% to 10% of the market's Total Value Locked).
* **The Halt:** Regular trading is instantly frozen, locking all open positions at their current virtual prices to protect the vault.

### 3. The Auction Phase (Discrete Settlement)

* **Price Discovery:** The contract automatically launches a Dutch auction to find the true clearing price. If the internal price was too low, the auction starts high and decays downward second by second.
* **The Free Market Clears:** Independent arbitrageurs watch the decay. The moment the auction price drops slightly below the asset's true real-world value, a rational arbitrageur will step in, inject capital, and click "Fill" to pocket a quick profit.
* **The Re-Rating:** The contract uses this exact fill price to instantly update the vAMM. Open positions are violently re-rated in a single block: winning positions see their equity jump, losing positions are deducted (or liquidated), the Challenger gets their bond back plus a bounty, and the market unfreezes back into Phase 1 at its perfectly corrected price.