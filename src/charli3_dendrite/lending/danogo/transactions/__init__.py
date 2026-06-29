"""Danogo transaction construction + Ogmios verification (read/build, never submit).

`DanogoTxBuilder` (``builder.py``) is Danogo's implementation of the cross-protocol
`AbstractLendingTxBuilder` seam. Its `supported_actions()` advertises BORROW (create
a loan), DEPOSIT and WITHDRAW (supply-side top-up / redemption), and REPAY (borrower
repayment via the on-chain `DecreaseLoanAmount` action). `resolve_snapshot` reads the
live state into a `CreateLoanSnapshot` (BORROW), a shared `TopupWithdrawSnapshot`
(DEPOSIT/WITHDRAW), or a `RepaySnapshot` for the loan named by `params.loan_utxo`
(REPAY); and `contribute` routes BORROW through `build_create_loan`, DEPOSIT/WITHDRAW
through `build_topup_withdraw`, and REPAY through `build_repay`, in each case stashing
the actor funding input for Ogmios. As with the seam, the success criterion is a
passing Ogmios evaluation -- it never signs or submits.

A repay reverses a borrow against an open loan. A **full** repay clears the accrued
debt, closes the loan (no loan output), burns the loan token + owner NFT under the
loan script, and releases all the collateral back to the borrower. A **partial** repay
reduces the loan amount and keeps a smaller loan UTxO open. A partial repay may also
**modify the collateral** (add and/or remove) in the same `DecreaseLoanAmount` tx: the
loan output locks a TARGET absolute collateral set (`build_repay`'s ``collateral`` /
the REPAY `ActionParams.collateral` -- a ``unit -> quantity`` map), with added units
funded by the borrower and removed units returned via balancing; a falsy target carries
the loan input's collateral forward unchanged. Collateral modification is partial-only
(a full repay releases all collateral), and the caller should run the health-factor
preflight (`safe_collateral_for_repay`) so the post-repay collateral value still exceeds
the reduced loan amount. Both repay kinds route through the loan-script
`Withdraw(loan_skh)` delegation hub that orchestrates the pool/loan spends (and, on full
repay, the burn).

Three paths live here:

- **Verifier** (``create_loan.py`` + ``verify.py``): rebuilds a *captured* create-loan
  tx from a fixture and re-evaluates it on Ogmios. Used to lock down byte-exact datum
  and redeemer reconstruction against known-good on-chain transactions.

- **Forward create-loan** (``build.py`` / `build_create_loan`): synthesizes a fresh
  create-loan tx from *live* chain state, contributing the pool script input, reference
  inputs, loan/owner-NFT mint, oracle withdrawal redeemer, and updated pool/loan outputs
  to a caller-supplied ``pycardano.TransactionBuilder``. Datums (``datum_synth.py``) and
  the oracle price-calc redeemer (``oracle_synth.py``) are synthesized from live state,
  not copied. ``build_and_evaluate`` is a thin driver that assembles the tx and proves
  every redeemer passes Ogmios ``evaluateTransaction``.

- **Forward deposit/withdraw** (``build.py`` / `build_topup_withdraw`): synthesizes a
  supply-side top-up (deposit) or redemption (withdraw) from live state, minting or
  burning dTokens, updating the pool output, and attaching the oracle reference. The
  direction is carried by a signed supply delta.

No path signs or submits.
"""
