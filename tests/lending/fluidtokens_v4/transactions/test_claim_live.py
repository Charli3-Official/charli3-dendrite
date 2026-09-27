"""Gated: dbsync finds a lender's pending repayments and Ogmios evaluates the claim.

The lender is the first one whose bonds at the lender manager own a repayment; its
wallet is that key with the stake credential of its bond UTxO. The claim is funded by
a synthetic wallet UTxO, which reaches Ogmios only as an additional UTxO. Needs
``DBSYNC_*`` and ``OGMIOS_HOST``.
"""

from __future__ import annotations

import pytest
from pycardano import Address
from pycardano import Network
from pycardano import VerificationKeyHash

from charli3_dendrite.lending.fluidtokens.transactions.utxos import Utxo
from charli3_dendrite.lending.fluidtokens_v4 import constants as c
from charli3_dendrite.lending.fluidtokens_v4.constants import resolve_config
from charli3_dendrite.lending.fluidtokens_v4.indexing import EntityKind
from charli3_dendrite.lending.fluidtokens_v4.indexing import entity_selectors
from charli3_dendrite.lending.fluidtokens_v4.loader import fetch_entities
from charli3_dendrite.lending.fluidtokens_v4.transactions.claim import ClaimSnapshot
from charli3_dendrite.lending.fluidtokens_v4.transactions.claim import build_claim
from tests.lending.fluidtokens_v4.transactions.replay import build
from tests.lending.fluidtokens_v4.transactions.replay import evaluate
from tests.lending.fluidtokens_v4.transactions.replay import needs_dbsync

pytestmark = needs_dbsync


@pytest.fixture(scope="module")
def backend():  # noqa: ANN201
    from charli3_dendrite.backend.dbsync import DbsyncBackend

    return DbsyncBackend()


def _lender_with_repayments(backend) -> str | None:  # noqa: ANN001
    """The wallet of the first lender whose bonds own a pending repayment."""
    selectors = entity_selectors(resolve_config(backend))
    owners = {
        name: manager
        for manager in fetch_entities(backend, selectors[EntityKind.LENDER_MANAGER])
        if manager.lender_auth.kind == "signature"  # type: ignore[attr-defined]
        for name in manager.lender_bond_names  # type: ignore[attr-defined]
    }
    for payment in fetch_entities(backend, selectors[EntityKind.ASSET_MANAGER]):
        unit = payment.owner_unit  # type: ignore[attr-defined]
        if unit is None or not unit.startswith(c.LENDER_BOND_POLICY):
            continue
        manager = owners.get(unit[len(c.LENDER_BOND_POLICY) :])
        if manager is not None:
            return Address(
                payment_part=VerificationKeyHash(
                    bytes.fromhex(manager.lender_auth.hash_hex),  # type: ignore[attr-defined]
                ),
                staking_part=Address.decode(manager.address).staking_part,
                network=Network.MAINNET,
            ).encode()
    return None


def test_live_claim_evaluates(backend) -> None:  # noqa: ANN001
    lender = _lender_with_repayments(backend)
    if lender is None:
        pytest.skip("no lender has a pending repayment")
    funding = Utxo(
        address=lender,
        lovelace=5_000_000,
        assets=[],
        datum=None,
        out_ref=("ff" * 32, 0),
    )
    snapshot = ClaimSnapshot.from_backend(
        backend,
        lender_address=lender,
        funding=[funding],
    )
    spends = sum(1 + len(p.repayments) for p in snapshot.positions)
    built = build(build_claim, snapshot, slot=0)
    assert evaluate(built, snapshot.funding) == sorted(
        ["spend"] * spends + ["withdraw"] * 3,
    )
