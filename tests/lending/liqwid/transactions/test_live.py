"""Live Liqwid V2 builds evaluated by Ogmios against the chain tip.

Each cell resolves a flow with ``from_backend`` from dbsync and evaluates the
unsigned transaction: the loans, action UTxOs, records and reference scripts are
read by Ogmios from its own ledger, and only the wallet funding is supplied. The
cells cover what never happened on chain: modifications of loans whose token is
named ``30``/``31``, an LP-market partial repay, an open on SNEK2-ADA, the close of a
loan owing only its minimum interest, and a redeem on a market over its supply cap.

A cell whose loan is spent by run time, or whose market cannot take the flow at
the moment (awaiting its batch, short of liquidity), skips with the reason. The
wallets funding the loan flows are made up (Ogmios checks scripts, not balances);
the redeem spends the real wallet holding the qTokens.
"""

from __future__ import annotations

import contextlib
import random
from collections.abc import Callable
from collections.abc import Iterator
from typing import Any

import pytest
from pycardano import Address
from pycardano import Network
from pycardano import VerificationKeyHash

from charli3_dendrite.lending.liqwid.deployment import default_deployment
from charli3_dendrite.lending.liqwid.transactions.borrow import BorrowSnapshot
from charli3_dendrite.lending.liqwid.transactions.borrow import build_borrow
from charli3_dendrite.lending.liqwid.transactions.borrow_more import IncreaseSnapshot
from charli3_dendrite.lending.liqwid.transactions.borrow_more import build_increase
from charli3_dendrite.lending.liqwid.transactions.builder import chosen_action_utxos
from charli3_dendrite.lending.liqwid.transactions.collateral import CollateralSnapshot
from charli3_dendrite.lending.liqwid.transactions.collateral import build_collateral
from charli3_dendrite.lending.liqwid.transactions.errors import (
    InsufficientLiquidityError,
)
from charli3_dendrite.lending.liqwid.transactions.errors import MarketAwaitingBatchError
from charli3_dendrite.lending.liqwid.transactions.errors import UnhealthyLoanError
from charli3_dendrite.lending.liqwid.transactions.repay import RepaySnapshot
from charli3_dendrite.lending.liqwid.transactions.repay import build_repay
from charli3_dendrite.lending.liqwid.transactions.resolve import LoanRecord
from charli3_dendrite.lending.liqwid.transactions.resolve import resolve_loan
from charli3_dendrite.lending.liqwid.transactions.resolve import resolve_market
from charli3_dendrite.lending.liqwid.transactions.supply import RedeemSnapshot
from charli3_dendrite.lending.liqwid.transactions.supply import build_redeem
from charli3_dendrite.lending.liqwid.transactions.terms import close_payment
from charli3_dendrite.lending.liqwid.transactions.terms import settle
from charli3_dendrite.lending.transactions.errors import OgmiosEvaluationError
from charli3_dendrite.lending.transactions.infra import current_slot
from charli3_dendrite.lending.transactions.resolve import db_query_rows
from charli3_dendrite.lending.transactions.resolve import resolve_utxo_by_outref
from charli3_dendrite.lending.transactions.utxos import Utxo
from charli3_dendrite.lending.units import out_ref_key
from tests.lending.liqwid.transactions.live import dbsync_backend
from tests.lending.liqwid.transactions.replay import Built
from tests.lending.liqwid.transactions.replay import Mutation
from tests.lending.liqwid.transactions.replay import build
from tests.lending.liqwid.transactions.replay import evaluate
from tests.lending.liqwid.transactions.replay import mints
from tests.lending.liqwid.transactions.replay import mutated
from tests.lending.liqwid.transactions.replay import needs_dbsync
from tests.lending.liqwid.transactions.replay import shift_value
from tests.lending.liqwid.transactions.replay import synthetic_utxo

pytestmark = needs_dbsync

# Ogmios: some scripts of the transaction failed; an input is not on the ledger.
SCRIPT_FAILURE = 3010
MISSING_INPUT = 3004

# Loans whose token is named 31: a USDC loan and an Ada loan.
USDC_LOAN_31 = "b48aba3d0836adb01405df4518ae915bf84c7f65d993186037adebee362ea7a9#40"
ADA_LOAN_31 = "79e2bda3acd86e9188c5a008fb825437e9d91a04d871b38961f05d907cc00902#40"
# A BTC loan owing only its minimum interest, (0, 0, m0 > 0).
MINIMUM_ONLY_LOAN = "027c8b4f62b81f6c116951a5210b05d18213961673af455d91a5356ffb4b5bbf#1"
LP_MARKET = "USDM-USDA-MINSWAP-LP-STABLE"
FUNDING_LOVELACE = 50_000_000


# --- helpers -------------------------------------------------------------------


@pytest.fixture(scope="module")
def backend() -> Any:  # noqa: ANN401
    return dbsync_backend()


def _market(name: str) -> Any:  # noqa: ANN401
    return default_deployment().market_by_name(name)


def _key_address(pkh: str) -> str:
    return str(
        Address(
            payment_part=VerificationKeyHash(bytes.fromhex(pkh)),
            network=Network.MAINNET,
        ),
    )


def _live_loan(backend: Any, out_ref: str) -> LoanRecord:  # noqa: ANN401
    """The loan at ``out_ref``; the cell skips when it is spent."""
    tx_hash, index = out_ref_key(out_ref)
    try:
        resolve_utxo_by_outref(backend, tx_hash, index)
    except ValueError:
        pytest.skip(f"loan {out_ref} is spent")
    return resolve_loan(backend, out_ref)


def _funding(seed: str, owner_address: str, assets: dict[str, int]) -> list[Utxo]:
    """A made-up wallet UTxO of ``owner_address`` holding ``assets`` and 50 ADA."""
    lovelace = FUNDING_LOVELACE + assets.get("lovelace", 0)
    tokens = [
        (unit[:56], unit[56:], qty)
        for unit, qty in assets.items()
        if unit != "lovelace"
    ]
    return [
        synthetic_utxo(
            f"live-{seed}",
            address=owner_address,
            lovelace=lovelace,
            assets=tokens,
        ),
    ]


@contextlib.contextmanager
def _market_permitting() -> Iterator[None]:
    """Skip the cell when the market cannot take the flow at the moment."""
    try:
        yield
    except (MarketAwaitingBatchError, InsufficientLiquidityError) as exc:
        pytest.skip(f"market state at the tip: {exc!r}")


def _evaluated(
    backend: Any,  # noqa: ANN401
    resolve: Callable[[frozenset[str]], Any],
    build_flow: Callable[..., None],
    *mutations: Mutation,
    funding: Callable[[Any], list[Utxo]],
) -> tuple[Any, Built, list[str]]:
    """Resolve, build and evaluate a flow: its snapshot, the build and the purposes.

    ``resolve`` takes the action out-refs to exclude. When Ogmios no longer holds
    an input (a batch spent the action UTxO or a record since dbsync was read),
    the flow is resolved and evaluated once more, without that action UTxO.
    """
    excluded: frozenset[str] = frozenset()
    for attempt in range(2):
        with _market_permitting():
            snapshot = resolve(excluded)
        built = build(
            mutated(build_flow, *mutations),
            snapshot,
            slot=current_slot(backend),
        )
        try:
            return snapshot, built, evaluate(built, funding(snapshot))
        except OgmiosEvaluationError as exc:
            if exc.code != MISSING_INPUT or attempt:
                raise
            missing = {f"{h}#{i}" for h, i in exc.missing_inputs()}
            excluded = excluded | (chosen_action_utxos(snapshot) & missing)
    raise AssertionError("unreachable")


def _loan_mint(built: Built, policy: str) -> set[tuple[str, int]]:
    return {(unit, qty) for unit, qty in mints(built) if unit.startswith(policy)}


def _owner(loan: LoanRecord) -> str:
    return _key_address(loan.datum.owner)


# --- loans named 30/31 ---------------------------------------------------------


def test_a_collateral_add_on_a_31_loan_flips_it_to_01(backend):
    loan = _live_loan(backend, USDC_LOAN_31)
    assert loan.token_name == "31"
    unit, held = next(iter(sorted(loan.collateral.items())))
    added = max(1, held // 100)
    owner = _owner(loan)
    funding = _funding("collateral-add-31", owner, {unit: added})

    def resolve(excluded: frozenset[str]) -> CollateralSnapshot:
        try:
            return CollateralSnapshot.from_backend(
                backend,
                loan_utxo=USDC_LOAN_31,
                owner_address=owner,
                collateral={**loan.collateral, unit: held + added},
                funding=funding,
            )
        except UnhealthyLoanError as exc:
            pytest.skip(f"the loan is unhealthy at the tip: {exc!r}")

    _, built, purposes = _evaluated(
        backend,
        resolve,
        build_collateral,
        funding=lambda snapshot: snapshot.funding,
    )
    policy = loan.market.new_borrow_policy
    assert _loan_mint(built, policy) == {(policy + "31", -1), (policy + "01", 1)}
    assert purposes == ["mint", "spend", "withdraw"]


def test_a_borrow_more_on_a_31_loan_flips_it_to_01(backend):
    loan = _live_loan(backend, ADA_LOAN_31)
    assert loan.token_name == "31"
    owner = _owner(loan)
    funding = _funding("borrow-more-31", owner, {})

    def resolve(excluded: frozenset[str]) -> IncreaseSnapshot:
        try:
            return IncreaseSnapshot.from_backend(
                backend,
                loan_utxo=ADA_LOAN_31,
                owner_address=owner,
                amount=81_000_000,
                exclude=excluded,
                funding=funding,
                rng=random.Random(0),
            )
        except UnhealthyLoanError as exc:
            pytest.skip(f"the loan cannot borrow 81 ADA more at the tip: {exc!r}")

    _, built, purposes = _evaluated(
        backend,
        resolve,
        build_increase,
        funding=lambda snapshot: snapshot.funding,
    )
    policy = loan.market.new_borrow_policy
    assert _loan_mint(built, policy) == {(policy + "31", -1), (policy + "01", 1)}
    assert purposes == ["mint", "spend", "spend", "withdraw"]


def _close(
    backend: Any,  # noqa: ANN401
    out_ref: str,
    seed: str,
    *mutations: Mutation,
) -> tuple[RepaySnapshot, Built, list[str]]:
    """Close the loan at ``out_ref`` with the payoff computed at the tip."""
    loan = _live_loan(backend, out_ref)
    owner = _owner(loan)

    def resolve(excluded: frozenset[str]) -> RepaySnapshot:
        records = resolve_market(backend, loan.market)
        payoff = close_payment(loan.datum, records.state_datum.index)
        return RepaySnapshot.from_backend(
            backend,
            loan_utxo=out_ref,
            owner_address=owner,
            repay=payoff,
            exclude=excluded,
            funding=_funding(seed, owner, {loan.market.underlying: payoff}),
            rng=random.Random(0),
        )

    return _evaluated(
        backend,
        resolve,
        build_repay,
        *mutations,
        funding=lambda snapshot: snapshot.funding,
    )


def test_a_31_loan_closes_paying_its_payoff(backend):
    snapshot, built, purposes = _close(backend, USDC_LOAN_31, "close-31")
    assert snapshot.closes
    assert snapshot.repay == close_payment(
        snapshot.loan.datum,
        snapshot.records.state_datum.index,
    )
    policy = snapshot.market.new_borrow_policy
    assert _loan_mint(built, policy) == {(policy + "31", -1)}
    assert purposes == ["mint", "spend", "spend", "withdraw"]


def test_one_more_paid_into_the_action_on_a_31_close_is_refused(backend):
    loan = _live_loan(backend, USDC_LOAN_31)
    records = resolve_market(backend, loan.market)
    credential = records.params_datum.action_validator
    one_more = shift_value(credential, assets={loan.market.underlying: 1})
    with pytest.raises(OgmiosEvaluationError) as info:
        _close(backend, USDC_LOAN_31, "close-31-plus-one", one_more)
    assert info.value.code == SCRIPT_FAILURE, info.value


def test_a_loan_owing_only_its_minimum_interest_closes(backend):
    snapshot, built, purposes = _close(backend, MINIMUM_ONLY_LOAN, "minimum-only")
    datum = snapshot.loan.datum
    assert (datum.principal, datum.interest) == (0, 0)
    assert datum.min_interest > 0
    assert snapshot.closes
    assert snapshot.repay == datum.min_interest
    assert purposes == ["mint", "spend", "spend", "withdraw"]


# --- markets with no such flow on chain ----------------------------------------


def _live_loans(backend: Any, policy: str) -> list[str]:  # noqa: ANN401
    """The out-refs of the latest unspent outputs holding a ``policy`` token."""
    rows = db_query_rows(
        backend,
        """SELECT encode(t.hash, 'hex') AS tx_hash, o.index AS idx
           FROM ma_tx_out m
           JOIN multi_asset ma ON ma.id = m.ident
           JOIN tx_out o ON o.id = m.tx_out_id
           JOIN tx t ON t.id = o.tx_id
           WHERE ma.policy = decode(%(policy)s, 'hex')
             AND o.consumed_by_tx_id IS NULL
           ORDER BY o.id DESC
           LIMIT 50""",
        {"policy": policy},
    )
    return [f"{r['tx_hash']}#{r['idx']}" for r in rows]


def test_an_lp_market_loan_repays_in_part(backend):
    market = _market(LP_MARKET)
    records = resolve_market(backend, market)
    minimum = records.params_datum.min_value
    s5 = records.state_datum.index
    candidates = []
    for out_ref in _live_loans(backend, market.new_borrow_policy):
        loan = resolve_loan(backend, out_ref)
        debt = settle(loan.datum, s5).debt
        if minimum <= debt and close_payment(loan.datum, s5) > minimum:
            candidates.append(loan)
    if not candidates:
        pytest.skip(f"no live {LP_MARKET} loan owes more than {minimum}")
    loan = candidates[0]
    owner = _owner(loan)
    out_ref = f"{loan.utxo.out_ref[0]}#{loan.utxo.out_ref[1]}"

    def resolve(excluded: frozenset[str]) -> RepaySnapshot:
        return RepaySnapshot.from_backend(
            backend,
            loan_utxo=out_ref,
            owner_address=owner,
            repay=minimum,
            exclude=excluded,
            funding=_funding("lp-repay", owner, {market.underlying: minimum}),
            rng=random.Random(0),
        )

    snapshot, _, purposes = _evaluated(
        backend,
        resolve,
        build_repay,
        funding=lambda snapshot: snapshot.funding,
    )
    assert not snapshot.closes
    assert purposes == ["mint", "spend", "spend", "withdraw"]


def test_a_snek2_ada_loan_opens(backend):
    """An open of ``p[1]`` on SNEK2-ADA against qSNEK2, its only collateral.

    The collateral starts at 10^9 qSNEK2 and grows fourfold until the loan is
    healthy at the tip.
    """
    market = _market("SNEK2-ADA")
    amount = resolve_market(backend, market).params_datum.min_value
    qsnek2 = _market("SNEK2").qtoken_policy
    owner = _key_address("0b" * 28)

    def resolver(qtokens: int) -> Callable[[frozenset[str]], BorrowSnapshot]:
        funding = _funding(f"snek2-open-{qtokens}", owner, {qsnek2: qtokens})

        def resolve(excluded: frozenset[str]) -> BorrowSnapshot:
            return BorrowSnapshot.from_backend(
                backend,
                market=market.name,
                owner_address=owner,
                amount=amount,
                collateral={qsnek2: qtokens},
                exclude=excluded,
                funding=funding,
                rng=random.Random(0),
            )

        return resolve

    qtokens = 10**9
    for _ in range(24):
        try:
            snapshot, built, purposes = _evaluated(
                backend,
                resolver(qtokens),
                build_borrow,
                funding=lambda snapshot: snapshot.funding,
            )
        except UnhealthyLoanError:
            qtokens *= 4
            continue
        assert (market.new_borrow_policy + "01", 1) in mints(built)
        assert snapshot.amount == amount
        assert purposes == ["mint", "spend", "withdraw"]
        return
    pytest.fail(f"no collateral up to {qtokens} qSNEK2 makes the open healthy")


# --- a market over its supply cap ----------------------------------------------


def _largest_holder(backend: Any, policy: str) -> tuple[str, list[str]]:  # noqa: ANN401
    """The key address holding the most of ``policy``'s qTokens, and those UTxOs."""
    rows = db_query_rows(
        backend,
        """SELECT a.address AS address, SUM(m.quantity) AS held
           FROM ma_tx_out m
           JOIN multi_asset ma ON ma.id = m.ident
           JOIN tx_out o ON o.id = m.tx_out_id
           JOIN address a ON a.id = o.address_id
           WHERE ma.policy = decode(%(policy)s, 'hex')
             AND ma.name = decode('', 'hex')
             AND o.consumed_by_tx_id IS NULL
             AND NOT a.has_script
           GROUP BY a.address
           ORDER BY held DESC
           LIMIT 1""",
        {"policy": policy},
    )
    if not rows:
        pytest.skip(f"no wallet holds qTokens of policy {policy}")
    address = rows[0]["address"]
    utxos = db_query_rows(
        backend,
        """SELECT encode(t.hash, 'hex') AS tx_hash, o.index AS idx
           FROM ma_tx_out m
           JOIN multi_asset ma ON ma.id = m.ident
           JOIN tx_out o ON o.id = m.tx_out_id
           JOIN tx t ON t.id = o.tx_id
           JOIN address a ON a.id = o.address_id
           WHERE ma.policy = decode(%(policy)s, 'hex')
             AND ma.name = decode('', 'hex')
             AND o.consumed_by_tx_id IS NULL
             AND a.address = %(address)s""",
        {"policy": policy, "address": address},
    )
    return address, [f"{r['tx_hash']}#{r['idx']}" for r in utxos]


def test_a_redeem_on_a_market_over_its_supply_cap_evaluates(backend):
    """A redeem is not held to the supply cap: NIGHT redeems while over its cap."""
    market = _market("NIGHT")
    records = resolve_market(backend, market)
    params, state = records.params_datum, records.state_datum
    if params.supply_cap is None or state.cash + state.principal <= params.supply_cap:
        pytest.skip("NIGHT is not over its supply cap at the tip")
    address, out_refs = _largest_holder(backend, market.qtoken_policy)
    wallet = [resolve_utxo_by_outref(backend, *out_ref_key(r)) for r in out_refs]

    def resolve(excluded: frozenset[str]) -> RedeemSnapshot:
        return RedeemSnapshot.from_backend(
            backend,
            market=market.name,
            owner_address=address,
            amount=params.min_value,
            exclude=excluded,
            funding=wallet,
            rng=random.Random(0),
        )

    snapshot, _, purposes = _evaluated(
        backend,
        resolve,
        build_redeem,
        funding=lambda snapshot: snapshot.funding,
    )
    assert snapshot.payout >= params.min_value
    assert purposes == ["mint", "spend"]
