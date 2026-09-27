"""FluidTokens V4 pool edit: new terms, or principal added or withdrawn, per pool.

Each pool and its pool manager are spent; the pool continues at its address with its
NFT, and the pool manager continues unchanged. The pool-manager owner signs.

The contracts let an edit change any pool datum field but the principal and its
oracle, and the pool's value freely. This builder changes only the lender terms and
the principal: the pool stays authorised by its manager and keeps its lender-bond
commitment, and it always keeps the ADA it must hold.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pycardano import Address
from pycardano import RawCBOR
from pycardano import TransactionBuilder
from pycardano import TransactionOutput

from charli3_dendrite.lending.fluidtokens.transactions.utxos import Utxo
from charli3_dendrite.lending.fluidtokens.transactions.utxos import script_ref_by_hash
from charli3_dendrite.lending.fluidtokens.transactions.utxos import to_pycardano_utxo
from charli3_dendrite.lending.fluidtokens.transactions.utxos import utxo_from_dict
from charli3_dendrite.lending.fluidtokens.transactions.utxos import utxo_value
from charli3_dendrite.lending.fluidtokens_v4 import constants as c
from charli3_dendrite.lending.fluidtokens_v4.transactions.common import ledger_order
from charli3_dendrite.lending.fluidtokens_v4.transactions.common import min_ada
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
from charli3_dendrite.lending.fluidtokens_v4.transactions.pool_terms import LenderTerms
from charli3_dendrite.lending.fluidtokens_v4.transactions.pool_terms import pool_min_ada
from charli3_dendrite.lending.fluidtokens_v4.transactions.redeemers import (
    PoolActionEdit,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.redeemers import (
    PoolEditActionWithdrawRedeemer,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.redeemers import PoolEditData
from charli3_dendrite.lending.fluidtokens_v4.transactions.redeemers import (
    PoolManagerActionEditPool,
)

if TYPE_CHECKING:
    from charli3_dendrite.backend.backend_base import AbstractBackend


@dataclass(frozen=True)
class PoolEdit:
    """A change to one pool: new lender terms, principal moved, or both.

    ``terms`` replaces the pool's lender terms (``None`` keeps them) and must keep its
    principal and principal oracle. ``principal_change`` adds principal to the pool;
    a negative change withdraws it.
    """

    pool_out_ref: tuple[str, int]
    terms: LenderTerms | None = None
    principal_change: int = 0


@dataclass
class EditedPool(PoolPosition):
    """A pool to edit and what it continues with: its datum (CBOR hex) and value."""

    datum: str
    lovelace: int
    assets: list[tuple[str, str, int]]


def edited_pool(position: PoolPosition, edit: PoolEdit) -> EditedPool:
    """``position`` continued with ``edit`` applied.

    The pool keeps the ADA it must hold under its new datum: an ADA pool is topped up
    to it unless the edit withdraws, and a token pool's ADA only ever grows. Raises
    ``ValueError`` for terms that change the principal or its oracle or that no
    borrower could take up, and for a withdrawal of more than the pool can release.
    """
    datum = position.pool_datum
    if edit.terms is not None:
        edit.terms.check()
        new, old = edit.terms.common_data, datum.common_data
        if (new.principal_asset, new.principal_oracle_asset) != (
            old.principal_asset,
            old.principal_oracle_asset,
        ):
            raise ValueError(
                "an edit cannot change the principal or its oracle "
                f"({edit.pool_out_ref})",
            )
        datum = edit.terms.apply(datum)
    pool = position.pool
    principal = datum.common_data.principal_asset.unit()
    nft = c.POOL_POLICY + position.pool_id.hex()
    assets = {p + n: q for p, n, q in pool.assets}
    floor = pool_min_ada(
        Address.decode(pool.address),
        datum,
        pool_id=position.pool_id,
        other_assets={u: q for u, q in assets.items() if u not in (nft, principal)},
    )
    lovelace = pool.lovelace
    if principal == "lovelace":
        lovelace += edit.principal_change
        if edit.principal_change < 0 and lovelace < floor:
            raise ValueError(
                f"pool {edit.pool_out_ref} can release at most "
                f"{max(pool.lovelace - floor, 0)} lovelace",
            )
    else:
        held = assets.get(principal, 0)
        if held + edit.principal_change < 0:
            raise ValueError(f"pool {edit.pool_out_ref} holds {held} of its principal")
        assets[principal] = held + edit.principal_change
    return EditedPool(
        pool=pool,
        pool_manager=position.pool_manager,
        datum=pool.datum if edit.terms is None and pool.datum else datum.to_cbor_hex(),
        lovelace=max(lovelace, floor),
        assets=[(u[:56], u[56:], q) for u, q in assets.items() if q],
    )


@dataclass
class PoolEditSnapshot(ManagedPoolSnapshot):
    """Everything an edit of one or several pools needs, resolved from chain."""

    positions: Sequence[EditedPool]

    @classmethod
    def from_capture(cls, fix: dict) -> PoolEditSnapshot:
        """Rebuild the snapshot of a captured mainnet edit (for byte-exact replay)."""
        inputs = [utxo_from_dict(u) for u in fix["inputs"]]
        outputs = [utxo_from_dict(u) for u in fix["outputs"]]
        refs = [utxo_from_dict(u) for u in fix["ref_inputs"]]
        pools = ledger_order(
            u for u in inputs if u.holds_policy(c.POOL_POLICY) and u.datum
        )
        continuing = [
            u
            for u in outputs
            if Address.decode(u.address).payment_part.payload.hex() == c.POOL_SPEND_SKH
        ]
        positions = [
            EditedPool(
                pool=pool,
                pool_manager=next(
                    u
                    for u in inputs
                    if u.holds(c.POOL_MANAGER_POLICY, pool_nft_name(pool).hex())
                ),
                datum=out.datum or "",
                lovelace=out.lovelace,
                assets=out.assets,
            )
            for pool, out in zip(pools, continuing)
        ]
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
            action_script_ref=script_ref_by_hash(refs, c.POOL_EDIT_ACTION_SKH),
            manager_action_script_ref=script_ref_by_hash(
                refs,
                c.POOL_MANAGER_EDIT_POOL_ACTION_SKH,
            ),
        )

    @classmethod
    def from_backend(
        cls,
        backend: AbstractBackend,
        *,
        edits: Sequence[PoolEdit],
        lender_address: str,
        funding: Sequence[Utxo] | None = None,
        allow_spent: bool = False,
    ) -> PoolEditSnapshot:
        """Resolve each of ``edits`` from the backend.

        Every pool must be owned by the key of ``lender_address``, whose UTxOs fund
        the edit unless ``funding`` is given. Refuses what :func:`edited_pool`
        refuses.
        """
        from charli3_dendrite.lending.fluidtokens_v4.transactions.resolve import (
            resolve_pool_action,
        )

        context = resolve_pool_action(
            backend,
            action="edit",
            pool_out_refs=[e.pool_out_ref for e in edits],
            lender_address=lender_address,
            funding=funding,
            allow_spent=allow_spent,
        )
        return cls(
            positions=[
                edited_pool(position, edit)
                for position, edit in zip(context.positions, edits)
            ],
            funding=context.funding,
            config=context.config,
            pool_spend_script_ref=context.pool_spend_script_ref,
            pool_manager_spend_script_ref=context.pool_manager_spend_script_ref,
            pool_policy_script_ref=context.pool_policy_script_ref,
            pool_manager_policy_script_ref=context.pool_manager_policy_script_ref,
            action_script_ref=context.action_script_ref,
            manager_action_script_ref=context.manager_action_script_ref,
        )


def build_pool_edit(
    tx_builder: TransactionBuilder,
    *,
    snapshot: PoolEditSnapshot,
) -> None:
    """Add an edit of every pool of ``snapshot`` to ``tx_builder``.

    Each continuing pool is followed by its unchanged pool manager among the outputs.
    A withdrawn principal lands in the caller's change. The caller balances, signs
    (with every pool-manager owner's key) and submits.

    The edit must be the only pool action of the transaction. Everything else the
    caller wants among the transaction's inputs, reference inputs, mints and
    withdrawals must be added before this call, which fills their indexes last;
    outputs may be added after, except to the pool and pool-manager scripts. Discard
    the builder if this raises.
    """
    positions: list[EditedPool] = snapshot.ordered_positions  # type: ignore[assignment]
    action = PoolEditActionWithdrawRedeemer(
        config_ref_input_index=0,
        actions_for_each_input=[PoolEditData(pool_id=p.pool_id) for p in positions],
    )
    redeemers = add_managed_pool_action(
        tx_builder,
        snapshot=snapshot,
        pool_action=PoolActionEdit(),
        manager_action=PoolManagerActionEditPool(),
        action=action,
    )
    for position in positions:
        tx_builder.add_output(_continuing_pool(position))
        tx_builder.add_output(_continuing_manager(position))
    finish_managed_pool_action(tx_builder, snapshot=snapshot, redeemers=redeemers)


def _continuing_pool(position: EditedPool) -> TransactionOutput:
    """The edited pool at its address; raises below the ADA the output needs."""
    output = TransactionOutput(
        Address.decode(position.pool.address),
        utxo_value(position.lovelace, position.assets),
        datum=RawCBOR(bytes.fromhex(position.datum)),
    )
    floor = min_ada(output)
    if position.lovelace < floor:
        raise ValueError(
            f"edited pool {position.out_ref} holds {position.lovelace} lovelace, "
            f"below the {floor} its output needs",
        )
    return output


def _continuing_manager(position: EditedPool) -> TransactionOutput:
    """The pool manager, re-created exactly as spent, reference script included.

    The owner check compares the whole spent output, so a manager carrying a
    reference script must continue with it.
    """
    return to_pycardano_utxo(position.pool_manager).output
