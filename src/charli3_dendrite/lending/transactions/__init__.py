"""Protocol-agnostic lending transaction seam + eval-only plumbing.

`AbstractLendingTxBuilder` (``base.py``) is the cross-protocol seam every lending
protocol implements, mirroring the AMM `AbstractPoolState.swap_utxo` split: the
concrete `build_and_evaluate` orchestration is shared, and each protocol supplies
the `resolve_snapshot` and `contribute` primitives. An action is named by the
`LendingAction` enum (DEPOSIT, WITHDRAW, BORROW, and REPAY) and parameterized by
`ActionParams` (actor address/amount/collateral plus optional funding and loan
out-refs). A builder advertises the subset it can assemble via `supported_actions()`,
which `build_and_evaluate` enforces up front.

`build_and_evaluate` drives the flow end to end: `resolve_snapshot` reads the live
on-chain state into a `PoolActionSnapshot`; `contribute` wires the spend / mint-burn
/ outputs / redeemers and stashes the actor (funding) inputs Ogmios cannot resolve;
the tx is then assembled unsigned and proven by Ogmios ``evaluateTransaction``. The
seam is eval-only: a successful evaluation is the success criterion -- nothing here
signs or submits.

`PoolActionSnapshot` (``snapshot.py``) is the snapshot base each protocol subclasses,
owning the shared `additionalUtxo` stash for actor funding inputs. ``infra.py`` holds
the protocol-agnostic, eval-only machinery: the network-free chain context, manual
(no-balancing) assembly, slot/out-ref helpers, the Ogmios `additionalUtxo` builder,
and the `evaluateTransaction` client. Protocol-specific domain logic stays in each
protocol's own ``transactions`` package.
"""
