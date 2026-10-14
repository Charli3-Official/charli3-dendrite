"""Gated: a live recast resolves only while its action can run on chain.

The action can run once its reward account is registered and a reference script for
it is published; the current config names a recast action with neither.
"""

from __future__ import annotations

import pytest

from charli3_dendrite.lending.fluidtokens_v4.transactions.recast import RecastSnapshot
from charli3_dendrite.lending.fluidtokens_v4.transactions.repay import RepaySnapshot
from charli3_dendrite.lending.fluidtokens_v4.transactions.resolve import config_datum
from charli3_dendrite.lending.fluidtokens_v4.transactions.resolve import (
    resolve_config_utxo,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.resolve import (
    resolve_script,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.resolve import (
    reward_account_registered,
)
from tests.lending.fluidtokens_v4.transactions.replay import fixture
from tests.lending.fluidtokens_v4.transactions.replay import needs_dbsync

pytestmark = needs_dbsync


@pytest.fixture(scope="module")
def backend():  # noqa: ANN201
    from charli3_dendrite.backend.dbsync import DbsyncBackend

    return DbsyncBackend()


def test_live_recast_follows_the_action_availability(backend) -> None:  # noqa: ANN001
    capture = RepaySnapshot.from_capture(fixture("repay_single"))
    (position,) = capture.positions
    action = config_datum(resolve_config_utxo(backend)).loan_recast_action_script_hash

    def resolve() -> RecastSnapshot:
        return RecastSnapshot.from_backend(
            backend,
            recasts=[(position.out_ref, 10_000_000)],
            borrower_address=position.borrower_bond.address,
            funding=capture.funding,
            valid_from=capture.valid_from,
            valid_to=capture.valid_to,
            allow_spent=True,
        )

    registered = reward_account_registered(backend, action.hex())
    try:
        resolve_script(backend, action)
    except ValueError:
        published = False
    else:
        published = True
    if registered and published:
        assert resolve().positions[0].amount_paid == 10_000_000
    else:
        with pytest.raises(ValueError, match="recast action .* is unavailable") as err:
            resolve()
        assert ("not registered" in str(err.value)) == (not registered)
        assert ("no reference script" in str(err.value)) == (not published)


def test_the_other_v4_withdraw_accounts_are_registered(backend) -> None:  # noqa: ANN001
    scripts = config_datum(resolve_config_utxo(backend))
    for script_hash in (
        scripts.pool_policy_id,
        scripts.pool_borrow_action_script_hash,
        scripts.loan_policy_id,
        scripts.loan_repay_action_script_hash,
        scripts.loan_change_collateral_action_script_hash,
        scripts.repayment_policy_id,
    ):
        assert reward_account_registered(backend, script_hash.hex())
