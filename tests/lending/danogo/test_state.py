from decimal import Decimal

import pytest
from pycardano import Address
from pycardano import IndefiniteList

from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.lending.danogo.datums import LoanDatum
from charli3_dendrite.lending.danogo.datums import OwnerNft
from charli3_dendrite.lending.danogo.datums import PoolDatum
from charli3_dendrite.lending.danogo.datums import PRational
from charli3_dendrite.lending.danogo.market import DanogoMarket
from charli3_dendrite.lending.danogo.state import DanogoLoanState
from charli3_dendrite.lending.danogo.state import DanogoPoolState
from charli3_dendrite.lending.oracles.models import OraclePrice
from charli3_dendrite.lending.oracles.models import OracleSource
from charli3_dendrite.lending.oracles.models import PriceMap

POOL_ADDR = "addr1zxn9efv2f6w82hagxqtn62ju4m293tqvw0uhmdl64ch8uw6j2c79gy9l76sdg0xwhd7r0c0kna0tycz4y5s6mlenh8pq6s3z70"

COLL = "aa" + "0" * 54  # one accepted collateral unit (matches _market())


def _market():
    return DanogoMarket(
        supply_token="lovelace",
        collaterals={"aa" + "0" * 54: 8000},
        alt_supply_tokens={},
        base_rate=400,
        power_base=10_470,
        util_cap=8500,
        loan_fee_rate=2000,
        loan_origination_fee_rate=0,
        min_tx_amount=1_000_000,
    )


def _bare_pool_state():
    datum = PoolDatum(
        total_supply=1_000_000,
        circulating_dtoken=900_000,
        total_borrow=850_000,
        borrow_apy=900,
        undistributed_fee=0,
        interest_index=1_000_000_000_000,
        interest_time=0,
        alt_supply_tokens_rate=[PRational(num=0, denom=1)],
    )
    return DanogoPoolState(
        address=POOL_ADDR,
        assets=Assets(root={"lovelace": 1_000_000}),
        block_time=1,
        block_index=1,
        plutus_v2=True,
        datum_cbor=datum.to_cbor_hex(),
        datum_hash="00",
        tx_index=0,
        tx_hash="ab",
    )


def _pool_state():
    p = _bare_pool_state()
    p.attach_market(_market())
    return p


def test_pool_protocol_and_units():
    p = _pool_state()
    assert p.protocol() == "Danogo"
    assert p.borrowable_unit == "lovelace"


def test_pool_utilization_ratio():
    p = _pool_state()
    assert p.utilization_ratio == Decimal("0.85")


def test_pool_id_and_stake_address():
    p = _pool_state()
    assert p.pool_id == f"{POOL_ADDR}:lovelace"
    assert p.stake_address == Address.decode(POOL_ADDR)


def test_pool_max_borrow_for_collateral_value():
    p = _pool_state()
    assert p.max_borrow_for_collateral_value(1_000_000) == 800_000


def test_pool_max_borrow_zero_when_no_collaterals():
    p = _bare_pool_state()
    p.attach_market(
        DanogoMarket(
            supply_token="lovelace",
            collaterals={},
            alt_supply_tokens={},
            base_rate=400,
            power_base=10_470,
            util_cap=8500,
            loan_fee_rate=2000,
            loan_origination_fee_rate=0,
            min_tx_amount=1_000_000,
        )
    )
    assert p.max_borrow_for_collateral_value(1_000_000) == 0


def test_pool_market_not_attached_raises():
    p = _bare_pool_state()
    with pytest.raises(ValueError):
        _ = p.market


def _loan_state(now_ms: int = 31_536_000_000):
    ld = LoanDatum(
        owner_nft=OwnerNft(asset=IndefiniteList([bytes.fromhex("cc" * 28), b"own"])),
        token=IndefiniteList([b"", b""]),
        loan_amount=1_000_000,
        initial_interest_index=1_000_000_000_000,
    )
    loan = DanogoLoanState(
        address=POOL_ADDR,
        assets=Assets(root={"lovelace": 2_000_000, COLL: 2_000_000}),
        block_time=1,
        block_index=1,
        plutus_v2=True,
        datum_cbor=ld.to_cbor_hex(),
        datum_hash="00",
        tx_index=0,
        tx_hash="cd",
    )
    loan.attach_context(pool=_pool_state(), market=_market(), now_ms=now_ms)
    return loan


def _prices(num: int, denom: int) -> PriceMap:
    pm = PriceMap()
    pm.add(
        OraclePrice(
            token=COLL,
            quote="lovelace",
            num=num,
            denom=denom,
            source=OracleSource.DANOGO_AGGREGATOR,
        )
    )
    return pm


def test_loan_debt_accrues_one_year_at_9pct():
    loan = _loan_state()  # pool borrow_apy=900 (9%), 1 year elapsed
    assert loan.current_debt() == 1_090_000


def test_loan_healthy_when_collateral_strong():
    loan = _loan_state()
    assert loan.health_factor(_prices(1, 1)) > 1
    assert loan.is_liquidatable(_prices(1, 1)) is False


def test_loan_liquidatable_when_price_drops():
    loan = _loan_state()
    assert loan.is_liquidatable(_prices(1, 2)) is True


def test_loan_not_liquidatable_when_price_missing():
    # An absent collateral price (oracle outage) must NOT be treated as a crash:
    # a position we cannot fully price is not flagged liquidatable.
    loan = _loan_state()
    assert loan.is_liquidatable(PriceMap()) is False
