"""FluidTokens V4 pool create: one or several lender pools, each with its manager.

Each new pool locks the lender's principal at the pool script under the lender's
stake credential, and its pool manager records the owner who may later edit or cancel
it. The pool NFT and the pool-manager NFT share one name: the pool's index among the
transaction's new pools, then ``blake2b_224`` of a spent input's out-ref (the "input
ref"), so the names are unique. The pool-manager policy only mints alongside an
identical pool mint, so a pool without a manager cannot be created here.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pycardano import Address
from pycardano import Asset
from pycardano import AssetName
from pycardano import MultiAsset
from pycardano import Redeemer
from pycardano import ScriptHash
from pycardano import TransactionBuilder
from pycardano import TransactionOutput
from pycardano import VerificationKeyHash

from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.lending.fluidtokens.transactions._common import ref_index
from charli3_dendrite.lending.fluidtokens.transactions.utxos import Utxo
from charli3_dendrite.lending.fluidtokens.transactions.utxos import loan_id_from_out_ref
from charli3_dendrite.lending.fluidtokens.transactions.utxos import ogmios_entry
from charli3_dendrite.lending.fluidtokens.transactions.utxos import script_ref_by_hash
from charli3_dendrite.lending.fluidtokens.transactions.utxos import to_pycardano_utxo
from charli3_dendrite.lending.fluidtokens.transactions.utxos import utxo_from_dict
from charli3_dendrite.lending.fluidtokens_v4 import constants as c
from charli3_dendrite.lending.fluidtokens_v4.datums import AuthCardanoSignature
from charli3_dendrite.lending.fluidtokens_v4.datums import LenderManagerDatum
from charli3_dendrite.lending.fluidtokens_v4.datums import PoolDatum
from charli3_dendrite.lending.fluidtokens_v4.datums import PoolManagerDatum
from charli3_dendrite.lending.fluidtokens_v4.datums import TxOutRef
from charli3_dendrite.lending.fluidtokens_v4.transactions.common import ledger_order
from charli3_dendrite.lending.fluidtokens_v4.transactions.common import out_ref_of
from charli3_dendrite.lending.fluidtokens_v4.transactions.common import settled_min_ada
from charli3_dendrite.lending.fluidtokens_v4.transactions.lender_bond import (
    CONVERT_LIQUIDATIONS,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.lender_bond import (
    LIQUIDATION_FEE_PER_MILLE,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.lender_bond import (
    MAX_FEE_PER_MILLE,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.lender_bond import (
    lender_bond_datum,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.pool_action import owner_pkh
from charli3_dendrite.lending.fluidtokens_v4.transactions.pool_action import (
    pool_address,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.pool_action import (
    pool_manager_address,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.pool_action import (
    require_sole_pool_action,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.pool_terms import LenderTerms
from charli3_dendrite.lending.fluidtokens_v4.transactions.pool_terms import (
    new_pool_datum,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.pool_terms import pool_min_ada
from charli3_dendrite.lending.fluidtokens_v4.transactions.redeemers import (
    PoolManagerMintRedeemer,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.redeemers import (
    PoolMintRedeemer,
)
from charli3_dendrite.lending.transactions.snapshot import PoolActionSnapshot
from charli3_dendrite.lending.units import constr
from charli3_dendrite.utility import asset_to_value

if TYPE_CHECKING:
    from charli3_dendrite.backend.backend_base import AbstractBackend

# The compounding fee per mille every pool manager carried when this module was
# written: what a compounding bot keeps of the interest it reinvests.
COMPOUNDING_FEE_PER_MILLE = 5

# A pool mint names at most 256 pools: the index is one byte.
_MAX_POOLS = 256

# Plutus ``Bool`` constructor alternative of ``True``.
_BOOL_TRUE = 1


def new_pool_id(input_ref: tuple[str, int], index: int) -> bytes:
    """The NFT name of the ``index``-th pool created from ``input_ref``."""
    return bytes([index]) + loan_id_from_out_ref(input_ref)


@dataclass(frozen=True)
class PoolOffer:
    """One new pool: the principal the lender puts in and the terms it lends on.

    ``reserve_lovelace`` is the ADA the pool keeps beyond its principal; it defaults
    to the least the pool must keep, and may not be less.
    """

    terms: LenderTerms
    principal_amount: int
    reserve_lovelace: int | None = None


def lender_owner_pkh(lender_address: str) -> bytes:
    """The key hash of ``lender_address``, which owns the new pools."""
    payment = Address.decode(lender_address).payment_part
    if not isinstance(payment, VerificationKeyHash):
        raise NotImplementedError("only a key address can own a pool")
    return payment.payload


@dataclass
class PoolCreateSnapshot(PoolActionSnapshot):
    """Everything a create of one or several pools needs, resolved from chain.

    The key of ``lender_address`` owns the new pool managers; its stake credential
    stakes the pools and receives their lender bonds. ``input_ref`` is the spent
    input the pool names derive from.
    """

    offers: list[PoolOffer]
    lender_address: str
    funding: list[Utxo]
    config: Utxo
    pool_policy_script_ref: Utxo
    pool_manager_policy_script_ref: Utxo
    input_ref: tuple[str, int]
    compounding_fee_per_mille: int = COMPOUNDING_FEE_PER_MILLE
    convert_liquidations: bool = CONVERT_LIQUIDATIONS
    liquidation_fee_per_mille: int = LIQUIDATION_FEE_PER_MILLE

    @property
    def pool_ids(self) -> list[bytes]:
        """The NFT name of each new pool, in offer order."""
        return [new_pool_id(self.input_ref, k) for k in range(len(self.offers))]

    @property
    def pool_manager_datum(self) -> PoolManagerDatum:
        """The datum every new pool manager carries."""
        return PoolManagerDatum(
            pool_owner_auth=AuthCardanoSignature(
                key_hash=lender_owner_pkh(self.lender_address),
            ),
            compounding_fee_per_mille=self.compounding_fee_per_mille,
        )

    def pool_datum(self, index: int) -> PoolDatum:
        """The datum of the ``index``-th new pool."""
        return new_pool_datum(
            self.offers[index].terms,
            lender_address=self.lender_address,
            pool_id=self.pool_ids[index],
            pool_manager=self.pool_manager_datum,
            convert_liquidations=self.convert_liquidations,
            liquidation_fee_per_mille=self.liquidation_fee_per_mille,
        )

    def pool_output(self, index: int) -> TransactionOutput:
        """The ``index``-th new pool: its principal, reserve, NFT and datum.

        Raises ``ValueError`` for a non-positive principal or a reserve below the
        pool's minimum.
        """
        offer = self.offers[index]
        pool_id = self.pool_ids[index]
        datum = self.pool_datum(index)
        address = pool_address(self.lender_address)
        if offer.principal_amount <= 0:
            raise ValueError("a pool must lend a positive principal")
        floor = pool_min_ada(address, datum, pool_id=pool_id)
        reserve = floor if offer.reserve_lovelace is None else offer.reserve_lovelace
        if reserve < floor:
            raise ValueError(
                f"pool {index} keeps {reserve} lovelace beyond its principal, below "
                f"the {floor} it must keep",
            )
        principal = datum.common_data.principal_asset.unit()
        assets = {"lovelace": reserve, c.POOL_POLICY + pool_id.hex(): 1}
        if principal == "lovelace":
            assets["lovelace"] += offer.principal_amount
        else:
            assets[principal] = offer.principal_amount
        return TransactionOutput(address, asset_to_value(Assets(**assets)), datum=datum)

    def pool_manager_output(self, index: int) -> TransactionOutput:
        """The ``index``-th new pool manager, holding the least ADA it can."""
        address = pool_manager_address(self.lender_address)
        assets = {c.POOL_MANAGER_POLICY + self.pool_ids[index].hex(): 1}
        datum = self.pool_manager_datum
        return TransactionOutput(
            address,
            asset_to_value(
                Assets(
                    **{"lovelace": settled_min_ada(address, assets, datum), **assets},
                ),
            ),
            datum=datum,
        )

    def check(self) -> None:
        """Raise for a create the contracts or later actions would refuse.

        That is no offers or more than a mint can name, a lender that is not a key
        address, a fee outside 0 to 1000 per mille, or offers with terms no borrower
        could take up.
        """
        if not self.offers:
            raise ValueError("a pool create needs at least one offer")
        if len(self.offers) > _MAX_POOLS:
            raise ValueError(f"a pool create names at most {_MAX_POOLS} pools")
        lender_owner_pkh(self.lender_address)
        for fee in (self.compounding_fee_per_mille, self.liquidation_fee_per_mille):
            if not 0 <= fee <= MAX_FEE_PER_MILLE:
                raise ValueError(f"a fee per mille must be 0 to {MAX_FEE_PER_MILLE}")
        for offer in self.offers:
            offer.terms.check()

    @classmethod
    def from_capture(cls, fix: dict) -> PoolCreateSnapshot:
        """Rebuild the snapshot of a captured mainnet create (for byte-exact replay).

        The lender is the pool manager's owner key with the pools' stake credential.
        A captured ADA pool cannot tell its reserve apart from its principal, so the
        offer keeps the least reserve the pool could hold and counts the rest as
        principal; the replay stays byte-exact either way.
        """
        outputs = [utxo_from_dict(u) for u in fix["outputs"]]
        refs = [utxo_from_dict(u) for u in fix["ref_inputs"]]
        pools = [u for u in outputs if u.holds_policy(c.POOL_POLICY)]
        manager = PoolManagerDatum.from_cbor(
            next(u for u in outputs if u.holds_policy(c.POOL_MANAGER_POLICY)).datum
            or "",
        )
        pool_at = Address.decode(pools[0].address)
        lender = Address(
            payment_part=VerificationKeyHash(owner_pkh(manager)),
            staking_part=pool_at.staking_part,
            network=pool_at.network,
        )
        mint = next(
            PoolMintRedeemer.from_cbor(r["cbor"])
            for r in fix["redeemers"]
            if r["purpose"] == "mint" and r["script_hash"] == c.POOL_POLICY
        )
        input_ref = (bytes(mint.input_ref.tx_id).hex(), mint.input_ref.index)
        offers = []
        for index, pool in enumerate(pools):
            datum = PoolDatum.from_cbor(pool.datum or "")
            terms = LenderTerms.from_pool_datum(datum)
            principal = datum.common_data.principal_asset.unit()
            if principal == "lovelace":
                floor = pool_min_ada(
                    pool_at,
                    datum,
                    pool_id=new_pool_id(input_ref, index),
                )
                offers.append(PoolOffer(terms, pool.lovelace - floor))
            else:
                amount = next(q for p, n, q in pool.assets if p + n == principal)
                offers.append(PoolOffer(terms, amount, reserve_lovelace=pool.lovelace))
        committed = LenderManagerDatum.from_cbor(
            lender_bond_datum(
                PoolDatum.from_cbor(pools[0].datum or ""),
                pool_id=new_pool_id(input_ref, 0),
                pool_manager=manager,
            ),
        )
        return cls(
            offers=offers,
            lender_address=lender.encode(),
            funding=[utxo_from_dict(u) for u in fix["inputs"]],
            config=next(
                u for u in refs if u.holds(c.CONFIG_NFT_POLICY, c.CONFIG_NFT_NAME)
            ),
            pool_policy_script_ref=script_ref_by_hash(refs, c.POOL_POLICY),
            pool_manager_policy_script_ref=script_ref_by_hash(
                refs,
                c.POOL_MANAGER_POLICY,
            ),
            input_ref=input_ref,
            compounding_fee_per_mille=manager.compounding_fee_per_mille,
            convert_liquidations=constr(
                committed.should_liquidation_convert_to_principal,
            )[0]
            == _BOOL_TRUE,
            liquidation_fee_per_mille=committed.liquidation_fee_per_mille,
        )

    @classmethod
    def from_backend(
        cls,
        backend: AbstractBackend,
        *,
        offers: Sequence[PoolOffer],
        lender_address: str,
        funding: Sequence[Utxo] | None = None,
        input_ref: tuple[str, int] | None = None,
        compounding_fee_per_mille: int = COMPOUNDING_FEE_PER_MILLE,
        convert_liquidations: bool = CONVERT_LIQUIDATIONS,
        liquidation_fee_per_mille: int = LIQUIDATION_FEE_PER_MILLE,
    ) -> PoolCreateSnapshot:
        """Resolve a create of one pool per offer, owned by ``lender_address``.

        The lender's UTxOs fund the create unless ``funding`` is given; the pool
        names derive from ``input_ref``, by default the first funding UTxO in ledger
        order. The defaults of the fee and liquidation settings are the ones every
        mainnet pool used when this module was written. Refuses what
        :meth:`check` refuses, plus a non-positive principal and a reserve below the
        pool's minimum: it builds every pool output to check them.
        """
        from charli3_dendrite.lending.fluidtokens_v4.transactions.resolve import (
            config_datum,
        )
        from charli3_dendrite.lending.fluidtokens_v4.transactions.resolve import (
            require_known_pool_manager,
        )
        from charli3_dendrite.lending.fluidtokens_v4.transactions.resolve import (
            resolve_config_utxo,
        )
        from charli3_dendrite.lending.fluidtokens_v4.transactions.resolve import (
            resolve_script,
        )
        from charli3_dendrite.lending.fluidtokens_v4.transactions.resolve import (
            resolve_wallet_funding,
        )

        if funding is not None:
            resolved = list(funding)
            if not resolved:
                raise ValueError("the create needs a funding UTxO")
        else:
            resolved = resolve_wallet_funding(backend, lender_address)
            if not resolved:
                raise ValueError(f"no UTxOs at {lender_address} fund the create")
        config = resolve_config_utxo(backend)
        scripts = config_datum(config)
        require_known_pool_manager(scripts)
        snapshot = cls(
            offers=list(offers),
            lender_address=lender_address,
            funding=resolved,
            config=config,
            pool_policy_script_ref=resolve_script(backend, scripts.pool_policy_id),
            pool_manager_policy_script_ref=resolve_script(
                backend,
                scripts.pool_manager_policy_id,
            ),
            input_ref=input_ref or out_ref_of(ledger_order(resolved)[0]),
            compounding_fee_per_mille=compounding_fee_per_mille,
            convert_liquidations=convert_liquidations,
            liquidation_fee_per_mille=liquidation_fee_per_mille,
        )
        snapshot.check()
        for index in range(len(snapshot.offers)):
            snapshot.pool_output(index)
        return snapshot


def build_pool_create(
    tx_builder: TransactionBuilder,
    *,
    snapshot: PoolCreateSnapshot,
) -> None:
    """Add a create of every pool of ``snapshot`` to ``tx_builder``.

    Each new pool is followed by its pool manager among the outputs. Funding inputs
    are recorded for Ogmios evaluation. The caller balances, signs and submits.

    The create must be the only pool action of the transaction, and ``input_ref``
    must be one of its inputs. Everything else the caller wants among the
    transaction's reference inputs must be added before this call, which fills the
    config index last; outputs may be added after, except to the pool and
    pool-manager scripts. Discard the builder if this raises.
    """
    require_sole_pool_action(tx_builder)
    snapshot.check()
    outputs = [
        output
        for index in range(len(snapshot.offers))
        for output in (
            snapshot.pool_output(index),
            snapshot.pool_manager_output(index),
        )
    ]

    for funding in snapshot.funding:
        tx_builder.add_input(to_pycardano_utxo(funding))
        snapshot.add_actor_additional_utxo(ogmios_entry(funding))
    spent = {
        (bytes(u.input.transaction_id).hex(), u.input.index) for u in tx_builder.inputs
    }
    if snapshot.input_ref not in spent:
        raise ValueError(
            f"the pool names derive from {snapshot.input_ref}, which the transaction "
            "does not spend",
        )
    tx_builder.reference_inputs.add(to_pycardano_utxo(snapshot.config))

    manager_mint = PoolManagerMintRedeemer(
        config_ref_input_index=0,
        pool_withdraw_redeemer_index=0,
    )
    pool_mint = PoolMintRedeemer(
        config_ref_input_index=0,
        input_ref=TxOutRef(
            tx_id=bytes.fromhex(snapshot.input_ref[0]),
            index=snapshot.input_ref[1],
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
    mint = MultiAsset(
        {
            ScriptHash(bytes.fromhex(policy)): Asset(
                {AssetName(pool_id): 1 for pool_id in snapshot.pool_ids},
            )
            for policy in (c.POOL_MANAGER_POLICY, c.POOL_POLICY)
        },
    )
    tx_builder.mint = mint if tx_builder.mint is None else tx_builder.mint + mint
    for output in outputs:
        tx_builder.add_output(output)

    config_index = ref_index(tx_builder)[out_ref_of(snapshot.config)]
    manager_mint.config_ref_input_index = config_index
    pool_mint.config_ref_input_index = config_index
