"""What a lender sets on a V4 pool, and what the pool datum and value must be.

A pool create and a pool edit take the lender's :class:`LenderTerms`. The rest of a
new pool's datum is wired here: the pool is permissionless, it is authorised by its
pool manager, and it sends lender bonds to the lender manager under the lender's
stake credential, committing to the lender-bond datum. The pool scripts check none of
this when a pool is created, and a datum the later actions cannot satisfy locks the
pool's funds, so the datum is only ever built here.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import cbor2  # type: ignore[import-not-found]
from pycardano import Address
from pycardano import RawPlutusData
from pycardano import ScriptHash
from pycardano import TransactionOutput

from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.lending.fluidtokens.transactions._common import plutus_address
from charli3_dendrite.lending.fluidtokens_v4 import constants as c
from charli3_dendrite.lending.fluidtokens_v4.datums import AuthCardanoWithdrawScript
from charli3_dendrite.lending.fluidtokens_v4.datums import CollateralAsset
from charli3_dendrite.lending.fluidtokens_v4.datums import CommonData
from charli3_dendrite.lending.fluidtokens_v4.datums import (
    NoLiquidationDutchAuctionClaim,
)
from charli3_dendrite.lending.fluidtokens_v4.datums import PoolDatum
from charli3_dendrite.lending.fluidtokens_v4.datums import PoolManagerDatum
from charli3_dendrite.lending.fluidtokens_v4.state import decode_repayment_mode
from charli3_dendrite.lending.fluidtokens_v4.transactions.common import min_ada
from charli3_dendrite.lending.fluidtokens_v4.transactions.lender_bond import (
    lender_bond_commitment,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.lender_bond import (
    lender_manager_datum,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.loan_terms import (
    INTEREST_ON_REMAINING_PRINCIPAL,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.loan_terms import PERPETUAL
from charli3_dendrite.lending.fluidtokens_v4.transactions.redeemers import BoolFalse
from charli3_dendrite.lending.fluidtokens_v4.transactions.redeemers import BoolTrue
from charli3_dendrite.lending.units import constr
from charli3_dendrite.utility import asset_to_value

# The permissioned-condition hash of a pool anyone may borrow from.
PERMISSIONLESS = b"NONE"

# Plutus ``Bool`` constructor alternative of ``True``.
_BOOL_TRUE = 1

# Quantities at their longest encoding: a 64-bit coin and a 63-bit token amount.
_MAX_COIN = 2**64 - 1
_MAX_TOKENS = 2**63 - 1


@dataclass(frozen=True)
class LenderTerms:
    """The terms a lender sets on a pool: its loan terms and the collateral it takes.

    ``collateral_options``, ``min_collateral`` and ``min_collateral_divider`` are
    index-aligned. An oracle-priced pool (``dynamic_collateral_price``) lends up to
    ``min_collateral[i] / min_collateral_divider[i]`` of option ``i``'s value;
    otherwise a loan locks at least ``principal * min_collateral[i] /
    min_collateral_divider[i]`` units of it.
    """

    common_data: CommonData
    collateral_options: Sequence[CollateralAsset]
    min_collateral: Sequence[int]
    min_collateral_divider: Sequence[int]
    dynamic_collateral_price: bool = True

    @classmethod
    def from_pool_datum(cls, datum: PoolDatum) -> LenderTerms:
        """The lender terms a pool datum carries."""
        return cls(
            common_data=datum.common_data,
            collateral_options=list(datum.collateral_options),
            min_collateral=list(datum.min_collateral),
            min_collateral_divider=list(datum.min_collateral_divider),
            dynamic_collateral_price=constr(datum.dynamic_collateral_price)[0]
            == _BOOL_TRUE,
        )

    def check(self) -> None:
        """Raise ``ValueError`` for terms the contracts would refuse to lend on.

        That includes a zero-rate amortized repayment mode (the installment formula
        divides by zero on-chain, so such a loan could never be repaid) and the
        Dutch-auction liquidation claim (disabled on-chain, so a defaulted loan could
        never be claimed).
        """
        common = self.common_data
        if bytes(common.principal_asset.policy_id).hex() == c.POOL_POLICY:
            raise ValueError("a pool cannot lend its own pool NFTs")
        sizes = {
            len(self.collateral_options),
            len(self.min_collateral),
            len(self.min_collateral_divider),
        }
        if len(sizes) != 1:
            raise ValueError(
                "collateral options, minimums and dividers must be index-aligned",
            )
        if not self.collateral_options:
            raise ValueError("a pool needs at least one collateral option")
        if min(self.min_collateral) <= 0 or min(self.min_collateral_divider) <= 0:
            raise ValueError("collateral minimums and dividers must be positive")
        alt, _ = decode_repayment_mode(common.repayment_mode)
        if alt != PERPETUAL and common.total_installments <= 0:
            raise ValueError("an installment loan needs at least one installment")
        if alt == INTEREST_ON_REMAINING_PRINCIPAL and common.interest_rate == 0:
            raise ValueError("a zero-rate amortized loan cannot be repaid on-chain")
        if (
            constr(common.liquidation_mode)[0]
            == NoLiquidationDutchAuctionClaim.CONSTR_ID
        ):
            raise ValueError(
                "the Dutch auction is disabled on-chain, so a defaulted loan could "
                "not be claimed",
            )

    def apply(self, datum: PoolDatum) -> PoolDatum:
        """``datum`` carrying these terms; every other field is kept."""
        return PoolDatum(
            permissioned_condition_script_hash=datum.permissioned_condition_script_hash,
            extra_data=datum.extra_data,
            common_data=self.common_data,
            lender_auth=datum.lender_auth,
            lender_bond_address=datum.lender_bond_address,
            lender_bond_inline_datum_hash=datum.lender_bond_inline_datum_hash,
            collateral_options=list(self.collateral_options),
            min_collateral=list(self.min_collateral),
            min_collateral_divider=list(self.min_collateral_divider),
            dynamic_collateral_price=BoolTrue()
            if self.dynamic_collateral_price
            else BoolFalse(),
        )


def new_pool_datum(
    terms: LenderTerms,
    *,
    lender_address: str,
    pool_id: bytes,
    pool_manager: PoolManagerDatum,
    convert_liquidations: bool,
    liquidation_fee_per_mille: int,
) -> PoolDatum:
    """The datum of new pool ``pool_id`` on ``terms``.

    Permissionless, authorised by the pool-manager policy, and sending lender bonds to
    the lender manager under the stake credential of ``lender_address``, committed to
    the lender-manager datum of ``pool_manager``'s owner with the given liquidation
    settings.
    """
    lender = Address.decode(lender_address)
    datum = terms.apply(
        PoolDatum(
            permissioned_condition_script_hash=PERMISSIONLESS,
            extra_data=RawPlutusData(cbor2.CBORTag(121, [])),
            common_data=terms.common_data,
            lender_auth=AuthCardanoWithdrawScript(
                script_hash=bytes.fromhex(c.POOL_MANAGER_POLICY),
            ),
            lender_bond_address=plutus_address(
                Address(
                    payment_part=ScriptHash(bytes.fromhex(c.LENDER_MANAGER_SPEND_SKH)),
                    staking_part=lender.staking_part,
                    network=lender.network,
                ),
            ),
            lender_bond_inline_datum_hash=b"",
            collateral_options=[],
            min_collateral=[],
            min_collateral_divider=[],
            dynamic_collateral_price=BoolTrue(),
        ),
    )
    datum.lender_bond_inline_datum_hash = lender_bond_commitment(
        lender_manager_datum(
            datum,
            pool_id=pool_id,
            pool_manager=pool_manager,
            convert_liquidations=convert_liquidations,
            liquidation_fee_per_mille=liquidation_fee_per_mille,
        ),
    )
    return datum


def pool_min_ada(
    address: Address,
    datum: PoolDatum,
    *,
    pool_id: bytes,
    other_assets: dict[str, int] | None = None,
) -> int:
    """The least ADA a pool must keep beyond its principal while it has ``datum``.

    Borrows shrink and compounding grows only the principal, so the pool is sized
    with its coin and its principal at their longest encoding: no later borrow or
    compound can then leave it below its minimum. ``other_assets`` are any tokens the
    pool holds besides its NFT and its principal.
    """
    assets = {c.POOL_POLICY + pool_id.hex(): 1, **(other_assets or {})}
    principal = datum.common_data.principal_asset.unit()
    if principal != "lovelace":
        assets[principal] = _MAX_TOKENS
    output = TransactionOutput(
        address,
        asset_to_value(Assets(**{"lovelace": _MAX_COIN, **assets})),
        datum=datum,
    )
    return min_ada(output)
