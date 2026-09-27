"""The shape every V4 lender pool action (create, edit, cancel) shares.

A pool and its pool manager hold NFTs of the same name, minted together by a pool
create. A pool's ``lender_auth`` is a withdrawal from the pool-manager policy, so an
edit or cancel spends each pool together with its pool manager: the pool-manager
dispatch withdraw names the owner-check script, which requires each pool manager's
owner to sign.

The owner-check scripts pair the i-th spent pool with the i-th spent pool manager,
each counted in the ledger's input order, so an edit or cancel of several pools needs
their pools and pool managers to sort alike. The pool scripts read a pool's output by
its position among the outputs to the pool script, and one pool dispatch withdraw
covers every pool a transaction spends, so a pool create, edit or cancel must be the
only pool action of its transaction.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pycardano import Address
from pycardano import Redeemer
from pycardano import ScriptHash
from pycardano import TransactionBuilder
from pycardano import VerificationKeyHash

from charli3_dendrite.lending.fluidtokens.transactions._common import ref_index
from charli3_dendrite.lending.fluidtokens.transactions._common import reward_address
from charli3_dendrite.lending.fluidtokens.transactions.utxos import Utxo
from charli3_dendrite.lending.fluidtokens.transactions.utxos import ogmios_entry
from charli3_dendrite.lending.fluidtokens.transactions.utxos import to_pycardano_utxo
from charli3_dendrite.lending.fluidtokens_v4 import constants as c
from charli3_dendrite.lending.fluidtokens_v4.datums import AuthCardanoSignature
from charli3_dendrite.lending.fluidtokens_v4.datums import AuthCardanoWithdrawScript
from charli3_dendrite.lending.fluidtokens_v4.datums import PoolDatum
from charli3_dendrite.lending.fluidtokens_v4.datums import PoolManagerDatum
from charli3_dendrite.lending.fluidtokens_v4.transactions.common import (
    add_zero_withdrawals,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.common import out_ref_of
from charli3_dendrite.lending.fluidtokens_v4.transactions.common import pool_nft_name
from charli3_dendrite.lending.fluidtokens_v4.transactions.common import script_hash_of
from charli3_dendrite.lending.fluidtokens_v4.transactions.common import sole_nft_name
from charli3_dendrite.lending.fluidtokens_v4.transactions.common import (
    withdraw_redeemer_position,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.redeemers import (
    LoanSpendRedeemer,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.redeemers import (
    PoolManagerActionWithdrawRedeemer,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.redeemers import (
    PoolManagerMintRedeemer,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.redeemers import (
    PoolManagerWithdrawRedeemer,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.redeemers import (
    PoolWithdrawRedeemer,
)
from charli3_dendrite.lending.transactions.snapshot import PoolActionSnapshot
from charli3_dendrite.lending.units import constr

if TYPE_CHECKING:
    from pycardano import PlutusData

_POOL_SCRIPTS = {c.POOL_SPEND_SKH, c.POOL_MANAGER_SPEND_SKH}
_POOL_POLICIES = {c.POOL_POLICY, c.POOL_MANAGER_POLICY}


def owner_pkh(manager: PoolManagerDatum) -> bytes:
    """The key hash that owns a pool manager.

    Raises ``NotImplementedError`` for an owner that is not a key: such a pool cannot
    be edited or cancelled with a wallet signature.
    """
    alt, fields = constr(manager.pool_owner_auth)
    if alt != AuthCardanoSignature.CONSTR_ID:
        raise NotImplementedError("only a pool manager owned by a key is supported")
    return bytes(fields[0])


@dataclass
class PoolPosition:
    """A pool UTxO and its pool-manager UTxO."""

    pool: Utxo
    pool_manager: Utxo

    @property
    def out_ref(self) -> tuple[str, int]:
        """The pool UTxO's out-ref."""
        return out_ref_of(self.pool)

    @property
    def pool_datum(self) -> PoolDatum:
        """The pool's datum."""
        if self.pool.datum is None:
            raise ValueError("pool UTxO is missing its datum")
        return PoolDatum.from_cbor(self.pool.datum)

    @property
    def manager_datum(self) -> PoolManagerDatum:
        """The pool manager's datum."""
        if self.pool_manager.datum is None:
            raise ValueError("pool-manager UTxO is missing its datum")
        return PoolManagerDatum.from_cbor(self.pool_manager.datum)

    @property
    def pool_id(self) -> bytes:
        """The pool NFT name, shared by the pool-manager NFT."""
        return pool_nft_name(self.pool)

    @property
    def owner_pkh(self) -> bytes:
        """The key hash that must sign an edit or cancel of the pool."""
        return owner_pkh(self.manager_datum)

    def check(self) -> None:
        """Raise unless the pool is managed by this pool manager.

        The pool must be authorised by the pool-manager policy and the pool manager
        must hold the NFT of the pool's name.
        """
        alt, fields = constr(self.pool_datum.lender_auth)
        if not (
            alt == AuthCardanoWithdrawScript.CONSTR_ID
            and bytes(fields[0]).hex() == c.POOL_MANAGER_POLICY
        ):
            raise NotImplementedError(
                f"pool {self.out_ref} is not authorised by its pool manager",
            )
        name = sole_nft_name(self.pool_manager, c.POOL_MANAGER_POLICY, "pool-manager")
        if name != self.pool_id:
            raise ValueError(
                f"pool manager {out_ref_of(self.pool_manager)} does not manage pool "
                f"{self.out_ref}",
            )


@dataclass
class ManagedPoolSnapshot(PoolActionSnapshot):
    """What a pool edit or cancel resolves: the pools, their managers, the scripts."""

    positions: Sequence[PoolPosition]
    funding: list[Utxo]
    config: Utxo
    pool_spend_script_ref: Utxo
    pool_manager_spend_script_ref: Utxo
    pool_policy_script_ref: Utxo
    pool_manager_policy_script_ref: Utxo
    action_script_ref: Utxo
    manager_action_script_ref: Utxo

    @property
    def ordered_positions(self) -> list[PoolPosition]:
        """The positions in the order the ledger sorts their pool inputs."""
        return sorted(
            self.positions,
            key=lambda p: (bytes.fromhex(p.out_ref[0]), p.out_ref[1]),
        )


def require_sole_pool_action(tx_builder: TransactionBuilder) -> None:
    """Raise if ``tx_builder`` already holds a pool action.

    That is any input or output at the pool or pool-manager script, a mint under
    either policy, or a withdrawal from either policy.
    """
    addresses = [u.output.address for u in tx_builder.inputs] + [
        o.address for o in tx_builder.outputs
    ]
    touches = any(
        isinstance(a.payment_part, ScriptHash)
        and a.payment_part.payload.hex() in _POOL_SCRIPTS
        for a in addresses
    )
    touches = touches or any(
        bytes(policy).hex() in _POOL_POLICIES for policy in (tx_builder.mint or {})
    )
    touches = touches or any(
        reward_address(policy) in (tx_builder.withdrawals or {})
        for policy in _POOL_POLICIES
    )
    if touches:
        raise ValueError(
            "a pool create, edit or cancel must be the only pool action in its "
            "transaction",
        )


def require_paired_order(positions: Sequence[PoolPosition]) -> None:
    """Raise unless the pools and their pool managers sort in the same order.

    ``positions`` are in pool-input order. The owner checks pair the i-th spent pool
    with the i-th spent pool manager, so pools whose managers sort differently must
    be edited or cancelled in separate transactions.
    """
    managers = sorted(
        positions,
        key=lambda p: (
            bytes.fromhex(out_ref_of(p.pool_manager)[0]),
            out_ref_of(p.pool_manager)[1],
        ),
    )
    if [p.pool_id for p in managers] != [p.pool_id for p in positions]:
        raise ValueError(
            "these pools and their pool managers sort in different orders; edit or "
            "cancel them in separate transactions",
        )


@dataclass
class ManagedPoolRedeemers:
    """The redeemers of a pool edit or cancel whose indexes are filled last."""

    pool_dispatch: PoolWithdrawRedeemer
    manager_dispatch: PoolManagerWithdrawRedeemer
    action: PlutusData
    manager_action: PoolManagerActionWithdrawRedeemer


def add_managed_pool_action(
    tx_builder: TransactionBuilder,
    *,
    snapshot: ManagedPoolSnapshot,
    pool_action: PlutusData,
    manager_action: PlutusData,
    action: PlutusData,
) -> ManagedPoolRedeemers:
    """Spend the pools and their managers and add the four withdrawals.

    ``pool_action`` and ``manager_action`` are the pool and pool-manager dispatch
    actions and ``action`` the pool action script's redeemer. Every owner is added
    as a required signer. Funding inputs are recorded for Ogmios evaluation; a
    funding UTxO that is one of the pools or managers is skipped. Returns the
    redeemers :func:`finish_managed_pool_action` fills.
    """
    require_sole_pool_action(tx_builder)
    positions = snapshot.ordered_positions
    if not positions:
        raise ValueError("a pool edit or cancel needs at least one pool")
    if len({p.out_ref for p in positions}) != len(positions):
        raise ValueError("a pool edit or cancel names each pool once")
    for position in positions:
        position.check()
    require_paired_order(positions)

    for position in positions:
        tx_builder.add_script_input(
            to_pycardano_utxo(position.pool),
            script=to_pycardano_utxo(snapshot.pool_spend_script_ref),
            redeemer=Redeemer(LoanSpendRedeemer()),
        )
        tx_builder.add_script_input(
            to_pycardano_utxo(position.pool_manager),
            script=to_pycardano_utxo(snapshot.pool_manager_spend_script_ref),
            redeemer=Redeemer(LoanSpendRedeemer()),
        )
    spent = {out_ref_of(u) for p in positions for u in (p.pool, p.pool_manager)}
    for funding in snapshot.funding:
        if funding.out_ref in spent:
            continue
        tx_builder.add_input(to_pycardano_utxo(funding))
        snapshot.add_actor_additional_utxo(ogmios_entry(funding))

    tx_builder.reference_inputs.add(to_pycardano_utxo(snapshot.config))
    redeemers = ManagedPoolRedeemers(
        pool_dispatch=PoolWithdrawRedeemer(
            config_ref_input_index=0,
            action=pool_action,
        ),
        manager_dispatch=PoolManagerWithdrawRedeemer(
            config_ref_input_index=0,
            action=manager_action,
        ),
        action=action,
        manager_action=PoolManagerActionWithdrawRedeemer(
            config_ref_input_index=0,
            pool_withdraw_redeemer_index=0,
            pool_manager_nft_names=[p.pool_id for p in positions],
        ),
    )
    for script_ref, redeemer in (
        (snapshot.pool_policy_script_ref, redeemers.pool_dispatch),
        (snapshot.pool_manager_policy_script_ref, redeemers.manager_dispatch),
        (snapshot.action_script_ref, redeemers.action),
        (snapshot.manager_action_script_ref, redeemers.manager_action),
    ):
        tx_builder.add_withdrawal_script(
            to_pycardano_utxo(script_ref),
            Redeemer(redeemer),
        )
    add_zero_withdrawals(
        tx_builder,
        [
            c.POOL_POLICY,
            c.POOL_MANAGER_POLICY,
            script_hash_of(snapshot.action_script_ref),
            script_hash_of(snapshot.manager_action_script_ref),
        ],
    )
    signers = list(tx_builder.required_signers or [])
    for position in positions:
        signer = VerificationKeyHash(position.owner_pkh)
        if signer not in signers:
            signers.append(signer)
    tx_builder.required_signers = signers
    return redeemers


def finish_managed_pool_action(
    tx_builder: TransactionBuilder,
    *,
    snapshot: ManagedPoolSnapshot,
    redeemers: ManagedPoolRedeemers,
    manager_mint: PoolManagerMintRedeemer | None = None,
    pool_mint: PlutusData | None = None,
) -> None:
    """Fill the config and redeemer indexes once the transaction is laid out.

    Every input, reference input, mint and withdrawal of the transaction must be in
    place: the indexes filled here go stale if one is added afterwards (outputs may
    still be added, except to the pool and pool-manager scripts). Discard the builder
    if a pool action raises part-way.
    """
    config_index = ref_index(tx_builder)[out_ref_of(snapshot.config)]
    pool_withdraw = withdraw_redeemer_position(tx_builder, c.POOL_POLICY)
    for redeemer in (
        redeemers.pool_dispatch,
        redeemers.manager_dispatch,
        redeemers.action,
        redeemers.manager_action,
        manager_mint,
        pool_mint,
    ):
        if redeemer is not None:
            redeemer.config_ref_input_index = config_index  # type: ignore[attr-defined]
    redeemers.manager_action.pool_withdraw_redeemer_index = pool_withdraw
    if manager_mint is not None:
        manager_mint.pool_withdraw_redeemer_index = pool_withdraw


def pool_manager_address(lender_address: str) -> Address:
    """The pool-manager script address under the lender's stake credential."""
    lender = Address.decode(lender_address)
    return Address(
        payment_part=ScriptHash(bytes.fromhex(c.POOL_MANAGER_SPEND_SKH)),
        staking_part=lender.staking_part,
        network=lender.network,
    )


def pool_address(lender_address: str) -> Address:
    """The pool script address under the lender's stake credential."""
    lender = Address.decode(lender_address)
    return Address(
        payment_part=ScriptHash(bytes.fromhex(c.POOL_SPEND_SKH)),
        staking_part=lender.staking_part,
        network=lender.network,
    )
