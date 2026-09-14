# Implementation Plan

Companion to `new_idea.md`, which is the mechanism spec (what the system does
and why). This doc is the "how to actually build it" side — stack, contract
architecture, the concrete numbers this repo's simulations already validated,
build order, and the engineering decisions that are easy to get wrong on a
public chain even when the spec is right.

## 1. Tech stack

- **Solidity + Foundry.** Foundry is the current standard for this kind of
  contract — fast iteration, and its built-in fuzzing/invariant testing is
  directly useful here (§7 maps specific invariants from `new_idea.md` onto
  fuzz tests, not just unit tests).
- **Deploy on an existing L2** (Arbitrum, Base, or Optimism), not a custom
  chain. Inherits Ethereum's security, no sequencer/infra to run yourselves. A
  dedicated app-chain is a real option later if protocol-level validator logic
  outgrows what a smart contract can enforce, but it's not the starting point.
- **Don't build the dispute/vote layer from scratch without first evaluating
  UMA's Optimistic Oracle v3.** It's deployed, audited, and has secured real
  money in production for years, and its "escalation manager" concept is
  designed for exactly this situation — swapping in a private/vetted validator
  set instead of UMA's own token holders. Worth spending real time on this
  evaluation before committing to a from-scratch validator-voting contract;
  even if you don't adopt it directly, its dispute-window and bonding
  mechanics are the closest existing reference for `new_idea.md` §3–§6.

## 2. Contract architecture

Rough module breakdown, mapped to `new_idea.md` section numbers:

- **`Vamm.sol`** — the constant-product curve (§1, §2). Pure math + reserve
  state; no knowledge of challenges or validators.
- **`Vault.sol`** — real collateral accounting: deposits, withdrawals, position
  cost-basis, equity/liquidation checks (§1's "the vault only holds real money
  once traders deposit collateral").
- **`Market.sol`** — owns one `Vamm` + `Vault` pair, exposes trade entrypoints,
  and holds the challenge state machine (§3, §4, §6): `canChallenge`,
  `fileChallenge`, `matchDispute`, `resolveWithVote`, `resolveUndisputed`.
- **`DisputePool.sol`** — crowdfunded matching-bond logic (§4): deposits,
  fill-tracking, proportional payouts on a win, full forfeiture on a loss.
- **`ValidatorRegistry.sol` + `ValidatorVoting.sol`** — stake accounting and the
  blind/simultaneous vote itself (§5). See §6 below — "blind" needs a
  commit-reveal scheme on a public chain, it isn't free.
- **`MarketFactory.sol`** — creates new `Market` instances (§1's genesis flow,
  reusing the challenge machinery with no prior price), and enforces the
  platform-wide concurrent-challenge cap `K` (§8) by tracking how many `Market`
  instances currently have an open challenge.
- **`ValidatorToken.sol`** — the token itself (§7): needs a transfer-restriction
  or allowlist mode for the private/vetted phase, with an explicit, deliberate
  path to lifting that restriction later.

## 3. Validated economic parameters (carry these forward — the Python sims won't travel with you)

This repo's simulations (`validators.py`, `dispute.py`, `multi_market.py`,
`forced_position.py`, and the two notebooks) already tested several parts of
this design numerically. Since only `new_idea.md` and this file are moving to
the new repo, the concrete findings are captured here so they aren't lost:

- **The >50% validator stake threshold is sharp, not gradual.** Simulating a
  blind stake-weighted median vote with a colluding minority showed control
  flips almost exactly at 50% of stake — below it, an honest majority fully
  absorbs a coordinated lie in the median; above it, the median can be dragged
  to essentially any value the corrupt slice wants. This is the number to
  design the whole validator-economics model around, not a rough guideline.
- **Watch for stake lumpiness with a small validator set.** With few
  validators and unequal stake, *targeting* just under 50% control (buying out
  the largest holders first) can *achieve* just over 50%, because you can't buy
  a fraction of a validator. Keep stake reasonably distributed even during the
  private/vetted phase, or size the validator set larger, to avoid handing an
  attacker more control than they paid for.
- **The platform-wide concurrent-challenge cap formula, confirmed numerically:**
  `K * max_single_market_exposure < cost_to_corrupt_validator_majority`. Worked
  example that was actually simulated: $1M cost to corrupt, $150K max
  exposure per market → breakeven at K ≈ 6.67, so K=6 is the largest safe cap
  and K=7+ is a profitable attack. Recompute this ratio for your real numbers
  before picking K — don't reuse "6" verbatim, reuse the formula.
- **Dispute pool payout must return principal plus a share of the reward, not
  just a share of the reward.** An early implementation bug here paid
  depositors only a share of the opposing side's forfeited bond, replacing
  their own stake instead of adding to it — which silently turns "you should
  profit for disputing an obviously-fake challenge" into "you merely get your
  money back," undermining the "free money attracts capital fast" argument the
  whole dispute-pool mechanism depends on. Payout on a win should be:
  `deposit + (deposit / pool_total) * reward_pool`.
- **Bond scaling:** dynamic bond as a function of divergence
  (`base_bond_pct` up to `max_bond_pct`, scaling with how far price has
  drifted) behaved as intended in simulation — bigger drift required a bigger
  bond to challenge, which is the right shape to deter spam on noise while
  still rewarding whoever catches a real dislocation.

## 4. Build phases

Suggested order — each phase should be independently testable before moving on:

1. **Vamm + Vault only.** A single market, no challenge mechanism at all. Get
   basic trading, margin, and liquidation math correct and fuzz-tested first —
   everything else builds on this being right.
2. **Challenge/dispute state machine with a trusted stand-in for validators**
   (e.g. a multisig voting directly) instead of real on-chain voting. Proves out
   §3–§6's state machine (open → disputed → resolved, no-concurrent-challenge
   enforcement, always-resolve-to-median logic) without also debugging voting
   mechanics at the same time.
3. **Real validator voting** — commit-reveal, stake-weighted median (§5), or the
   UMA OOv3 integration evaluated in §1. Swap this in under the same `Market`
   interface from phase 2.
4. **Dispute pools** (§4) — crowdfunded bonds, proportional payout logic.
5. **`MarketFactory` + platform-wide concurrent-challenge cap** (§8) — this is
   the piece that's easy to defer and easy to forget; it's load-bearing (§3 of
   this doc), not a nice-to-have.
6. **Market genesis flow** (§1) — proposing a new market's starting price via
   the same optimistic-challenge path.
7. **Security audit(s), testnet, bug bounty** — before any real collateral.
8. **Mainnet/L2 launch** — gated on legal/compliance decisions (geoblocking,
   entity structure) made separately from the engineering track.

## 5. Key engineering decisions to resolve early

- **"Blind, simultaneous" requires commit-reveal on a public chain.** A vote
  submitted directly as a normal transaction is visible in the mempool the
  moment it's broadcast — there's no such thing as a private transaction by
  default. Validators must commit a hash of `(vote, salt)` during the voting
  window and reveal only after it closes, or "blind" is fiction and the
  Schelling-point argument in `new_idea.md` §5 doesn't actually hold.
- **Flash-loan price manipulation on the challenge-eligibility check.** §3's
  "at least X% away from current price" and the TVL-based bond size both read
  live on-chain state — exactly the pattern flash-loan attacks target (borrow,
  manipulate price in one atomic transaction, trigger the read, repay,
  profit). Use a TWAP or a minimum-elapsed-blocks check for the eligibility
  read, not raw spot price. This is the concrete fix for the "manufactured
  eligibility divergence" issue already flagged as a residual concern in
  `new_idea.md` §9.
- **Fixed-point math.** Solidity has no native floats or sqrt. Percentage math
  (X%, Y%, A%, bond scaling) and the vAMM's `rerate` (needs a square root, per
  `vamm.py`'s implementation) both need a fixed-point library (PRBMath is the
  common choice) — decide this before writing the math, not after finding a
  precision bug.
- **Reentrancy.** Every function that moves collateral (deposit, withdraw, bond
  posting/return, dispute pool payout) needs checks-effects-interactions plus a
  reentrancy guard. Standard, but easy to miss on the less-obvious paths
  (dispute pool payouts especially, since they're a loop over depositors).
- **Upgradability.** Decide explicitly whether contracts are immutable (safer
  for user trust, can't patch bugs) or upgradeable via a proxy (can patch,
  introduces admin-key trust assumptions and its own audit surface). Common
  pattern: upgradeable behind a timelocked multisig early, with an explicit,
  public plan to renounce upgradability once the system is proven — don't
  leave this undecided by default.
- **Emergency pause vs. the challenge mechanism.** These are different things —
  a challenge is a game-theoretic correction, an emergency pause is "we found a
  bug, stop everything." Build a separate, narrowly-scoped, governance-gated
  pause switch; don't overload the challenge state machine to do both jobs.
- **Off-chain validator portal.** Validators need a real way to look at actual
  market data (e.g. NASDAQ's real GOOG price) and submit commit/reveal votes —
  this is a piece of off-chain software, not a smart contract, and needs to
  exist before you can test phase 3 above with real people.
- **Indexing.** A subgraph or custom indexer for market state, challenge
  history, and TVL over time — needed for any real frontend, and useful during
  testing to sanity-check contract state against expectations.

## 6. Testing strategy

- **Unit tests** for each contract in isolation (standard Foundry `test/`).
- **Invariant/fuzz tests**, specifically for the properties the simulations
  already showed matter:
  - No sequence of trades/challenges can put two challenges open on the same
    market at once.
  - The concurrent-challenge cap `K` can never be exceeded platform-wide,
    under any sequence of calls.
  - Dispute pool payouts always sum to exactly `pool_total + reward_pool` on a
    win, and exactly `0` on a loss — no rounding leakage, no double payout.
  - Bond and reward accounting always balances — nothing paid out that wasn't
    first taken in.
  - A disputed challenge always resolves to the validator median, never to the
    raw claim, regardless of how far off the claim was.
- **Port the Python simulations' scenarios as acceptance tests**, even if only
  informally at first: the corruption-threshold sweep and the aggregate-attack
  breakeven-K calculation from `validator_dispute_sim.ipynb` are exactly the
  kind of economic property a Foundry fuzz test or a scenario script should
  reproduce against the real contracts, to confirm the implementation actually
  has the properties the design was validated to have.

## 7. Reference implementations worth reading (not necessarily forking)

- **Perpetual Protocol** — open-source vAMM-based perpetual futures, closest
  existing analog to `Vamm.sol` + `Vault.sol`.
- **GMX** — a different (pooled-liquidity) approach to synthetic markets,
  useful as a contrast.
- **UMA Protocol (Optimistic Oracle v3)** — the closest existing analog to
  `new_idea.md` §3–§6's propose/dispute/vote flow; see §1 above.

Search for each by name rather than relying on a pasted link here — verify
you're looking at the current, canonical repo before reading or forking
anything.

## 8. Pre-launch checklist

- [ ] Security audit(s) completed (see build phase 7) — non-negotiable before
      real collateral touches these contracts.
- [ ] Legal/compliance decisions made *before* mainnet, not after: entity
      structure, frontend geoblocking, validator token distribution structure.
      (See the earlier legal-landscape discussion for this project — securities,
      derivatives, and token-as-security exposure all apply here and need real
      counsel, not this checklist, to resolve.)
- [ ] Testnet deployment with real (non-adversarial) users, long enough to
      observe at least one full challenge/dispute/resolution cycle organically.
- [ ] Bug bounty live before mainnet TVL grows past whatever the concurrent-
      challenge cap's cost-to-corrupt assumptions were calibrated against.
