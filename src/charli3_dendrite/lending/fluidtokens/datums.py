"""pycardano mirrors of the deployed FluidTokens V3 datums (mainnet)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import List
from typing import TypeAlias

from pycardano import Datum
from pycardano import IndefiniteList
from pycardano import PlutusData

from charli3_dendrite.lending.units import asset_unit


@dataclass
class Asset(PlutusData):
    """(policy_id, asset_name) as a `Constr`; ADA is (b"", b"")."""

    CONSTR_ID = 0
    policy_id: bytes
    asset_name: bytes

    def unit(self) -> str:
        """Dendrite unit string ('lovelace' for ADA)."""
        return asset_unit(self.policy_id, self.asset_name)


@dataclass
class CollateralAsset(PlutusData):
    """A collateral type: policy, optional asset name, and its oracle token."""

    CONSTR_ID = 0
    policy_id: bytes
    maybe_asset_name: Datum  # Option<AssetName>: Constr0([name]) / Constr1([])
    oracle_token_asset: Asset


@dataclass
class NoLiquidationFullCollateralClaim(PlutusData):
    """LiquidationMode variant: on default the lender claims all collateral."""

    CONSTR_ID = 0


@dataclass
class NoLiquidationDutchAuctionClaim(PlutusData):
    """LiquidationMode variant: on default the collateral is dutch-auctioned."""

    CONSTR_ID = 1


@dataclass
class Liquidation(PlutusData):
    """LiquidationMode variant: liquidate against a loan-to-value threshold."""

    CONSTR_ID = 2
    l_tv: int
    l_tv_divider: int
    partial_liquidation_penalty_per_mille: int
    equity_in_principal_currency: bool


@dataclass
class InterestOnRemainingPrincipal(PlutusData):
    """RepaymentMode variant: interest accrues on the remaining principal."""

    CONSTR_ID = 0
    max_possible_recasts: int


@dataclass
class PrincipalAndInterestOnInstallments(PlutusData):
    """RepaymentMode variant: principal and interest split across installments."""

    CONSTR_ID = 1


@dataclass
class PerpetualLoan(PlutusData):
    """RepaymentMode variant: perpetual loan with a linear APY increase."""

    CONSTR_ID = 2
    apy_increase_linear_coefficient: int
    max_possible_recasts: int


# Unions; parse via from_primitive dispatch where needed.
LiquidationMode: TypeAlias = Datum
RepaymentMode: TypeAlias = Datum


@dataclass
class CommonData(PlutusData):
    """Loan terms shared by pool, request, and loan datums."""

    CONSTR_ID = 0
    principal_asset: Asset
    principal_oracle_asset: Asset
    interest_rate: int
    installment_period: int
    total_installments: int
    initial_grace_period: int
    liquidation_mode: LiquidationMode
    repayment_mode: RepaymentMode
    repayment_time_window: int
    penalty_fee_for_late_repayment: int
    repayment_receipts: Datum  # Bool: Constr0/Constr1


@dataclass
class AuthCardanoSignature(PlutusData):
    """AuthorizationMethod variant: authorize by a verification-key hash."""

    CONSTR_ID = 0
    key_hash: bytes


@dataclass
class AuthCardanoSpendScript(PlutusData):
    """AuthorizationMethod variant: authorize by a spending script hash."""

    CONSTR_ID = 1
    script_hash: bytes


@dataclass
class AuthCardanoWithdrawScript(PlutusData):
    """AuthorizationMethod variant: authorize by a withdrawal script hash."""

    CONSTR_ID = 2
    script_hash: bytes


@dataclass
class AuthCardanoMintScript(PlutusData):
    """AuthorizationMethod variant: authorize by a minting script hash."""

    CONSTR_ID = 3
    script_hash: bytes


@dataclass
class PoolDatum(PlutusData):
    """Deployed PoolDatum — Constr0, 10 fields.

    ``collateral_options``, ``min_collateral``, and ``min_collateral_divider`` follow
    Plutus list serialization: empty is a definite-length empty array (``80``);
    non-empty is an indefinite-length array (``9f..ff``). A plain ``list`` field
    reproduces the empty case but emits a DEFINITE array when non-empty, diverging
    from on-chain, so ``__post_init__`` rebuilds each non-empty list as an
    ``IndefiniteList`` (coercing decoded raw ``collateral_options`` entries back into
    ``CollateralAsset``).
    """

    CONSTR_ID = 0
    permissioned_condition_script_hash: bytes
    extra_data: Datum
    common_data: CommonData
    lender_auth: Datum  # AuthorizationMethod union
    lender_bond_address: Datum  # pycardano Address constr
    lender_bond_inline_datum_hash: bytes
    collateral_options: List[CollateralAsset] | IndefiniteList
    min_collateral: List[int] | IndefiniteList
    min_collateral_divider: List[int] | IndefiniteList
    dynamic_collateral_price: Datum  # Bool Constr0/Constr1

    def __post_init__(self) -> None:
        """Pin the list fields to their conditional on-chain encoding."""
        options = [
            o if isinstance(o, CollateralAsset) else CollateralAsset.from_primitive(o)
            for o in self.collateral_options
        ]
        self.collateral_options = IndefiniteList(options) if options else options
        self.min_collateral = (
            IndefiniteList(list(self.min_collateral))
            if len(self.min_collateral)
            else self.min_collateral
        )
        self.min_collateral_divider = (
            IndefiniteList(list(self.min_collateral_divider))
            if len(self.min_collateral_divider)
            else self.min_collateral_divider
        )


@dataclass
class LoanDatum(PlutusData):
    """Deployed LoanDatum — Constr0, 17 fields."""

    CONSTR_ID = 0
    done_recasts: int
    principal_amount: int
    lend_date: int
    repaid_installments: int
    interest_rate: int
    total_installments: int
    principal_asset: Asset
    principal_oracle_asset: Asset
    installment_period: int
    initial_grace_period: int
    liquidation_mode: LiquidationMode
    repayment_mode: RepaymentMode
    repayment_time_window: int
    penalty_fee_for_late_repayment: int
    repayment_receipts: Datum
    origin_id: bytes
    collateral: CollateralAsset


@dataclass
class RequestDatum(PlutusData):
    """Deployed RequestDatum — Constr0, 12 fields."""

    CONSTR_ID = 0
    permissioned_condition_script_hash: bytes
    extra_data: Datum
    common_data: CommonData
    borrower_auth: Datum
    borrower_address: Datum
    collateral: CollateralAsset
    min_principal: int
    min_principal_divider: int
    max_principal: int
    dynamic_collateral_price: Datum
    request_expiration: int
    request_expiration_penalty: int
