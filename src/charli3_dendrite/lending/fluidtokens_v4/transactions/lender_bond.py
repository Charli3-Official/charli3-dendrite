"""The lender-bond output datum a V4 pool commits to.

A pool commits only ``blake2b_256`` of the lender-bond output's inline datum. Pools
that send lender bonds to the lender manager commit a :class:`LenderManagerDatum`
whose fields all come from chain except two lender settings: the pool NFT name, the
pool's principal asset, the pool manager's owner authorization, and the stake
credential of the pool's lender-bond address, plus whether liquidations convert the
collateral to principal and the liquidation fee per mille. A pool create picks the
settings; a borrow recovers them by hashing each candidate against the commitment.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator

import cbor2  # type: ignore[import-not-found]
from pycardano import RawPlutusData

from charli3_dendrite.lending.fluidtokens.transactions.borrow_terms import (
    lender_bond_datum_matches,
)
from charli3_dendrite.lending.fluidtokens_v4 import constants as c
from charli3_dendrite.lending.fluidtokens_v4.datums import LenderManagerDatum
from charli3_dendrite.lending.fluidtokens_v4.datums import PoolDatum
from charli3_dendrite.lending.fluidtokens_v4.datums import PoolManagerDatum
from charli3_dendrite.lending.units import constr

# Plutus ``Bool`` constructors.
_TRUE = RawPlutusData(cbor2.CBORTag(122, []))
_FALSE = RawPlutusData(cbor2.CBORTag(121, []))

# Plutus ``Credential.ScriptCredential`` is constructor alternative 1.
_SCRIPT_CREDENTIAL = 1

# A fee per mille is 0 to 1000: the range this module's commitment search covers,
# and the range a pool create bounds its own fees to.
MAX_FEE_PER_MILLE = 1000

# The settings every lender-manager pool used when this module was written: a pool
# create defaults to them, and a borrow tries them first so the common case needs a
# single hash.
CONVERT_LIQUIDATIONS = True
LIQUIDATION_FEE_PER_MILLE = 40


def _settings() -> Iterator[tuple[bool, int]]:
    usual = (CONVERT_LIQUIDATIONS, LIQUIDATION_FEE_PER_MILLE)
    yield usual
    for convert in (True, False):
        for fee in range(MAX_FEE_PER_MILLE + 1):
            if (convert, fee) != usual:
                yield convert, fee


def sends_bonds_to_lender_manager(pool_datum: PoolDatum) -> bool:
    """True if the pool's lender-bond address is the lender-manager spend script."""
    _, (payment, _staking) = constr(pool_datum.lender_bond_address)
    alt, (credential,) = constr(payment)
    return alt == _SCRIPT_CREDENTIAL and bytes(credential).hex() == (
        c.LENDER_MANAGER_SPEND_SKH
    )


def lender_bond_datum(
    pool_datum: PoolDatum,
    *,
    pool_id: bytes,
    pool_manager: PoolManagerDatum,
) -> str:
    """The lender-bond inline datum (CBOR hex) that hashes to the pool's commitment.

    Raises ``ValueError`` if the pool does not send lender bonds to the lender manager
    (the borrow builder does not support such pools) or if no setting matches.
    """
    if not sends_bonds_to_lender_manager(pool_datum):
        raise ValueError(
            "pool sends lender bonds to an address other than the lender manager; "
            "the borrow builder does not support such pools",
        )
    for convert, fee in _settings():
        candidate = lender_manager_datum(
            pool_datum,
            pool_id=pool_id,
            pool_manager=pool_manager,
            convert_liquidations=convert,
            liquidation_fee_per_mille=fee,
        ).to_cbor_hex()
        if lender_bond_datum_matches(pool_datum, candidate):
            return candidate
    raise ValueError("no lender-manager setting reproduces the pool's commitment")


def lender_manager_datum(
    pool_datum: PoolDatum,
    *,
    pool_id: bytes,
    pool_manager: PoolManagerDatum,
    convert_liquidations: bool,
    liquidation_fee_per_mille: int,
) -> LenderManagerDatum:
    """The lender-manager datum of pool ``pool_id``'s lender bonds.

    The stake credential is the one of the pool's lender-bond address; the principal
    asset is the pool's.
    """
    _, (_payment, staking) = constr(pool_datum.lender_bond_address)
    return LenderManagerDatum(
        lender_auth=pool_manager.pool_owner_auth,
        lender_stake_credential=RawPlutusData(
            staking
            if isinstance(staking, cbor2.CBORTag)
            else cbor2.loads(staking.to_cbor()),
        ),
        should_liquidation_convert_to_principal=_TRUE
        if convert_liquidations
        else _FALSE,
        liquidation_fee_per_mille=liquidation_fee_per_mille,
        pool_id=pool_id,
        principal_asset=pool_datum.common_data.principal_asset,
    )


def lender_bond_commitment(datum: LenderManagerDatum) -> bytes:
    """What a pool commits to: ``blake2b_256`` of the lender-bond datum."""
    return hashlib.blake2b(datum.to_cbor(), digest_size=32).digest()
