"""FluidTokens V4 pool cancel: close one or several pools and their pool managers.

Each pool and its pool manager are spent and both NFTs burnt; the pool's remaining
principal and both UTxOs' ADA land in the caller's change. The pool-manager owner
signs.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pycardano import Asset
from pycardano import AssetName
from pycardano import MultiAsset
from pycardano import Redeemer
from pycardano import ScriptHash
from pycardano import TransactionBuilder

from charli3_dendrite.lending.fluidtokens.transactions.utxos import Utxo
from charli3_dendrite.lending.fluidtokens.transactions.utxos import script_ref_by_hash
from charli3_dendrite.lending.fluidtokens.transactions.utxos import to_pycardano_utxo
from charli3_dendrite.lending.fluidtokens.transactions.utxos import utxo_from_dict
from charli3_dendrite.lending.fluidtokens_v4 import constants as c
from charli3_dendrite.lending.fluidtokens_v4.datums import TxOutRef
from charli3_dendrite.lending.fluidtokens_v4.transactions.common import ledger_order
from charli3_dendrite.lending.fluidtokens_v4.transactions.common import out_ref_of
from charli3_dendrite.lending.fluidtokens_v4.transactions.common import pool_nft_name
from charli3_dendrite.lending.fluidtokens_v4.transactions.pool_action import (
    ManagedPoolSnapshot,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.pool_action import (
    PoolPosition,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.pool_action import (
    add_managed_pool_action,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.pool_action import (
    finish_managed_pool_action,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.redeemers import (
    PoolActionCancel,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.redeemers import (
    PoolCancelAction,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.redeemers import (
    PoolCancelWithdrawRedeemer,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.redeemers import (
    PoolManagerActionCancel,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.redeemers import (
    PoolManagerMintRedeemer,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.redeemers import (
    PoolMintRedeemer,
)

if TYPE_CHECKING:
    from charli3_dendrite.backend.backend_base import AbstractBackend


@dataclass
class PoolCancelSnapshot(ManagedPoolSnapshot):
    """Everything a cancel of one or several pools needs, resolved from chain.

    ``mint_input_ref`` is the spent input the pool policy's burn redeemer names; the
    policy only checks that the transaction spends it.
    """

    mint_input_ref: tuple[str, int]

    @classmethod
    def from_capture(cls, fix: dict) -> PoolCancelSnapshot:
        """Rebuild the snapshot of a captured mainnet cancel (for byte-exact replay)."""
        inputs = [utxo_from_dict(u) for u in fix["inputs"]]
        refs = [utxo_from_dict(u) for u in fix["ref_inputs"]]
        pools = ledger_order(
            u for u in inputs if u.holds_policy(c.POOL_POLICY) and u.datum
        )
        positions = [
            PoolPosition(
                pool=pool,
                pool_manager=next(
                    u
                    for u in inputs
                    if u.holds(c.POOL_MANAGER_POLICY, pool_nft_name(pool).hex())
                ),
            )
            for pool in pools
        ]
        burn = next(
            PoolMintRedeemer.from_cbor(r["cbor"])
            for r in fix["redeemers"]
            if r["purpose"] == "mint" and r["script_hash"] == c.POOL_POLICY
        )
        used = {out_ref_of(u) for p in positions for u in (p.pool, p.pool_manager)}
        return cls(
            positions=positions,
            funding=[u for u in inputs if u.out_ref not in used],
            config=next(
                u for u in refs if u.holds(c.CONFIG_NFT_POLICY, c.CONFIG_NFT_NAME)
            ),
            pool_spend_script_ref=script_ref_by_hash(refs, c.POOL_SPEND_SKH),
            pool_manager_spend_script_ref=script_ref_by_hash(
                refs,
                c.POOL_MANAGER_SPEND_SKH,
            ),
            pool_policy_script_ref=script_ref_by_hash(refs, c.POOL_POLICY),
            pool_manager_policy_script_ref=script_ref_by_hash(
                refs,
                c.POOL_MANAGER_POLICY,
            ),
            action_script_ref=script_ref_by_hash(refs, c.POOL_CANCEL_ACTION_SKH),
            manager_action_script_ref=script_ref_by_hash(
                refs,
                c.POOL_MANAGER_CANCEL_ACTION_SKH,
            ),
            mint_input_ref=(burn.input_ref.tx_id.hex(), burn.input_ref.index),
        )

    @classmethod
    def from_backend(
        cls,
        backend: AbstractBackend,
        *,
        pools: Sequence[tuple[str, int]],
        lender_address: str,
        funding: Sequence[Utxo] | None = None,
        allow_spent: bool = False,
    ) -> PoolCancelSnapshot:
        """Resolve a cancel of each pool in ``pools`` (out-refs) from the backend.

        Every pool must be owned by the key of ``lender_address``, whose UTxOs fund
        the fee unless ``funding`` is given. The burn names the first funding UTxO
        in ledger order, or the first pool without funding.
        """
        from charli3_dendrite.lending.fluidtokens_v4.transactions.resolve import (
            resolve_pool_action,
        )

        context = resolve_pool_action(
            backend,
            action="cancel",
            pool_out_refs=pools,
            lender_address=lender_address,
            funding=funding,
            allow_spent=allow_spent,
        )
        first = ledger_order(context.funding or [p.pool for p in context.positions])
        return cls(
            positions=context.positions,
            funding=context.funding,
            config=context.config,
            pool_spend_script_ref=context.pool_spend_script_ref,
            pool_manager_spend_script_ref=context.pool_manager_spend_script_ref,
            pool_policy_script_ref=context.pool_policy_script_ref,
            pool_manager_policy_script_ref=context.pool_manager_policy_script_ref,
            action_script_ref=context.action_script_ref,
            manager_action_script_ref=context.manager_action_script_ref,
            mint_input_ref=out_ref_of(first[0]),
        )


def build_pool_cancel(
    tx_builder: TransactionBuilder,
    *,
    snapshot: PoolCancelSnapshot,
) -> None:
    """Add a cancel of every pool of ``snapshot`` to ``tx_builder``.

    The pools' value lands in the caller's change. The caller balances, signs (with
    every pool-manager owner's key) and submits.

    The cancel must be the only pool action of the transaction. Everything else the
    caller wants among the transaction's inputs, reference inputs, mints and
    withdrawals must be added before this call, which fills their indexes last;
    outputs may be added after, except to the pool and pool-manager scripts. Discard
    the builder if this raises.
    """
    positions = snapshot.ordered_positions
    action = PoolCancelWithdrawRedeemer(
        config_ref_input_index=0,
        actions_for_each_input=[PoolCancelAction(pool_id=p.pool_id) for p in positions],
    )
    redeemers = add_managed_pool_action(
        tx_builder,
        snapshot=snapshot,
        pool_action=PoolActionCancel(),
        manager_action=PoolManagerActionCancel(),
        action=action,
    )
    spent = {
        (bytes(u.input.transaction_id).hex(), u.input.index) for u in tx_builder.inputs
    }
    if snapshot.mint_input_ref not in spent:
        raise ValueError(
            f"the burn names {snapshot.mint_input_ref}, which the transaction does "
            "not spend",
        )

    manager_mint = PoolManagerMintRedeemer(
        config_ref_input_index=0,
        pool_withdraw_redeemer_index=0,
    )
    pool_mint = PoolMintRedeemer(
        config_ref_input_index=0,
        input_ref=TxOutRef(
            tx_id=bytes.fromhex(snapshot.mint_input_ref[0]),
            index=snapshot.mint_input_ref[1],
        ),
    )
    tx_builder.add_minting_script(
        to_pycardano_utxo(snapshot.pool_manager_policy_script_ref),
        redeemer=Redeemer(manager_mint),
    )
    tx_builder.add_minting_script(
        to_pycardano_utxo(snapshot.pool_policy_script_ref),
        redeemer=Redeemer(pool_mint),
    )
    burn = MultiAsset(
        {
            ScriptHash(bytes.fromhex(policy)): Asset(
                {AssetName(p.pool_id): -1 for p in positions},
            )
            for policy in (c.POOL_MANAGER_POLICY, c.POOL_POLICY)
        },
    )
    tx_builder.mint = burn if tx_builder.mint is None else tx_builder.mint + burn
    finish_managed_pool_action(
        tx_builder,
        snapshot=snapshot,
        redeemers=redeemers,
        manager_mint=manager_mint,
        pool_mint=pool_mint,
    )
