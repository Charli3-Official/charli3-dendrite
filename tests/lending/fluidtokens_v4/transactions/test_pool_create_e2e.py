"""Gated: V4 pool creates evaluate on Ogmios against mainnet (OGMIOS_HOST)."""

from __future__ import annotations

from dataclasses import replace

import pytest
from pycardano import Address
from pycardano import TransactionOutput

from charli3_dendrite.lending.fluidtokens.transactions.utxos import Utxo
from charli3_dendrite.lending.fluidtokens_v4.transactions.pool_action import (
    PoolPosition,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.pool_cancel import (
    PoolCancelSnapshot,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.pool_cancel import (
    build_pool_cancel,
)
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
from charli3_dendrite.lending.fluidtokens_v4.transactions.pool_edit import edited_pool
from tests.lending.fluidtokens_v4.transactions.replay import build
from tests.lending.fluidtokens_v4.transactions.replay import evaluate
from tests.lending.fluidtokens_v4.transactions.replay import fixture
from tests.lending.fluidtokens_v4.transactions.replay import fixture_utxos
from tests.lending.fluidtokens_v4.transactions.replay import needs_ogmios

pytestmark = needs_ogmios

# Only the pool-manager and pool policies run.
_CREATE = ["mint", "mint"]

# A cancel: both spends, both burns, and the four withdrawals.
_CANCEL = sorted(["spend", "spend", "mint", "mint"] + ["withdraw"] * 4)

# An edit: both spends and the four withdrawals.
_EDIT = sorted(["spend", "spend"] + ["withdraw"] * 4)


def _as_utxo(output: TransactionOutput, out_ref: tuple[str, int]) -> Utxo:
    """A built ``output`` as a resolved ``Utxo`` at ``out_ref``.

    Ogmios reads it from ``additionalUtxo``, so a later transaction can spend it
    although it was never actually submitted.
    """
    assets = [
        (bytes(policy).hex(), name.payload.hex(), qty)
        for policy, names in output.amount.multi_asset.items()
        for name, qty in names.items()
    ]
    return Utxo(
        address=str(output.address),
        lovelace=output.amount.coin,
        assets=assets,
        datum=output.datum.to_cbor().hex() if output.datum is not None else None,
        out_ref=out_ref,
    )


@pytest.mark.parametrize("name", ["pool_create", "pool_create_token"])
def test_captured_create_evaluates(name: str) -> None:
    fix = fixture(name)
    snapshot = PoolCreateSnapshot.from_capture(fix)
    built = build(build_pool_create, snapshot, slot=fix["invalid_before"])
    assert evaluate(built, fixture_utxos(fix)) == _CREATE


def test_three_pools_created_together_evaluate() -> None:
    fix = fixture("pool_create")
    snapshot = PoolCreateSnapshot.from_capture(fix)
    (offer,) = snapshot.offers
    three = replace(
        snapshot,
        offers=[offer, replace(offer, principal_amount=1_000_000), offer],
    )
    built = build(build_pool_create, three, slot=fix["invalid_before"])
    assert evaluate(built, fixture_utxos(fix)) == _CREATE


def test_a_lender_without_a_stake_key_creates_a_pool() -> None:
    fix = fixture("pool_create")
    snapshot = PoolCreateSnapshot.from_capture(fix)
    lender = Address.decode(snapshot.lender_address)
    unstaked = Address(payment_part=lender.payment_part, network=lender.network)
    built = build(
        build_pool_create,
        replace(snapshot, lender_address=unstaked.encode()),
        slot=fix["invalid_before"],
    )
    assert built.tx.transaction_body.outputs[0].address.staking_part is None
    assert evaluate(built, fixture_utxos(fix)) == _CREATE


@pytest.mark.parametrize("staked", [True, False])
def test_a_created_pool_can_be_cancelled_and_edited(staked: bool) -> None:
    """The second of two pools created together is later cancelled, then edited.

    Both continuations are built against captured cancel and edit fixtures whose
    positions and funding are swapped for the freshly created pool: Ogmios evaluates
    each against the pool as ``additionalUtxo``, so neither ever needs to be mined.
    """
    fix = fixture("pool_create")
    snapshot = PoolCreateSnapshot.from_capture(fix)
    lender = Address.decode(snapshot.lender_address)
    address = (
        snapshot.lender_address
        if staked
        else Address(payment_part=lender.payment_part, network=lender.network).encode()
    )
    (offer,) = snapshot.offers
    two = replace(
        snapshot,
        lender_address=address,
        offers=[offer, replace(offer, principal_amount=1_000_000_000)],
    )
    outputs = build(build_pool_create, two, slot=fix["invalid_before"]).tx
    outputs = outputs.transaction_body.outputs
    # The second offer's pool and manager, at index 1.
    pool = _as_utxo(outputs[2], ("aa" * 32, 0))
    manager = _as_utxo(outputs[3], ("ab" * 32, 0))
    funding = Utxo(
        address=address,
        lovelace=20_000_000,
        assets=[],
        datum=None,
        out_ref=("ac" * 32, 0),
    )

    cfix = fixture("pool_cancel")
    cancel_snapshot = replace(
        PoolCancelSnapshot.from_capture(cfix),
        positions=[PoolPosition(pool, manager)],
        funding=[funding],
        mint_input_ref=funding.out_ref,
    )
    cancel_built = build(
        build_pool_cancel, cancel_snapshot, slot=cfix["invalid_before"]
    )
    assert (
        evaluate(cancel_built, [*fixture_utxos(cfix), pool, manager, funding])
        == _CANCEL
    )

    efix = fixture("pool_edit")
    edit = edited_pool(
        PoolPosition(pool, manager),
        PoolEdit(pool.out_ref, principal_change=-500_000_000),
    )
    edit_snapshot = replace(
        PoolEditSnapshot.from_capture(efix),
        positions=[edit],
        funding=[funding],
    )
    edit_built = build(build_pool_edit, edit_snapshot, slot=efix["invalid_before"])
    assert evaluate(edit_built, [*fixture_utxos(efix), pool, manager, funding]) == _EDIT
