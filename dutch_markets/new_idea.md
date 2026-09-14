# Architecture: Pegged Markets + Validator-Resolved Disputes

Two sides to the system:

- **Markets** — pegged to different real-world assets, trade freely on their own
  curve until challenged.
- **Validator tokens** — a separate token, held by a private/vetted set for now,
  used only to resolve disputes when a market's price is challenged.

## 1. Market creation

Before a market can float or be challenged, it has to start somewhere: what
price, and who's on the other side of the very first trade?

- **Execution model: a virtual AMM, not an order book.** An order book needs a
  resting seller before the first buy can happen — a brand-new market has none.
  A virtual constant-product curve (`x*y=k`, the same mechanism from `idea.md`)
  sidesteps this: the counterparty to every trade is the curve itself, so a
  market can always be traded against, no matter how thin real interest is.
- **The platform doesn't need to seed real capital to bootstrap this.** The
  curve's initial reserves (`x`, `y`) are virtual units, chosen only to set a
  starting price and a depth/slippage profile — not real assets that need
  funding. The vault only holds real money once traders deposit their own
  collateral to open positions; PnL nets between winning and losing traders out
  of that pool. The platform itself is never a funded counterparty at risk.
- **Depth has to scale with real TVL, not be a fixed constant.** Too deep
  relative to a market's actual deposited collateral, and price stops
  responding to real trades — which quietly breaks the arbitrage-convergence
  properties this design leans on elsewhere (§3, and the disclosure-timing
  arbitrage in §5). Too shallow, and a small amount of capital can swing price
  enough to manufacture a challenge (§9) or just look manipulated. Depth should
  be a function of a market's current TVL (e.g. reserves proportional to
  deposited collateral), so a brand-new, thin market is appropriately
  responsive and a mature one is appropriately stable, rather than picking one
  fixed number that's wrong at both ends of a market's life.
- **Genesis reuses the challenge machinery instead of inventing a new one.**
  Proposing a market's starting price is just a challenge with no prior price
  to diverge from: broadcast the proposed price, and if undisputed after a
  window it's simply accepted (the same optimistic-default path as §6's
  "Undisputed" case). If disputed, it goes to the same validator vote as any
  other challenge. One mechanism, reused for both correcting an existing market
  and creating a new one.

## 2. Floating phase

Markets trade freely with no external oracle. Anyone can trade at any time,
including during a pending challenge (see §4).

## 3. Challenge

Any participant can challenge a market's price:

- They state a claimed value at least **X%** away from the market's current
  price at the moment of the challenge.
- They lock a bond of **Y%** of the market's net locked value (TVL), computed at
  challenge time.
- The challenge is broadcast publicly, including its resolution timer.
- **The market stays open.** A challenge does not freeze trading. Because the
  challenge and its timer are public, rational trading during the window pulls
  the price toward the expected outcome on its own — the eventual resolution
  (§6) usually confirms a price the market has already mostly moved to, rather
  than forcing a violent snap. A challenge is a trigger for a decision process,
  not a circuit breaker.
- **No concurrent challenges on the same market.** A market can have at most one
  open challenge at a time; a new challenge can't be filed until the current one
  fully resolves. This keeps "the price" unambiguous for the length of the
  dispute — see §5 for why this matters.
- Trading by the challenger (or anyone else) during the open window is allowed.
  Restricting it would be unenforceable in a permissionless system anyway (any
  such rule is trivially bypassed with a fresh address) and unnecessary: the
  resolution price is set by an independent validator vote, not by trade flow,
  so pre-positioning ahead of a challenge is ordinary speculation on an honest
  outcome, not a way to influence that outcome.

## 4. Dispute

Once a challenge is live, anyone can dispute it by matching the challenge bond,
sending the decision to the validators.

- If nobody matches the bond before the cutoff, the challenge goes undisputed
  (§6).
- Disputers don't need their own capital: they can open a **dispute pool** that
  anyone can deposit into for a proportional share of the outcome.
  - If the pool reaches 100% of the required bond before the cutoff **and** the
    validators vote against the challenge, depositors split the winnings
    proportionally.
  - If the pool reaches 100% **and** the validators side with the challenger,
    the pool's stake is forfeit (to the challenger) — depositors bear real loss
    risk once the pool is actually used to back a dispute.
  - If the pool never reaches 100% before the cutoff, it was never put at risk:
    deposits are simply returned.
  - This is the mechanism that keeps disputing accessible without capital,
    without requiring a whale to front the bond personally.

## 5. Validator resolution

If a challenge is disputed, validators resolve it:

- **Blind, simultaneous vote** — every validator votes on price independently,
  with no visibility into other votes before their own is locked in. This is a
  Schelling-point mechanism: it forces independent estimates instead of
  copy/bandwagon voting, and makes the median the honest consensus rather than
  whoever votes last.
- Validators have **Z hours** to vote.
- **What they vote on:** the fair value of the asset *at the moment the
  challenge was filed* — not the live price at vote time, and not a
  reconstructed/backtracked estimate. Asking "what was it worth right now" is
  unanswerable without an oracle (which is the thing we don't have), and
  "backtrack to then" reduces to the same question. Anchoring the vote to a
  specific, well-defined historical snapshot is the only version of this
  question that has a stable answer. Any drift in the real value that
  accumulates during the Z+B hour resolution window is not corrected by this
  vote — it's left to organic trading once the market reopens, or to a fresh
  challenge. Keeping Z and B short bounds how much staleness this can
  introduce.
- **Quorum:** with a private/vetted validator set, non-participation/DDoS-into-
  silence isn't a live concern today — vetted validators are expected to show
  up. This needs to be revisited (an explicit quorum-failure fallback defined)
  once validator tokens are freely distributed and participation can no longer
  be assumed.
- **Result disclosure: the moment the vote closes, not at resolution.** `V_med`
  is public as soon as voting ends — the B-hour gap in §6 is a mechanical/
  implementation buffer before the price is actually snapped, not a secrecy
  period. This matters: once `V_med` is public and the exact time it takes
  effect is known, holding a known, dated future price is a near-riskless
  arbitrage (buy now if current price is below `V_med`, short if above), and
  competition to capture it should push the live price to `V_med` well before
  the B hours are up — organically finishing the correction the vote just
  authorized. This is a different situation from the auction-style racing
  problem this design was chosen over: there, competitors were racing on a
  *private, noisy belief* about an unknown value (the winner's curse); here
  the value is public and certain, so competition is simply efficient price
  discovery, not adverse selection. It also directly shrinks the residual-
  staleness problem noted above: most of the correction should already be
  organically priced in by the time the mechanical snap happens. (Two bounds on
  how clean this is: a thin market can't close the gap instantly, only as fast
  as available capital allows; and this only holds if a closed vote is final —
  see below.)
- **Finality:** a closed, resolved vote cannot be re-disputed or appealed. This
  isn't just tidiness -- it's what makes the arbitrage above close to riskless
  rather than a probability-weighted bet; any chance of reversal would make
  arbitrageurs size into it more cautiously and slow down the very convergence
  this is supposed to produce.

## 6. Resolution outcomes

Given the median validator vote `V_med` and the challenger's claim `V_claim`:

- **Median within A% of the claim:** the market resolves to `V_med` (not to
  `V_claim` — always use the best available information), in B hours. The
  challenger gets their bond back plus a reward from the disputer's forfeited
  bond.
- **Median not within A% of the claim:** the market *still* resolves to
  `V_med` — a validator-vetted median shouldn't be thrown away just because the
  original challenger overshot or undershot. Only the bond outcome depends on
  accuracy: the challenger's bond is forfeited to the disputers. This decouples
  "what's the right price" (always answered by the vote, when there is one)
  from "who was right" (the accuracy check, which only decides who gets paid).
- **Undisputed:** if nobody matches the bond before the cutoff, the market
  resolves to the challenger's claimed price after **C hours** — the fully
  optimistic path, no vote required.

## 7. Validator token distribution

Validator tokens are held privately for now — either kept off-market or
distributed to vetted validators under contract — before any future move to
free/open distribution. This keeps the validator set small and accountable
while the system is unproven, and avoids the aggregate-security-budget problem
(§8) being immediately live in its open-market form.

## 8. Security invariants

Two separate invariants, not one:

- **Per-challenge:** the profit obtainable from any single fraudulent
  resolution must be less than what a validator (or validator majority) stands
  to lose by being caught — token value crashing, slashed collateral, or (in
  the current private-validator phase) contractual/reputational penalties.
  This bounds a single bad actor attacking a single market.
- **Aggregate, platform-wide:** this is not sufficient on its own. A corrupted
  validator majority isn't limited to one market — nothing stops them from
  voting fraudulently on many *different* markets within the same window,
  before the compromise is even visible, collecting the profit from all of
  them for one cost of corruption. Concretely: if corrupting the validator set
  costs $1M, and each market is individually capped so no single fraudulent
  resolution nets more than $150K, a corrupted majority can still challenge 10
  markets at once and net $1.5M for that same $1M — passing every per-market
  check while still being profitable in aggregate.

  The fix is an explicit, enforced cap, not just a stated principle: **at most
  K markets platform-wide may have an open challenge at the same time**, with K
  chosen so that `K * max_single_market_exposure < cost_to_corrupt_validator_majority`.
  During the private-validator phase, "cost to corrupt" should be read as
  whatever collateral/contractual penalty backs the vetted validator set, not
  an open-market token price — that changes (and this cap needs recalibrating)
  once tokens are freely distributed.

## 9. Residual / minor considerations

- **Manufactured eligibility divergence:** a challenger could, in principle,
  trade the market away from fair value immediately before filing, in order to
  manufacture the X% divergence needed to be eligible to challenge at all. This
  doesn't help them *win* — validators vote on genuine fair value, not on
  distance from a manipulated in-market price — so the existing bond-forfeiture
  economics should already discourage it. Worth confirming this holds once real
  parameters are chosen, but it's a minor consideration, not a structural gap.
- Revisit §5's quorum assumption and §8's aggregate cap calibration together
  once validator tokens move to open distribution — both currently lean on the
  validator set being small, known, and vetted.
