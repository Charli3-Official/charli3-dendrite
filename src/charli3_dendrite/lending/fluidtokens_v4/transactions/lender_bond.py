"""The lender-bond output datum a V4 borrow must reproduce.

A pool commits only ``blake2b_256`` of the lender-bond output's inline datum. Pools
that send lender bonds to the lender manager commit a :class:`LenderManagerDatum`
whose fields all come from chain except two lender settings: the pool NFT name, the
pool's principal asset, the pool manager's owner authorization, and the stake
credential of the pool's lender-bond address, plus whether liquidations convert the
collateral to principal and the liquidation fee per mille. The settings are recovered
by hashing each candidate against the commitment.
"""

from __future__ import annotations

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

# A liquidation fee is expressed per mille.
_MAX_FEE_PER_MILLE = 1000

# The settings every lender-manager pool used when this module was written; tried
# first so the common case needs a single hash.
_USUAL_SETTINGS = (_TRUE, 40)


def _settings() -> Iterator[tuple[RawPlutusData, int]]:
    yield _USUAL_SETTINGS
    for convert in (_TRUE, _FALSE):
        for fee in range(_MAX_FEE_PER_MILLE + 1):
            if (convert, fee) != _USUAL_SETTINGS:
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
    _, (_payment, staking) = constr(pool_datum.lender_bond_address)
    stake_credential = RawPlutusData(
        staking
        if isinstance(staking, cbor2.CBORTag)
        else cbor2.loads(staking.to_cbor()),
    )
    for convert, fee in _settings():
        candidate = LenderManagerDatum(
            lender_auth=pool_manager.pool_owner_auth,
            lender_stake_credential=stake_credential,
            should_liquidation_convert_to_principal=convert,
            liquidation_fee_per_mille=fee,
            pool_id=pool_id,
            principal_asset=pool_datum.common_data.principal_asset,
        ).to_cbor_hex()
        if lender_bond_datum_matches(pool_datum, candidate):
            return candidate
    raise ValueError("no lender-manager setting reproduces the pool's commitment")
