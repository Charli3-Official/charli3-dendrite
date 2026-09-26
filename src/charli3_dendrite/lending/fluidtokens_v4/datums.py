"""pycardano mirrors of the deployed FluidTokens V4 datums (mainnet).

Mirrors ``lib/fluidtokens/types`` of ``ft-cardano-loans-v4`` at ``a8bb3f4d``. Types the
V4 contracts share with V3 are imported from the V3 package. A V4 type that only
appends a field subclasses its V3 class and declares the new field (dataclass
inheritance appends it last, matching the on-chain layout). A V4 type whose nested
field type changed re-annotates that field; a re-annotated dataclass field keeps the
position of its first declaration, so the layout is unchanged.

Every class decodes strictly (see :class:`StrictPlutusData`), so a V3 datum does not
decode as a V4 class wherever the layouts differ, and vice versa.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Union

from pycardano import Datum
from pycardano.exception import DeserializeException

from charli3_dendrite.lending.fluidtokens import datums as v3
from charli3_dendrite.lending.fluidtokens.datums import Asset
from charli3_dendrite.lending.fluidtokens.datums import AuthCardanoMintScript
from charli3_dendrite.lending.fluidtokens.datums import AuthCardanoSignature
from charli3_dendrite.lending.fluidtokens.datums import AuthCardanoSpendScript
from charli3_dendrite.lending.fluidtokens.datums import AuthCardanoWithdrawScript
from charli3_dendrite.lending.fluidtokens.datums import CollateralAsset
from charli3_dendrite.lending.fluidtokens.datums import InterestOnRemainingPrincipal
from charli3_dendrite.lending.fluidtokens.datums import NoLiquidationDutchAuctionClaim
from charli3_dendrite.lending.fluidtokens.datums import NoLiquidationFullCollateralClaim
from charli3_dendrite.lending.fluidtokens.datums import PerpetualLoan
from charli3_dendrite.lending.fluidtokens.datums import (
    PrincipalAndInterestOnInstallments,
)
from charli3_dendrite.lending.fluidtokens.datums import TxOutRef
from charli3_dendrite.lending.plutus import StrictPlutusData

__all__ = [
    "Asset",
    "AssetManagerDatumWithHash",
    "AssetManagerDatumWithToken",
    "AuthCardanoMintScript",
    "AuthCardanoSignature",
    "AuthCardanoSpendScript",
    "AuthCardanoWithdrawScript",
    "CollateralAsset",
    "CommonData",
    "ConfigDatum",
    "InterestOnRemainingPrincipal",
    "LenderManagerConfigDatum",
    "LenderManagerDatum",
    "Liquidation",
    "LiquidationMode",
    "LoanDatum",
    "LoanRepaymentData",
    "LockedBorrowerManagerDatum",
    "NoLiquidationDutchAuctionClaim",
    "NoLiquidationFullCollateralClaim",
    "PerpetualLoan",
    "PoolDatum",
    "PoolManagerDatum",
    "PrincipalAndInterestOnInstallments",
    "RequestDatum",
    "TxOutRef",
    "decode_asset_manager_datum",
]


@dataclass
class Liquidation(v3.Liquidation):
    """LiquidationMode variant: liquidate against a loan-to-value threshold.

    V4 appends ``equity_in_principal_currency`` (Plutus ``Bool``: ``Constr1`` is
    ``True``): whether the borrower's equity on liquidation is computed in the principal
    currency rather than the collateral currency.
    """

    CONSTR_ID = 2
    equity_in_principal_currency: Datum


LiquidationMode = Union[
    NoLiquidationFullCollateralClaim,
    NoLiquidationDutchAuctionClaim,
    Liquidation,
]


@dataclass
class CommonData(v3.CommonData):
    """Loan terms shared by pool, request and loan datums.

    V4 types the liquidation mode and appends ``borrower_bond_destination_script_hash``:
    empty sends the borrower bond to the borrower's wallet, otherwise to that script
    with a ``LockedBorrowerManagerDatum``.
    """

    CONSTR_ID = 0
    liquidation_mode: LiquidationMode
    borrower_bond_destination_script_hash: bytes


@dataclass
class PoolDatum(v3.PoolDatum):
    """Deployed V4 PoolDatum: the V3 layout with the V4 ``CommonData``."""

    CONSTR_ID = 0
    common_data: CommonData


@dataclass
class RequestDatum(v3.RequestDatum):
    """Deployed V4 RequestDatum: the V3 layout with the V4 ``CommonData``."""

    CONSTR_ID = 0
    common_data: CommonData


@dataclass
class LoanDatum(v3.LoanDatum):
    """Deployed V4 LoanDatum: the V3 layout with the V4 liquidation mode."""

    CONSTR_ID = 0
    liquidation_mode: LiquidationMode


@dataclass
class ConfigDatum(StrictPlutusData):
    """Protocol config held with the config NFT: every policy id and script hash."""

    CONSTR_ID = 0
    smart_tokens_spend_script_hash: bytes
    admin_credential: Datum  # Credential: Constr0([key hash]) / Constr1([script hash])
    pool_policy_id: bytes
    request_policy_id: bytes
    borrower_bond_policy_id: bytes
    lender_bond_policy_id: bytes
    loan_policy_id: bytes
    repayment_policy_id: bytes
    pool_spend_script_hash: bytes
    request_spend_script_hash: bytes
    loan_spend_script_hash: bytes
    loan_claim_action_script_hash: bytes
    loan_repay_action_script_hash: bytes
    loan_change_collateral_action_script_hash: bytes
    loan_recast_action_script_hash: bytes
    asset_manager_spend_script_hash: bytes
    dutch_auction_spend_script_hash: bytes
    dutch_auction_withdraw_script_hash: bytes
    dutch_auction_starting_increase_per_mille: int
    dutch_auction_lowering_amount: int
    dutch_auction_lowering_frequency: int
    dutch_auction_min_price_to_cancel: int
    pool_cancel_action_script_hash: bytes
    pool_borrow_action_script_hash: bytes
    pool_sell_lender_position_action_script_hash: bytes
    pool_compound_action_script_hash: bytes
    pool_edit_action_script_hash: bytes
    pool_manager_spend_script_hash: bytes
    pool_manager_policy_id: bytes
    locked_borrower_manager_spend_script_hash: bytes


@dataclass
class PoolManagerDatum(StrictPlutusData):
    """Pool-manager UTxO datum: who may edit / cancel the pool, and the bot fee."""

    CONSTR_ID = 0
    pool_owner_auth: Datum  # AuthorizationMethod union
    compounding_fee_per_mille: int


@dataclass
class AssetManagerDatumWithToken(StrictPlutusData):
    """Asset-manager datum whose owner is the holder of ``owner_asset``."""

    CONSTR_ID = 0
    input_output_reference: TxOutRef
    action: bytes
    data: Datum
    owner_asset: Asset


@dataclass
class AssetManagerDatumWithHash(StrictPlutusData):
    """Asset-manager datum whose owner is an authorization method."""

    CONSTR_ID = 1
    input_output_reference: TxOutRef
    action: bytes
    data: Datum
    owner_auth: Datum  # AuthorizationMethod union


@dataclass
class LoanRepaymentData(StrictPlutusData):
    """The ``data`` of an installment repayment's asset-manager datum.

    Records the repaid loan's terms, with ``repaid_installments`` already counting the
    installment this payment settles.
    """

    CONSTR_ID = 0
    loan_id: bytes
    principal_amount: int
    interest_rate: int
    repaid_installments: int
    total_installments: int
    repayment_mode: Datum


def decode_asset_manager_datum(
    datum_cbor: str,
) -> AssetManagerDatumWithToken | AssetManagerDatumWithHash:
    """Decode an asset-manager datum into whichever variant it is."""
    variants: tuple[
        type[AssetManagerDatumWithToken | AssetManagerDatumWithHash],
        ...,
    ] = (AssetManagerDatumWithToken, AssetManagerDatumWithHash)
    for cls in variants:
        try:
            return cls.from_cbor(datum_cbor)
        except DeserializeException:
            continue
    raise DeserializeException("not an asset-manager datum")


@dataclass
class LockedBorrowerManagerDatum(StrictPlutusData):
    """Datum of a borrower bond locked at a borrower-bond destination script."""

    CONSTR_ID = 0
    origin_ref: TxOutRef
    borrower_auth: Datum  # AuthorizationMethod union


@dataclass
class LenderManagerDatum(StrictPlutusData):
    """Lender-manager UTxO datum: lender bonds held for automated claims.

    ``lender_stake_credential`` is ``Option<StakeCredential>``;
    ``should_liquidation_convert_to_principal`` is a Plutus ``Bool``; ``pool_id`` is the
    pool NFT asset name compounding targets (empty disables compounding).
    """

    CONSTR_ID = 0
    lender_auth: Datum  # AuthorizationMethod union
    lender_stake_credential: Datum
    should_liquidation_convert_to_principal: Datum
    liquidation_fee_per_mille: int
    pool_id: bytes
    principal_asset: Asset


@dataclass
class LenderManagerConfigDatum(StrictPlutusData):
    """Lender-manager config held with its own config NFT: the action script hashes."""

    CONSTR_ID = 0
    admin_credential: Datum
    withdraw_bonds_action_script_hash: bytes
    liquidate_action_script_hash: bytes
    compound_action_script_hash: bytes
    liquidate_and_pay_in_advance_action_script_hash: bytes
    liquidate_and_convert_action_script_hash: bytes
    liquidate_pay_in_advance_and_compound_action_script_hash: bytes
    liquidate_convert_and_compound_action_script_hash: bytes
