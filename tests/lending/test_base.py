from dataclasses import dataclass
from decimal import Decimal

from pycardano import PlutusData

from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dataclasses.models import PoolSelector
from charli3_dendrite.dataclasses.models import PoolStateList
from charli3_dendrite.lending.base import AbstractLendingPoolState
from charli3_dendrite.lending.base import AbstractLoanState
from charli3_dendrite.lending.base import LendingBook
from charli3_dendrite.lending.base import LendingPriceBook
from charli3_dendrite.lending.oracles.models import OraclePrice
from charli3_dendrite.lending.oracles.models import OracleRef
from charli3_dendrite.lending.oracles.models import OracleSource
from charli3_dendrite.lending.oracles.models import register_resolver

COLLAT = "cccccccccccccccccccccccccccccccccccccccccccccccccccccccc"


class _FakeLoan(AbstractLoanState):
    debt: int
    collateral_amount: int
    loan_ref: str

    @classmethod
    def protocol(cls):
        return "Fake"

    @classmethod
    def loan_datum_class(cls):
        from pycardano import PlutusData

        return PlutusData

    @classmethod
    def loan_selector(cls):
        return PoolSelector(addresses=["addr_loan"])

    @property
    def stake_address(self):
        return None

    @property
    def loan_id(self):
        return self.loan_ref

    @property
    def pool_id(self):
        return "pool-1"

    @property
    def borrowed_unit(self):
        return "lovelace"

    def oracle_refs(self):
        return [
            OracleRef(source=OracleSource.DEX_POOLED, token=COLLAT, address="addr_feed")
        ]

    def current_debt(self):
        return self.debt

    def collateral_value_lovelace(self, prices):
        price = prices.require(COLLAT)
        return int(self.collateral_amount * price.as_decimal())

    def health_factor(self, prices):
        debt = self.current_debt()
        if debt == 0:
            return Decimal("Infinity")
        return Decimal(self.collateral_value_lovelace(prices)) / Decimal(debt)


class _FakeResolver:
    source = OracleSource.DEX_POOLED

    def selectors(self, refs):
        return [r.selector() for r in refs if r.selector() is not None]

    def resolve(self, ref, utxos):
        return OraclePrice(
            token=ref.token,
            num=2,
            denom=1,
            source=self.source,
            valid_from=0,
            valid_to=10**13,
        )


class _FakeBackend:
    def get_pool_utxos(self, **kwargs):
        return PoolStateList(root=[])


def _loan(debt, collat, ref):
    return _FakeLoan(
        assets=Assets(root={"lovelace": 1_000_000}),
        block_time=1000,
        block_index=1,
        plutus_v2=True,
        datum_cbor="d87980",
        datum_hash="00",
        tx_index=0,
        tx_hash="ab",
        debt=debt,
        collateral_amount=collat,
        loan_ref=ref,
    )


def test_price_book_build_dispatches_and_collects():
    register_resolver(_FakeResolver())
    loans = [_loan(100, 100, "l1")]
    pm = LendingPriceBook.build(loans, backend=_FakeBackend())
    assert pm.require(COLLAT).as_decimal() == Decimal("2")


def test_book_filters_by_health():
    register_resolver(_FakeResolver())
    healthy = _loan(100, 100, "healthy")  # HF = 200/100 = 2.0
    underwater = _loan(100, 10, "underwater")  # HF = 20/100 = 0.2
    watch = _loan(100, 52, "watch")  # HF = 104/100 = 1.04
    loans = [healthy, underwater, watch]
    pm = LendingPriceBook.build(loans, backend=_FakeBackend())
    book = LendingBook.from_loans(loans, prices=pm)

    assert [ln.loan_id for ln in book.liquidatable_loans()] == ["underwater"]
    assert [ln.loan_id for ln in book.near_liquidation()] == ["watch"]
    assert book.liquidation_candidates_by_urgency()[0].loan_id == "underwater"
    assert {ln.loan_id for ln in book.by_pool("pool-1")} == {
        "healthy",
        "underwater",
        "watch",
    }


def test_repay_amount_for_target_hf():
    register_resolver(_FakeResolver())
    loan = _loan(100, 100, "l")  # collateral value = 200 lovelace, debt = 100
    pm = LendingPriceBook.build([loan], backend=_FakeBackend())
    assert loan.repay_amount_for_target_hf(Decimal("2"), pm) == 0  # already HF 2
    assert loan.repay_amount_for_target_hf(Decimal("4"), pm) == 50  # need debt <= 50


@dataclass
class _TrivialDatum(PlutusData):
    CONSTR_ID = 0


class _FakePool(AbstractLendingPoolState):
    @classmethod
    def protocol(cls):
        return "FakePool"

    @classmethod
    def pool_datum_class(cls):
        return _TrivialDatum

    @classmethod
    def pool_selector(cls):
        return PoolSelector(addresses=["addr_pool"])

    @property
    def stake_address(self):
        return None

    @property
    def pool_id(self):
        return "pool-1"

    @property
    def borrowable_unit(self):
        return "lovelace"

    @property
    def utilization_ratio(self):
        return Decimal("0")

    def max_borrow_for_collateral_value(self, collateral_value_lovelace):
        return collateral_value_lovelace


def test_pool_base_lazy_datum_is_cached():
    cbor = _TrivialDatum().to_cbor_hex()
    pool = _FakePool(
        assets=Assets(root={"lovelace": 1_000_000}),
        block_time=1000,
        block_index=1,
        plutus_v2=True,
        datum_cbor=cbor,
        datum_hash="00",
        tx_index=0,
        tx_hash="ab",
    )
    parsed = pool.pool_datum
    assert isinstance(parsed, _TrivialDatum)
    # Second access must return the SAME cached object (private-attr caching).
    assert pool.pool_datum is parsed
