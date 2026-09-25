"""Gated live FluidTokens V4 checks over real dbsync (FLUID_E2E=1).

Doubles as a redeploy watch: the live config datum must equal the checked-in
constants, and every live UTxO carrying a V4 identity token must parse. Skipped unless
``FLUID_E2E=1`` (needs reachable dbsync credentials in the environment / ``.env``).
"""

import os

import pytest
from dotenv import load_dotenv

pytestmark = pytest.mark.skipif(
    os.environ.get("FLUID_E2E") != "1",
    reason="requires live dbsync (FLUID_E2E=1)",
)


@pytest.fixture(scope="module")
def backend():
    load_dotenv()
    from charli3_dendrite.backend.dbsync import DbsyncBackend

    return DbsyncBackend()


@pytest.fixture(scope="module")
def snap(backend):
    from charli3_dendrite.lending.fluidtokens_v4.loader import snapshot

    return snapshot(backend)


def test_live_config_matches_the_constants(backend):
    from charli3_dendrite.lending.fluidtokens_v4.constants import default_config
    from charli3_dendrite.lending.fluidtokens_v4.constants import resolve_config

    assert resolve_config(backend).to_cbor_hex() == default_config().to_cbor_hex()


def test_every_identity_bearing_utxo_parses(backend):
    from charli3_dendrite.lending.fluidtokens_v4.indexing import entity_selectors
    from charli3_dendrite.lending.fluidtokens_v4.indexing import parse_utxo
    from charli3_dendrite.lending.fluidtokens_v4.state import identity_name

    # Kinds without an identity policy (asset, lender and locked-borrower managers)
    # have no token to tell a genuine UTxO from one anyone paid to the address.
    for selector in entity_selectors().values():
        if selector.identity_policy is None:
            continue
        rows = backend.get_pool_utxos(addresses=[selector.address], historical=False)
        for info in rows:
            if identity_name(info.assets, selector.identity_policy) is not None:
                assert parse_utxo(info) is not None, (selector.kind, info.tx_hash)


def test_snapshot_links_the_live_protocol(snap):
    assert snap.pools, "no live pools"
    assert snap.loans, "no live loans"
    assert {p.pool_id for p in snap.pools} == {m.pool_id for m in snap.pool_managers}
    for loan in snap.loans:
        # Debt falls below the principal as installments are repaid, so only a
        # non-negative debt is invariant; the API cross-check pins the value.
        assert loan.current_debt() >= 0
    bonds = {name for m in snap.lender_managers for name in m.lender_bond_names}
    assert bonds
