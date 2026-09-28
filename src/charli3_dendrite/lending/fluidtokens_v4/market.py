"""FluidTokens V4 states as common lending views.

A V4 pool is one market, a loan one position and a request one borrow request. The
conversions read only the UTxO (and, for a pool's lender, its pool manager); every
number comes from the datum and the contract-exact maths.
"""

from __future__ import annotations

from fractions import Fraction
from typing import TYPE_CHECKING
from typing import Any

from pycardano import RawCBOR

from charli3_dendrite.lending.fluidtokens.transactions.borrow_terms import (
    min_output_lovelace,
)
from charli3_dendrite.lending.fluidtokens_v4 import constants as c
from charli3_dendrite.lending.fluidtokens_v4.rates import PROTOCOL_NAME
from charli3_dendrite.lending.fluidtokens_v4.rates import FluidAmortizedRate
from charli3_dendrite.lending.fluidtokens_v4.rates import FluidFlatTermRate
from charli3_dendrite.lending.fluidtokens_v4.rates import FluidPerpetualRate
from charli3_dendrite.lending.fluidtokens_v4.state import PERMISSIONLESS_MARKER
from charli3_dendrite.lending.fluidtokens_v4.state import AuthMethod
from charli3_dendrite.lending.fluidtokens_v4.state import FluidV4LoanState
from charli3_dendrite.lending.fluidtokens_v4.state import FluidV4PoolState
from charli3_dendrite.lending.fluidtokens_v4.state import FluidV4RequestState
from charli3_dendrite.lending.fluidtokens_v4.state import auth_method
from charli3_dendrite.lending.fluidtokens_v4.state import collateral_asset_unit
from charli3_dendrite.lending.fluidtokens_v4.state import decode_repayment_mode
from charli3_dendrite.lending.fluidtokens_v4.state import names_under
from charli3_dendrite.lending.normalized import BorrowRequest
from charli3_dendrite.lending.normalized import CollateralHolding
from charli3_dendrite.lending.normalized import CollateralTerms
from charli3_dendrite.lending.normalized import DefaultRule
from charli3_dendrite.lending.normalized import LendingMarket
from charli3_dendrite.lending.normalized import LendingParseError
from charli3_dendrite.lending.normalized import LendingPosition
from charli3_dendrite.lending.normalized import Origin
from charli3_dendrite.lending.normalized import Party
from charli3_dendrite.lending.normalized import PartyKind
from charli3_dendrite.lending.oracles.models import OracleRef
from charli3_dendrite.lending.oracles.models import OracleSource
from charli3_dendrite.lending.units import constr

if TYPE_CHECKING:
    from charli3_dendrite.dataclasses.models import Assets
    from charli3_dendrite.lending.fluidtokens_v4.datums import CollateralAsset
    from charli3_dendrite.lending.fluidtokens_v4.state import FluidV4PoolManagerState
    from charli3_dendrite.lending.rates import RateModel

# Constructor alternatives of RepaymentMode, LiquidationMode and Option.
_AMORTIZED, _FLAT_TERM, _PERPETUAL = 0, 1, 2
_FULL_CLAIM, _DUTCH_AUCTION, _LIQUIDATION = 0, 1, 2
_OPTION_NONE = 1

# The oracle-token placeholder of a collateral with no price feed.
_NO_ORACLE = b"NONE"

_PER_MILLE = 1000

# Errors a malformed datum field raises while it is converted.
_CONVERSION_ERRORS = (ValueError, TypeError, IndexError, AttributeError)


def party_of(auth: AuthMethod) -> Party:
    """A V4 ``AuthorizationMethod`` as a party; the auth kind is kept as ``detail``."""
    kind = PartyKind.KEY if auth.kind == "signature" else PartyKind.SCRIPT
    return Party(kind=kind, identifier=auth.hash_hex, detail=auth.kind)


def rate_model_of(terms: Any) -> RateModel:  # noqa: ANN401 - CommonData or LoanDatum
    """The rate model of a V4 ``CommonData`` or ``LoanDatum``'s loan terms."""
    alt, fields = decode_repayment_mode(terms.repayment_mode)
    shared = {
        "interest_rate": int(terms.interest_rate),
        "total_installments": int(terms.total_installments),
        "installment_period": int(terms.installment_period),
        "initial_grace_period": int(terms.initial_grace_period),
        "repayment_time_window": int(terms.repayment_time_window),
        "penalty_fee_for_late_repayment": int(terms.penalty_fee_for_late_repayment),
    }
    if alt == _PERPETUAL:
        return FluidPerpetualRate(**shared, apy_coef=int(fields[0]))
    if alt == _FLAT_TERM:
        return FluidFlatTermRate(**shared)
    return FluidAmortizedRate(**shared)


class _Liquidation:
    """What a V4 ``LiquidationMode`` means for the common views."""

    def __init__(self, mode: Any) -> None:  # noqa: ANN401 - a LiquidationMode
        """Decode ``mode``; raises LendingParseError on an unknown or bad variant."""
        alt, fields = constr(mode)
        self.ltv: Fraction | None = None
        self.penalty: Fraction | None = None
        self.equity_to_borrower = False
        if alt == _LIQUIDATION:
            l_tv, divider, penalty = int(fields[0]), int(fields[1]), int(fields[2])
            if l_tv < 0 or divider <= 0:
                raise LendingParseError(f"invalid liquidation LTV {l_tv}/{divider}")
            self.rule = DefaultRule.PRICE_LIQUIDATION
            self.ltv = Fraction(l_tv, divider)
            # A negative penalty means the borrower forfeits all collateral.
            self.equity_to_borrower = penalty >= 0
            self.penalty = Fraction(penalty, _PER_MILLE) if penalty >= 0 else None
        elif alt == _FULL_CLAIM:
            self.rule = DefaultRule.FULL_COLLATERAL_CLAIM
        elif alt == _DUTCH_AUCTION:
            self.rule = DefaultRule.DUTCH_AUCTION
        else:
            raise LendingParseError(f"unknown liquidation mode {alt}")


def _ratio(numerator: int, denominator: int, what: str) -> Fraction:
    """A positive datum ratio; the contract cannot use a zero or negative one."""
    if numerator <= 0 or denominator <= 0:
        raise LendingParseError(f"invalid {what} {numerator}/{denominator}")
    return Fraction(numerator, denominator)


def _is_policy_wide(collateral: CollateralAsset) -> bool:
    return constr(collateral.maybe_asset_name)[0] == _OPTION_NONE


def _price_source(
    collateral: CollateralAsset,
    unit: str,
    quote: str,
    *,
    dynamic: bool,
) -> OracleRef | None:
    """The feed pricing ``collateral`` in ``quote``; None when not oracle-priced."""
    feed = collateral.oracle_token_asset
    if not dynamic or feed.policy_id == _NO_ORACLE:
        return None
    return OracleRef(
        source=OracleSource.FLUID_AGGREGATED,
        token=unit,
        quote=quote,
        feed_policy=feed.policy_id.hex(),
        feed_name=feed.asset_name.hex(),
    )


def _collateral_terms(
    collateral: CollateralAsset,
    ratio: Fraction,
    liquidation: _Liquidation,
    quote: str,
    *,
    dynamic: bool,
) -> CollateralTerms:
    unit = collateral_asset_unit(collateral)
    return CollateralTerms(
        unit=unit,
        policy_wide=_is_policy_wide(collateral),
        max_ltv=ratio if dynamic else None,
        fixed_ratio=None if dynamic else ratio,
        liquidation_ltv=liquidation.ltv,
        liquidation_penalty=liquidation.penalty,
        liquidation_discount=None,
        equity_to_borrower=liquidation.equity_to_borrower,
        price_source=_price_source(collateral, unit, quote, dynamic=dynamic),
    )


def _holding(
    assets: Assets,
    collateral: CollateralAsset,
) -> list[CollateralHolding]:
    """The collateral a UTxO holds: one unit, or each token of a policy-wide option."""
    unit = collateral_asset_unit(collateral)
    if not _is_policy_wide(collateral):
        return [CollateralHolding(unit, assets[unit])]
    return [
        CollateralHolding(unit + name, assets[unit + name])
        for name in names_under(assets, unit)
    ]


def lendable_principal(pool: FluidV4PoolState) -> int:
    """How much principal ``pool`` can lend now.

    A borrow must leave the pool output holding exactly its input less the principal,
    so an ADA pool keeps the continuing output's min-ADA: the same floor the borrow
    builder enforces. A token pool lends its whole token quantity.
    """
    unit = pool.borrowable_unit
    if unit != "lovelace":
        return pool.assets[unit]
    others = {u: q for u, q in pool.assets.items() if u != "lovelace" and q}
    floor = min_output_lovelace(
        address=pool.address,
        assets=others,
        datum=RawCBOR(bytes.fromhex(pool.datum_cbor)),
        slot=0,
    )
    return max(pool.assets["lovelace"] - floor, 0)


def _converted(what: str, exc: Exception) -> LendingParseError:
    if isinstance(exc, LendingParseError):
        return exc
    return LendingParseError(f"{what}: {exc}")


def pool_to_market(
    pool: FluidV4PoolState,
    pool_manager: FluidV4PoolManagerState | None = None,
) -> LendingMarket:
    """``pool`` as a market; its lender is the pool manager's owner when given."""
    if pool_manager is not None and pool_manager.pool_id != pool.pool_id:
        raise ValueError(
            f"pool manager {pool_manager.out_ref} does not manage pool {pool.out_ref}",
        )
    try:
        datum = pool.pool_datum
        common = datum.common_data  # type: ignore[attr-defined]
        quote = common.principal_asset.unit()
        liquidation = _Liquidation(common.liquidation_mode)
        dynamic = pool.market.is_dynamic
        collateral = tuple(
            _collateral_terms(
                option,
                _ratio(int(minimum), int(divider), "collateral ratio"),
                liquidation,
                quote,
                dynamic=dynamic,
            )
            for option, minimum, divider in zip(
                datum.collateral_options,  # type: ignore[attr-defined]
                datum.min_collateral,  # type: ignore[attr-defined]
                datum.min_collateral_divider,  # type: ignore[attr-defined]
                strict=True,
            )
        )
        owner = (
            pool_manager.owner_auth
            if pool_manager is not None
            else auth_method(datum.lender_auth)  # type: ignore[attr-defined]
        )
        return LendingMarket(
            protocol=PROTOCOL_NAME,
            market_id=c.POOL_POLICY + pool.pool_id,
            borrow_unit=quote,
            lender=party_of(owner),
            available_liquidity=lendable_principal(pool),
            rate_model=rate_model_of(common),
            supply_rate=None,
            receipt_unit=None,
            utilization=None,
            collateral=collateral,
            on_default=liquidation.rule,
            permissioned=pool.is_permissioned,
        )
    except _CONVERSION_ERRORS as exc:
        raise _converted(f"pool {pool.out_ref}", exc) from exc


def _origin(loan: FluidV4LoanState) -> Origin | None:
    kind, name = loan.origin
    if kind == "pool":
        return Origin("market", c.POOL_POLICY + name)
    if kind == "request":
        return Origin("request", c.REQUEST_POLICY + name)
    return None


def loan_to_position(loan: FluidV4LoanState) -> LendingPosition:
    """``loan`` as a position; its bonds' holders are the borrower and the lender."""
    try:
        datum = loan.loan_datum
        loan_id = loan.loan_id
        liquidation = _Liquidation(datum.liquidation_mode)  # type: ignore[attr-defined]
        return LendingPosition(
            protocol=PROTOCOL_NAME,
            position_id=c.LOAN_POLICY + loan_id,
            origin=_origin(loan),
            borrower=Party(PartyKind.TOKEN, c.BORROWER_BOND_POLICY + loan_id),
            lender=Party(PartyKind.TOKEN, c.LENDER_BOND_POLICY + loan_id),
            borrow_unit=loan.borrowed_unit,
            principal=int(datum.principal_amount),  # type: ignore[attr-defined]
            installments_paid=int(datum.repaid_installments),  # type: ignore[attr-defined]
            collateral=tuple(
                _holding(loan.assets, datum.collateral),  # type: ignore[attr-defined]
            ),
            rate_model=rate_model_of(datum),
            liquidation_ltv=liquidation.ltv,
            on_default=liquidation.rule,
            opened_ms=int(datum.lend_date),  # type: ignore[attr-defined]
        )
    except _CONVERSION_ERRORS as exc:
        raise _converted(f"loan {loan.out_ref}", exc) from exc


def request_to_borrow_request(request: FluidV4RequestState) -> BorrowRequest:
    """``request`` as a borrow request."""
    try:
        datum = request.request_datum
        common = datum.common_data
        quote = common.principal_asset.unit()
        liquidation = _Liquidation(common.liquidation_mode)
        held = _holding(request.assets, datum.collateral)
        holding = CollateralHolding(
            collateral_asset_unit(datum.collateral),
            sum(h.amount for h in held),
        )
        return BorrowRequest(
            protocol=PROTOCOL_NAME,
            request_id=c.REQUEST_POLICY + request.request_id,
            borrower=party_of(auth_method(datum.borrower_auth)),
            borrow_unit=quote,
            collateral=holding,
            principal_limit=_collateral_terms(
                datum.collateral,
                _ratio(
                    int(datum.min_principal),
                    int(datum.min_principal_divider),
                    "principal ratio",
                ),
                liquidation,
                quote,
                dynamic=request.is_dynamic,
            ),
            principal_max=int(datum.max_principal),
            rate_model=rate_model_of(common),
            on_default=liquidation.rule,
            expires_ms=int(datum.request_expiration),
            expiry_penalty=int(datum.request_expiration_penalty),
            permissioned=datum.permissioned_condition_script_hash
            != PERMISSIONLESS_MARKER,
        )
    except _CONVERSION_ERRORS as exc:
        raise _converted(f"request {request.out_ref}", exc) from exc
