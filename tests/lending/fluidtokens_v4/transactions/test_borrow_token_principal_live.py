"""Gated: borrow a token from a live pool at live prices and evaluate it on Ogmios.

The pool is the first live pool this builder serves that lends a token and offers the
wanted collateral (ADA, or a token); the borrow takes 1% of its principal (or a single
unit, whose ADA collateral falls below the loan's minimum ADA), sized and priced by
``BorrowSnapshot.from_backend`` with signed prices fetched from the FluidTokens
registry. The borrow is evaluated as built, then with one unit less collateral. The
borrower is synthetic, and so is its funding UTxO, which reaches Ogmios only as an
additional UTxO; every other input is read from the live ledger. Needs ``DBSYNC_*``,
``OGMIOS_HOST`` and ``FLUIDTOKENS_API_KEY``.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from pycardano import Address
from pycardano import Network
from pycardano import VerificationKeyHash

from charli3_dendrite.lending.fluidtokens.transactions.borrow_terms import (
    min_collateral_amount,
)
from charli3_dendrite.lending.fluidtokens.transactions.utxos import Utxo
from charli3_dendrite.lending.fluidtokens_v4.constants import resolve_config
from charli3_dendrite.lending.fluidtokens_v4.datums import PoolDatum
from charli3_dendrite.lending.fluidtokens_v4.indexing import EntityKind
from charli3_dendrite.lending.fluidtokens_v4.indexing import entity_selectors
from charli3_dendrite.lending.fluidtokens_v4.loader import fetch_entities
from charli3_dendrite.lending.fluidtokens_v4.transactions.borrow import BorrowLeg
from charli3_dendrite.lending.fluidtokens_v4.transactions.borrow import BorrowSnapshot
from charli3_dendrite.lending.fluidtokens_v4.transactions.borrow import PoolBorrow
from charli3_dendrite.lending.fluidtokens_v4.transactions.borrow import build_borrow
from charli3_dendrite.lending.fluidtokens_v4.transactions.borrow import option_unit
from charli3_dendrite.lending.fluidtokens_v4.transactions.common import is_policy_wide
from charli3_dendrite.lending.fluidtokens_v4.transactions.common import script_hash_of
from charli3_dendrite.lending.fluidtokens_v4.transactions.lender_bond import (
    sends_bonds_to_lender_manager,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.oracle import OracleWitness
from charli3_dendrite.lending.fluidtokens_v4.transactions.resolve import resolve_utxo
from charli3_dendrite.lending.units import constr
from tests.lending.fluidtokens_v4.transactions.replay import Built
from tests.lending.fluidtokens_v4.transactions.replay import build
from tests.lending.fluidtokens_v4.transactions.replay import evaluate
from tests.lending.fluidtokens_v4.transactions.replay import needs_dbsync

pytestmark = [
    needs_dbsync,
    pytest.mark.skipif(
        not os.environ.get("FLUIDTOKENS_API_KEY"),
        reason="needs FLUIDTOKENS_API_KEY",
    ),
]

# A synthetic borrower: a key-hash payment credential with a stake credential.
_BORROWER = Address(
    payment_part=VerificationKeyHash(b"\x11" * 28),
    staking_part=VerificationKeyHash(b"\x22" * 28),
    network=Network.MAINNET,
).encode()
_FUNDING_LOVELACE = 10_000_000_000
_PERMISSIONLESS = b"NONE"
_BOOL_TRUE = 1


@pytest.fixture(scope="module")
def backend():  # noqa: ANN201
    from charli3_dendrite.backend.dbsync import DbsyncBackend

    return DbsyncBackend()


def _served(datum: PoolDatum) -> bool:
    """True for a pool this builder serves whose token principal is oracle-priced.

    False for the pools the builder refuses (permissioned, sending borrower bonds to a
    script, or sending lender bonds anywhere but the lender manager) and for pools
    that read no oracle price.
    """
    return (
        bytes(datum.permissioned_condition_script_hash) == _PERMISSIONLESS
        and not datum.common_data.borrower_bond_destination_script_hash
        and sends_bonds_to_lender_manager(datum)
        and constr(datum.dynamic_collateral_price)[0] == _BOOL_TRUE
        and bool(datum.common_data.principal_asset.policy_id)
    )


def _token_pools(
    backend,  # noqa: ANN001
    *,
    ada: bool,
) -> Iterator[tuple[Utxo, int, int]]:
    """Live pools this builder serves that lend a token against ADA (or a token).

    Yields each pool, the index of its first such collateral option and the principal
    it holds. A policy-wide collateral is refused by the builder, and a collateral that
    is the principal itself shares its price, so neither is picked.
    """
    selector = entity_selectors(resolve_config(backend))[EntityKind.POOL]
    for state in fetch_entities(backend, selector):
        tx_hash, index = state.out_ref.split("#")
        pool = resolve_utxo(backend, (tx_hash, int(index)))
        datum = PoolDatum.from_cbor(pool.datum or "")
        principal = datum.common_data.principal_asset.unit()
        lent = sum(q for p, n, q in pool.assets if p + n == principal)
        if lent < 100 or not _served(datum):  # noqa: PLR2004 - 1% is a unit
            continue
        for option, collateral in enumerate(datum.collateral_options):
            unit = option_unit(datum, option)
            if is_policy_wide(collateral) or unit == principal:
                continue
            if (unit == "lovelace") == ada:
                yield pool, option, lent
                break


def _funding(leg: BorrowLeg) -> Utxo:
    """A synthetic wallet UTxO holding the loan's collateral and ample ADA."""
    assets = []
    if leg.collateral_unit != "lovelace":
        unit = leg.collateral_unit
        assets.append((unit[:56], unit[56:], leg.collateral_amount))
    return Utxo(
        address=_BORROWER,
        lovelace=_FUNDING_LOVELACE,
        assets=assets,
        datum=None,
        out_ref=("ff" * 32, 0),
    )


def _price(oracle: OracleWitness | None) -> tuple[int, int]:
    """``(num, den)`` of a signed price; 1:1 for a side priced without an oracle."""
    if oracle is None:
        return 1, 1
    return oracle.reward.price_num, oracle.reward.price_den


def _minimum(snapshot: BorrowSnapshot, leg: BorrowLeg) -> int:
    """The least collateral the pool accepts, at the snapshot's own signed prices."""
    price_num, price_den = _price(snapshot.oracle_for(leg))
    principal_num, principal_den = _price(snapshot.principal_oracle_for(leg))
    return min_collateral_amount(
        leg.pool_datum,
        chosen_collateral_index=leg.chosen_collateral_index,
        principal_amount=leg.principal_amount,
        price_num=price_num,
        price_den=price_den,
        principal_price_num=principal_num,
        principal_price_den=principal_den,
    )


def _borrow_action_failure(built: Built, snapshot: BorrowSnapshot) -> str:
    """The evaluation error of the borrow action's withdraw, as a ``match`` pattern."""
    hashes = sorted(
        Address.from_primitive(account).staking_part.payload.hex()
        for account in built.tx.transaction_body.withdraws
    )
    position = hashes.index(script_hash_of(snapshot.borrow_action_script_ref))
    return (
        r"ogmios evaluate error.*'validator': \{'index': "
        f"{position}, 'purpose': 'withdraw'}}"
    )


@pytest.mark.parametrize(
    ("ada", "one_unit", "oracle_withdraws"),
    [(True, False, 1), (True, True, 1), (False, False, 2)],
    ids=["ada-collateral", "ada-collateral-topped-up", "token-collateral"],
)
def test_live_token_borrow_evaluates_at_the_least_collateral(
    backend,  # noqa: ANN001
    ada: bool,
    one_unit: bool,
    oracle_withdraws: int,
) -> None:
    candidate = next(_token_pools(backend, ada=ada), None)
    if candidate is None:
        pytest.skip("no live pool lends a token against that collateral")
    pool, option, lent = candidate
    snapshot = BorrowSnapshot.from_backend(
        backend,
        borrows=[PoolBorrow(pool.out_ref, 1 if one_unit else lent // 100, option)],
        borrower_address=_BORROWER,
        funding=[],
    )
    (leg,) = snapshot.legs
    minimum = _minimum(snapshot, leg)
    assert leg.collateral_amount == minimum
    assert snapshot.principal_oracle_for(leg) is not None
    assert (snapshot.oracle_for(leg) is None) == ada
    if ada:
        # The loan's ADA is its collateral, topped up to the output's minimum ADA; a
        # single unit's collateral is a few lovelace, so that loan is topped up.
        assert leg.loan_lovelace >= minimum
        assert (leg.loan_lovelace > minimum) == one_unit
    snapshot.funding = [_funding(leg)]

    # As built: the collateral and every output's ADA exactly as sized. The pool
    # spend, three mints, the pool dispatch and borrow action withdraws, and one
    # oracle withdraw per side priced through an oracle.
    built = build(build_borrow, snapshot, slot=snapshot.valid_from)
    assert evaluate(built, snapshot.funding) == sorted(
        ["spend"] + ["mint"] * 3 + ["withdraw"] * (2 + oracle_withdraws),
    )

    # One unit less collateral fails the borrow action. An ADA collateral is the
    # loan's ADA; Ogmios runs the scripts only, so a loan below the ledger's minimum
    # ADA still tests the pool's collateral floor.
    leg.collateral_amount = minimum - 1
    if ada:
        leg.loan_lovelace = minimum - 1
    built = build(build_borrow, snapshot, slot=snapshot.valid_from)
    with pytest.raises(AssertionError, match=_borrow_action_failure(built, snapshot)):
        evaluate(built, snapshot.funding)
