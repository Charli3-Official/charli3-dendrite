"""Gated: dbsync resolves V4 pool actions and Ogmios evaluates them.

The captured cancel replays against its spent pool with ``allow_spent``. The live
pool edited and cancelled here is the first live pool whose manager a key owns; its
owner's wallet is that key with the pool's stake credential. The create is funded by
a synthetic wallet, and every funding UTxO reaches Ogmios only as an additional
UTxO. Needs ``DBSYNC_*`` and ``OGMIOS_HOST``.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from pycardano import Address
from pycardano import Network
from pycardano import VerificationKeyHash

from charli3_dendrite.lending.fluidtokens.transactions.utxos import Utxo
from charli3_dendrite.lending.fluidtokens_v4.constants import resolve_config
from charli3_dendrite.lending.fluidtokens_v4.indexing import EntityKind
from charli3_dendrite.lending.fluidtokens_v4.indexing import entity_selectors
from charli3_dendrite.lending.fluidtokens_v4.loader import fetch_entities
from charli3_dendrite.lending.fluidtokens_v4.transactions.common import pool_nft_name
from charli3_dendrite.lending.fluidtokens_v4.transactions.pool_action import (
    PoolPosition,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.pool_cancel import (
    PoolCancelSnapshot,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.pool_cancel import (
    build_pool_cancel,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.pool_create import PoolOffer
from charli3_dendrite.lending.fluidtokens_v4.transactions.pool_create import (
    PoolCreateSnapshot,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.pool_create import (
    build_pool_create,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.pool_edit import PoolEdit
from charli3_dendrite.lending.fluidtokens_v4.transactions.pool_edit import (
    PoolEditSnapshot,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.pool_edit import (
    build_pool_edit,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.pool_terms import (
    LenderTerms,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.resolve import (
    resolve_pool_manager_utxo,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.resolve import resolve_utxo
from tests.lending.fluidtokens_v4.transactions.replay import build
from tests.lending.fluidtokens_v4.transactions.replay import captured_redeemers
from tests.lending.fluidtokens_v4.transactions.replay import evaluate
from tests.lending.fluidtokens_v4.transactions.replay import fixture
from tests.lending.fluidtokens_v4.transactions.replay import fixture_utxos
from tests.lending.fluidtokens_v4.transactions.replay import needs_dbsync
from tests.lending.fluidtokens_v4.transactions.replay import redeemers

pytestmark = needs_dbsync

_FUNDING_LOVELACE = 20_000_000_000
_EDIT = ["spend", "spend", "withdraw", "withdraw", "withdraw", "withdraw"]


@pytest.fixture(scope="module")
def backend():  # noqa: ANN201
    from charli3_dendrite.backend.dbsync import DbsyncBackend

    return DbsyncBackend()


def _funding(address: str) -> Utxo:
    """A synthetic wallet UTxO at ``address`` holding ample ADA."""
    return Utxo(
        address=address,
        lovelace=_FUNDING_LOVELACE,
        assets=[],
        datum=None,
        out_ref=("ff" * 32, 0),
    )


def _live_pool(backend) -> tuple[PoolPosition, str]:  # noqa: ANN001
    """The first live pool managed by a key, and its owner's wallet address."""
    selector = entity_selectors(resolve_config(backend))[EntityKind.POOL]
    for state in fetch_entities(backend, selector):
        tx_hash, index = state.out_ref.split("#")
        pool = resolve_utxo(backend, (tx_hash, int(index)))
        position = PoolPosition(
            pool=pool,
            pool_manager=resolve_pool_manager_utxo(backend, pool_nft_name(pool)),
        )
        try:
            position.check()
            owner = position.owner_pkh
        except (NotImplementedError, ValueError):
            continue
        wallet = Address(
            payment_part=VerificationKeyHash(owner),
            staking_part=Address.decode(pool.address).staking_part,
            network=Network.MAINNET,
        ).encode()
        return position, wallet
    pytest.skip("no live pool is managed by a key")


def test_live_cancel_reproduces_the_capture(backend) -> None:  # noqa: ANN001
    fix = fixture("pool_cancel")
    capture = PoolCancelSnapshot.from_capture(fix)
    snapshot = PoolCancelSnapshot.from_backend(
        backend,
        pools=[capture.positions[0].out_ref],
        lender_address=capture.funding[0].address,
        funding=capture.funding,
        allow_spent=True,
    )
    built = build(build_pool_cancel, snapshot, slot=fix["invalid_before"])
    assert redeemers(built.tx) == captured_redeemers(fix)
    assert evaluate(built, fixture_utxos(fix)) == sorted(_EDIT + ["mint", "mint"])


def test_live_pool_edit_evaluates(backend) -> None:  # noqa: ANN001
    position, wallet = _live_pool(backend)
    terms = LenderTerms.from_pool_datum(position.pool_datum)
    snapshot = PoolEditSnapshot.from_backend(
        backend,
        edits=[
            PoolEdit(
                position.out_ref,
                terms=replace(
                    terms,
                    common_data=replace(
                        terms.common_data,
                        interest_rate=terms.common_data.interest_rate + 1,
                    ),
                ),
                principal_change=1,
            ),
        ],
        lender_address=wallet,
        funding=[_funding(wallet)],
    )
    built = build(build_pool_edit, snapshot, slot=0)
    assert evaluate(built, snapshot.funding) == _EDIT


def test_live_pool_cancel_evaluates(backend) -> None:  # noqa: ANN001
    position, wallet = _live_pool(backend)
    snapshot = PoolCancelSnapshot.from_backend(
        backend,
        pools=[position.out_ref],
        lender_address=wallet,
        funding=[_funding(wallet)],
    )
    built = build(build_pool_cancel, snapshot, slot=0)
    assert evaluate(built, snapshot.funding) == sorted(_EDIT + ["mint", "mint"])


def test_live_pool_create_evaluates(backend) -> None:  # noqa: ANN001
    position, _ = _live_pool(backend)
    lender = Address(
        payment_part=VerificationKeyHash(b"\x11" * 28),
        staking_part=VerificationKeyHash(b"\x22" * 28),
        network=Network.MAINNET,
    ).encode()
    snapshot = PoolCreateSnapshot.from_backend(
        backend,
        offers=[PoolOffer(LenderTerms.from_pool_datum(position.pool_datum), 1_000)],
        lender_address=lender,
        funding=[_funding(lender)],
    )
    built = build(build_pool_create, snapshot, slot=0)
    assert evaluate(built, snapshot.funding) == ["mint", "mint"]
