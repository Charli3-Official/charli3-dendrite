"""FluidTokens V4 states built from raw UTxO records (offline)."""

from decimal import Decimal

import cbor2
import pytest
from pycardano import Address
from pycardano import RawPlutusData
from pycardano.exception import DeserializeException

from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.lending.fluidtokens.math import perpetual_outstanding_debt
from charli3_dendrite.lending.fluidtokens_v4 import constants as c
from charli3_dendrite.lending.fluidtokens_v4.datums import AuthCardanoSignature
from charli3_dendrite.lending.fluidtokens_v4.datums import LockedBorrowerManagerDatum
from charli3_dendrite.lending.fluidtokens_v4.datums import TxOutRef
from charli3_dendrite.lending.fluidtokens_v4.state import AuthMethod
from charli3_dendrite.lending.fluidtokens_v4.state import FluidV4AssetManagerState
from charli3_dendrite.lending.fluidtokens_v4.state import FluidV4LenderManagerState
from charli3_dendrite.lending.fluidtokens_v4.state import FluidV4LoanState
from charli3_dendrite.lending.fluidtokens_v4.state import FluidV4LockedBorrowerState
from charli3_dendrite.lending.fluidtokens_v4.state import FluidV4PoolManagerState
from charli3_dendrite.lending.fluidtokens_v4.state import FluidV4PoolState
from charli3_dendrite.lending.fluidtokens_v4.state import FluidV4RequestState
from charli3_dendrite.lending.fluidtokens_v4.state import identity_name
from charli3_dendrite.lending.oracles.models import OraclePrice
from charli3_dendrite.lending.oracles.models import OracleSource
from charli3_dendrite.lending.oracles.models import PriceMap
from charli3_dendrite.lending.units import constr
from tests.lending.fluidtokens_v4.records import FIX
from tests.lending.fluidtokens_v4.records import record_info
from tests.lending.fluidtokens_v4.records import v4_request_record

_HOUR_MS = 3_600_000


def _pools():
    return [FluidV4PoolState.from_record(record_info(r)) for r in FIX["pool"]]


def _loan(index=0, now_ms=None):
    loan = FluidV4LoanState.from_record(record_info(FIX["loan"][index]))
    if now_ms is not None:
        loan.set_time(now_ms)
    return loan


def test_pool_identity_and_terms():
    for pool in _pools():
        assert len(pool.pool_id) == 58
        assert pool.out_ref == f"{pool.tx_hash}#{pool.tx_index}"
        assert pool.protocol() == "FluidTokensV4"
        assert pool.market.principal_unit == pool.borrowable_unit
        assert pool.market.liquidation_ltv is not None
        assert pool.is_permissioned is False


def test_pool_manager_shares_its_pools_asset_name():
    pool_ids = {pool.pool_id for pool in _pools()}
    managers = [
        FluidV4PoolManagerState.from_record(record_info(r)) for r in FIX["pool_manager"]
    ]
    assert {m.pool_id for m in managers} == pool_ids
    for manager in managers:
        assert manager.owner_auth.kind == "signature"
        assert len(manager.owner_auth.hash_hex) == 56
        assert manager.compounding_fee_per_mille >= 0


def test_loan_links_to_its_pool_through_origin_id():
    pool_ids = {pool.pool_id for pool in _pools()}
    loans = [FluidV4LoanState.from_record(record_info(r)) for r in FIX["loan"]]
    assert all(loan.origin[0] == "pool" for loan in loans)
    assert all(len(loan.loan_id) == 56 for loan in loans)
    # Most captured loans point at a captured pool; the rest point at pools the
    # fixture does not hold.
    assert sum(loan.pool_id in pool_ids for loan in loans) > 0


def test_current_debt_requires_an_evaluation_time():
    with pytest.raises(ValueError, match="set_time"):
        _loan().current_debt()


def test_current_debt_matches_the_perpetual_formula():
    loan = _loan()
    datum = loan.loan_datum
    now_ms = datum.lend_date + 720 * _HOUR_MS
    loan.set_time(now_ms)
    mode_alt, mode_fields = constr(datum.repayment_mode)
    assert mode_alt == 2  # PerpetualLoan, the only mode in the captured fixture
    assert loan.current_debt() == perpetual_outstanding_debt(
        principal=datum.principal_amount,
        interest_rate=datum.interest_rate,
        apy_coef=int(mode_fields[0]),
        lend_date_ms=datum.lend_date,
        now_ms=now_ms,
        repaid_installments=datum.repaid_installments,
        installment_period=datum.installment_period,
        initial_grace_period=datum.initial_grace_period,
    )
    assert loan.current_debt() > datum.principal_amount


def test_health_factor_without_a_pool():
    # Loan terms live in the loan datum, so an orphaned loan still evaluates.
    loan = _loan()
    loan.set_time(loan.loan_datum.lend_date + _HOUR_MS)
    (ref,) = loan.oracle_refs()
    assert ref.quote == loan.borrowed_unit
    prices = PriceMap()
    prices.add(
        OraclePrice(
            token=ref.token,
            quote=ref.quote,
            num=1,
            denom=10**9,
            source=OracleSource.FLUID_AGGREGATED,
        ),
    )
    assert loan.health_factor(prices) < Decimal(1)
    assert loan.is_liquidatable(prices) is True
    assert loan.is_liquidatable(PriceMap()) is False


def test_request_view():
    request = FluidV4RequestState.from_record(record_info(v4_request_record()))
    assert request.request_id == "cd" * 28
    assert request.out_ref == "ab" * 32 + "#0"
    assert request.principal_unit == "lovelace"
    assert request.collateral_unit
    assert (
        request.request_datum.common_data.borrower_bond_destination_script_hash == b""
    )


def test_selectors_on_the_state_classes():
    (pool_address,) = FluidV4PoolState.pool_selector().addresses
    (loan_address,) = FluidV4LoanState.loan_selector().addresses
    assert Address.decode(pool_address).payment_part.payload.hex() == c.POOL_SPEND_SKH
    assert Address.decode(loan_address).payment_part.payload.hex() == c.LOAN_SPEND_SKH


def test_asset_manager_view():
    for rec in FIX["asset_manager"]:
        manager = FluidV4AssetManagerState.from_record(record_info(rec))
        assert manager.action
        assert "#" in manager.input_out_ref
        assert (manager.owner_unit is None) != (manager.owner_auth is None)
    first = FluidV4AssetManagerState.from_record(record_info(FIX["asset_manager"][0]))
    assert first.action == b"installment_repayment"
    assert first.owner_unit.startswith(c.LENDER_BOND_POLICY)


def test_lender_manager_holds_bonds_named_after_loans():
    loan_ids = {
        FluidV4LoanState.from_record(record_info(r)).loan_id for r in FIX["loan"]
    }
    managers = [
        FluidV4LenderManagerState.from_record(record_info(r))
        for r in FIX["lender_manager"]
    ]
    bonds = [name for m in managers for name in m.lender_bond_names]
    assert bonds
    # A lender may also hold a bond in a wallet; the captured ones are all managed.
    assert loan_ids & set(bonds)
    for manager in managers:
        assert manager.lender_auth.kind == "signature"
        assert manager.principal_unit
        assert manager.liquidation_fee_per_mille >= 0


def test_locked_borrower_view():
    datum = LockedBorrowerManagerDatum(
        origin_ref=TxOutRef(tx_id=bytes.fromhex("aa" * 32), index=1),
        borrower_auth=AuthCardanoSignature(key_hash=bytes.fromhex("bb" * 28)),
    )
    bond = c.BORROWER_BOND_POLICY + "cc" * 28
    state = FluidV4LockedBorrowerState.from_record(
        record_info(
            FIX["pool"][0],
            address=FIX["pool"][0]["address"],
            datum_cbor=datum.to_cbor_hex(),
            assets=Assets(root={"lovelace": 2_000_000, bond: 1}),
        ),
    )
    assert state.origin_out_ref == "aa" * 32 + "#1"
    assert state.borrower_auth == AuthMethod(kind="signature", hash_hex="bb" * 28)
    assert state.borrower_bond_names == ["cc" * 28]


def test_from_record_raises_on_a_foreign_datum():
    with pytest.raises(DeserializeException):
        FluidV4PoolManagerState.from_record(record_info(FIX["pool"][0]))


@pytest.mark.parametrize(
    ("root", "expected"),
    [
        ({"lovelace": 1, c.POOL_POLICY + "ab": 1}, "ab"),
        ({"lovelace": 1}, None),
        ({"lovelace": 1, c.POOL_POLICY + "ab": 2}, None),
        ({"lovelace": 1, c.POOL_POLICY + "ab": 1, c.POOL_POLICY + "cd": 1}, None),
    ],
)
def test_identity_name(root, expected):
    assert identity_name(Assets(root=root), c.POOL_POLICY) == expected


def test_equity_flag_is_exposed_on_the_typed_liquidation():
    liquidation = _loan().loan_datum.liquidation_mode
    assert liquidation.equity_in_principal_currency in (
        RawPlutusData(cbor2.CBORTag(121, [])),
        RawPlutusData(cbor2.CBORTag(122, [])),
    )


STATE_CLASSES = [
    FluidV4PoolState,
    FluidV4LoanState,
    FluidV4RequestState,
    FluidV4PoolManagerState,
    FluidV4AssetManagerState,
    FluidV4LenderManagerState,
    FluidV4LockedBorrowerState,
]


@pytest.mark.parametrize("cls", STATE_CLASSES)
def test_derived_views_are_properties(cls):
    # from_record reads each view with getattr; a method listed here would never run.
    assert cls.DERIVED_VIEWS
    for view in cls.DERIVED_VIEWS:
        assert isinstance(getattr(cls, view, None), property), view


def _replace(node, path, value):
    """``node`` with the value at ``path`` (constructor / list indexes) replaced."""
    if not path:
        return value
    items = list(node.value if isinstance(node, cbor2.CBORTag) else node)
    items[path[0]] = _replace(items[path[0]], path[1:], value)
    return cbor2.CBORTag(node.tag, items) if isinstance(node, cbor2.CBORTag) else items


def _mutated(rec, path, value):
    top = cbor2.loads(bytes.fromhex(rec["datum_cbor"]))
    return {**rec, "datum_cbor": cbor2.dumps(_replace(top, path, value)).hex()}


# (state class, record, path to an untyped datum field, malformed value)
MALFORMED_UNTYPED_FIELDS = [
    (
        "loan repayment without fields",
        FluidV4LoanState,
        "loan",
        (11,),
        cbor2.CBORTag(123, []),
    ),
    (
        "loan repayment non-int",
        FluidV4LoanState,
        "loan",
        (11,),
        cbor2.CBORTag(123, [b"x", 5]),
    ),
    (
        "loan collateral name missing",
        FluidV4LoanState,
        "loan",
        (16, 1),
        cbor2.CBORTag(121, []),
    ),
    (
        "pool repayment unknown alt",
        FluidV4PoolState,
        "pool",
        (2, 7),
        cbor2.CBORTag(126, []),
    ),
    (
        "pool collateral name not bytes",
        FluidV4PoolState,
        "pool",
        (6, 0, 1),
        cbor2.CBORTag(121, [5]),
    ),
    (
        "request repayment without fields",
        FluidV4RequestState,
        "request",
        (2, 7),
        cbor2.CBORTag(123, []),
    ),
    (
        "pool manager auth not bytes",
        FluidV4PoolManagerState,
        "pool_manager",
        (0,),
        cbor2.CBORTag(121, [7]),
    ),
    (
        "lender manager auth unknown alt",
        FluidV4LenderManagerState,
        "lender_manager",
        (0,),
        cbor2.CBORTag(126, [b"x"]),
    ),
]


@pytest.mark.parametrize(
    ("cls", "kind", "path", "value"),
    [case[1:] for case in MALFORMED_UNTYPED_FIELDS],
    ids=[case[0] for case in MALFORMED_UNTYPED_FIELDS],
)
def test_from_record_rejects_a_malformed_untyped_field(cls, kind, path, value):
    rec = v4_request_record() if kind == "request" else FIX[kind][0]
    with pytest.raises(ValueError):
        cls.from_record(record_info(_mutated(rec, path, value)))
