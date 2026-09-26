"""The lender-bond datum a borrow reproduces from its pool and pool manager."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from pycardano import Address

from charli3_dendrite.lending.fluidtokens.transactions._common import plutus_address
from charli3_dendrite.lending.fluidtokens.transactions.borrow_terms import (
    lender_bond_datum_matches,
)
from charli3_dendrite.lending.fluidtokens.transactions.utxos import (
    loan_id_from_out_ref,
)
from charli3_dendrite.lending.fluidtokens.transactions.utxos import utxo_from_dict
from charli3_dendrite.lending.fluidtokens_v4 import constants as c
from charli3_dendrite.lending.fluidtokens_v4.datums import PoolDatum
from charli3_dendrite.lending.fluidtokens_v4.datums import PoolManagerDatum
from charli3_dendrite.lending.fluidtokens_v4.transactions.lender_bond import (
    lender_bond_datum,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.lender_bond import (
    sends_bonds_to_lender_manager,
)
from tests.lending.fluidtokens_v4.transactions.replay import fixture

_ENTITIES = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "entities.json").read_text(),
)


def _pool_id(assets: dict[str, int]) -> str:
    return next(u[56:] for u in assets if u.startswith(c.POOL_POLICY))


def _managers() -> dict[str, PoolManagerDatum]:
    """Pool-manager datums by pool NFT name (a manager NFT shares its pool's name)."""
    return {
        next(u[56:] for u in rec["assets"] if u.startswith(c.POOL_MANAGER_POLICY)): (
            PoolManagerDatum.from_cbor(rec["datum_cbor"])
        )
        for rec in _ENTITIES["pool_manager"]
    }


def test_every_live_pool_commits_a_reconstructible_datum() -> None:
    managers = _managers()
    for rec in _ENTITIES["pool"]:
        datum = PoolDatum.from_cbor(rec["datum_cbor"])
        pool_id = _pool_id(rec["assets"])
        preimage = lender_bond_datum(
            datum,
            pool_id=bytes.fromhex(pool_id),
            pool_manager=managers[pool_id],
        )
        assert lender_bond_datum_matches(datum, preimage)


@pytest.mark.parametrize("name", ["borrow_single", "borrow_multi"])
def test_reconstruction_equals_the_captured_lender_bonds(name: str) -> None:
    fix = fixture(name)
    managers = _managers()
    pools = [
        utxo_from_dict(u)
        for u in fix["inputs"]
        if any(p == c.POOL_POLICY for p, _, _ in u["assets"])
    ]
    for pool in pools:
        loan_id = loan_id_from_out_ref(pool.out_ref).hex()
        captured = next(
            u["datum"]
            for u in fix["outputs"]
            if any(
                p == c.LENDER_BOND_POLICY and n == loan_id for p, n, _ in u["assets"]
            )
        )
        pool_id = next(n for p, n, _ in pool.assets if p == c.POOL_POLICY)
        assert (
            lender_bond_datum(
                PoolDatum.from_cbor(pool.datum),
                pool_id=bytes.fromhex(pool_id),
                pool_manager=managers[pool_id],
            )
            == captured
        )


def test_a_pool_paying_bonds_to_a_wallet_is_not_supported() -> None:
    rec = _ENTITIES["pool"][0]
    wallet = Address.decode(
        next(
            u["address"]
            for u in fixture("borrow_single")["inputs"]
            if not u.get("datum")
        ),
    )
    datum = replace(
        PoolDatum.from_cbor(rec["datum_cbor"]),
        lender_bond_address=plutus_address(wallet),
    )
    assert not sends_bonds_to_lender_manager(datum)
    with pytest.raises(ValueError, match="does not support such pools"):
        lender_bond_datum(
            datum,
            pool_id=bytes.fromhex(_pool_id(rec["assets"])),
            pool_manager=_managers()[_pool_id(rec["assets"])],
        )


def test_no_setting_matching_the_commitment_raises() -> None:
    rec = _ENTITIES["pool"][0]
    datum = replace(
        PoolDatum.from_cbor(rec["datum_cbor"]),
        lender_bond_inline_datum_hash=b"\x00" * 32,
    )
    with pytest.raises(ValueError, match="no lender-manager setting"):
        lender_bond_datum(
            datum,
            pool_id=bytes.fromhex(_pool_id(rec["assets"])),
            pool_manager=_managers()[_pool_id(rec["assets"])],
        )
