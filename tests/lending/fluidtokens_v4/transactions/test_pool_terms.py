"""Lender terms, the datum of a new pool, and the ADA a pool must keep."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from pycardano import Address
from pycardano import RawCBOR
from pycardano import TransactionOutput
from pycardano import VerificationKeyHash

from charli3_dendrite.lending.fluidtokens.datums import InterestOnRemainingPrincipal
from charli3_dendrite.lending.fluidtokens.datums import NoLiquidationDutchAuctionClaim
from charli3_dendrite.lending.fluidtokens.transactions.utxos import utxo_from_dict
from charli3_dendrite.lending.fluidtokens.transactions.utxos import utxo_value
from charli3_dendrite.lending.fluidtokens_v4 import constants as c
from charli3_dendrite.lending.fluidtokens_v4.datums import Asset
from charli3_dendrite.lending.fluidtokens_v4.datums import LenderManagerDatum
from charli3_dendrite.lending.fluidtokens_v4.datums import PoolDatum
from charli3_dendrite.lending.fluidtokens_v4.datums import PoolManagerDatum
from charli3_dendrite.lending.fluidtokens_v4.transactions.common import min_ada
from charli3_dendrite.lending.fluidtokens_v4.transactions.common import pool_nft_name
from charli3_dendrite.lending.fluidtokens_v4.transactions.lender_bond import (
    lender_bond_datum,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.pool_terms import (
    LenderTerms,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.pool_terms import (
    new_pool_datum,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.pool_terms import (
    pool_min_ada,
)
from charli3_dendrite.lending.units import constr
from tests.lending.fluidtokens_v4.transactions.replay import fixture

_ENTITIES = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "entities.json").read_text(),
)


def _pool(name: str) -> tuple[PoolDatum, str, bytes]:
    """The datum, address and NFT name of the pool a captured create makes."""
    pool = next(
        utxo_from_dict(u)
        for u in fixture(name)["outputs"]
        if any(p == c.POOL_POLICY for p, _, _ in u["assets"])
    )
    return PoolDatum.from_cbor(pool.datum), pool.address, pool_nft_name(pool)


def _owner(manager: PoolManagerDatum) -> bytes:
    """The key hash of a pool manager owned by a signature."""
    _, (key_hash,) = constr(manager.pool_owner_auth)
    return bytes(key_hash)


def _manager(name: str) -> PoolManagerDatum:
    return PoolManagerDatum.from_cbor(
        next(
            u["datum"]
            for u in fixture(name)["outputs"]
            if any(p == c.POOL_MANAGER_POLICY for p, _, _ in u["assets"])
        ),
    )


def test_terms_round_trip_every_live_pool() -> None:
    for rec in _ENTITIES["pool"]:
        datum = PoolDatum.from_cbor(rec["datum_cbor"])
        assert (
            LenderTerms.from_pool_datum(datum).apply(datum).to_cbor_hex()
            == rec["datum_cbor"]
        )


@pytest.mark.parametrize("name", ["pool_create", "pool_create_token"])
def test_new_pool_datum_reproduces_the_captured_pool(name: str) -> None:
    datum, address, pool_id = _pool(name)
    manager = _manager(name)
    lender = Address(
        payment_part=VerificationKeyHash(_owner(manager)),
        staking_part=Address.decode(address).staking_part,
    )
    built = new_pool_datum(
        LenderTerms.from_pool_datum(datum),
        lender_address=lender.encode(),
        pool_id=pool_id,
        pool_manager=manager,
        convert_liquidations=True,
        liquidation_fee_per_mille=40,
    )
    assert built.to_cbor_hex() == datum.to_cbor_hex()


def test_new_pool_datum_commits_to_its_own_name() -> None:
    datum, address, pool_id = _pool("pool_create")
    lender = Address(
        payment_part=VerificationKeyHash(b"\x01" * 28),
        staking_part=Address.decode(address).staking_part,
    ).encode()
    terms = LenderTerms.from_pool_datum(datum)
    kwargs = {
        "lender_address": lender,
        "pool_manager": _manager("pool_create"),
        "convert_liquidations": True,
        "liquidation_fee_per_mille": 40,
    }
    first = new_pool_datum(terms, pool_id=pool_id, **kwargs)
    second = new_pool_datum(terms, pool_id=b"\x01" + pool_id[1:], **kwargs)
    assert first.lender_bond_inline_datum_hash != second.lender_bond_inline_datum_hash


@pytest.mark.parametrize("staked", [True, False])
def test_a_borrow_reproduces_the_lender_bond_a_new_pool_commits_to(
    staked: bool,
) -> None:
    datum, address, pool_id = _pool("pool_create")
    manager = _manager("pool_create")
    lender = Address(
        payment_part=VerificationKeyHash(_owner(manager)),
        staking_part=Address.decode(address).staking_part if staked else None,
    ).encode()
    built = new_pool_datum(
        LenderTerms.from_pool_datum(datum),
        lender_address=lender,
        pool_id=pool_id,
        pool_manager=manager,
        convert_liquidations=False,
        liquidation_fee_per_mille=25,
    )
    committed = LenderManagerDatum.from_cbor(
        lender_bond_datum(built, pool_id=pool_id, pool_manager=manager),
    )
    assert committed.liquidation_fee_per_mille == 25  # noqa: PLR2004
    assert committed.pool_id == pool_id


def _terms() -> LenderTerms:
    datum, _, _ = _pool("pool_create")
    return LenderTerms.from_pool_datum(datum)


@pytest.mark.parametrize(
    ("change", "match"),
    [
        ({"min_collateral": []}, "index-aligned"),
        (
            {
                "collateral_options": [],
                "min_collateral": [],
                "min_collateral_divider": [],
            },
            "at least one collateral option",
        ),
        ({"min_collateral_divider": [0]}, "positive"),
        ({"min_collateral": [0]}, "positive"),
    ],
)
def test_terms_no_borrower_could_take_up_are_refused(change: dict, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        replace(_terms(), **change).check()


def test_a_pool_cannot_lend_pool_nfts() -> None:
    terms = _terms()
    common = replace(
        terms.common_data,
        principal_asset=Asset(policy_id=bytes.fromhex(c.POOL_POLICY), asset_name=b""),
    )
    with pytest.raises(ValueError, match="own pool NFTs"):
        replace(terms, common_data=common).check()


def test_an_installment_loan_needs_installments() -> None:
    terms = _terms()
    amortized = replace(
        terms.common_data,
        repayment_mode=InterestOnRemainingPrincipal(max_possible_recasts=0),
        total_installments=0,
    )
    with pytest.raises(ValueError, match="at least one installment"):
        replace(terms, common_data=amortized).check()
    replace(
        terms,
        common_data=replace(amortized, total_installments=12),
    ).check()


def test_a_zero_rate_amortized_pool_cannot_be_repaid() -> None:
    terms = _terms()
    amortized = replace(
        terms.common_data,
        repayment_mode=InterestOnRemainingPrincipal(max_possible_recasts=0),
        total_installments=12,
        interest_rate=0,
    )
    with pytest.raises(ValueError, match="zero-rate amortized loan"):
        replace(terms, common_data=amortized).check()


def test_a_non_zero_rate_amortized_pool_with_installments_passes() -> None:
    terms = _terms()
    amortized = replace(
        terms.common_data,
        repayment_mode=InterestOnRemainingPrincipal(max_possible_recasts=0),
        total_installments=12,
        interest_rate=777,
    )
    replace(terms, common_data=amortized).check()


def test_a_dutch_auction_claim_pool_is_refused() -> None:
    terms = _terms()
    dutch = replace(
        terms.common_data,
        liquidation_mode=NoLiquidationDutchAuctionClaim(),
    )
    with pytest.raises(ValueError, match="Dutch auction is disabled"):
        replace(terms, common_data=dutch).check()


@pytest.mark.parametrize("name", ["pool_create", "pool_create_token"])
def test_pool_min_ada_covers_the_pool_at_any_principal(name: str) -> None:
    datum, address, pool_id = _pool(name)
    floor = pool_min_ada(Address.decode(address), datum, pool_id=pool_id)
    principal = datum.common_data.principal_asset.unit()
    for amount in (0, 1, 2**32, 2**63 - 1):
        assets = [(c.POOL_POLICY, pool_id.hex(), 1)]
        lovelace = floor
        if principal == "lovelace":
            lovelace += amount
        elif amount:
            assets.append((principal[:56], principal[56:], amount))
        output = TransactionOutput(
            Address.decode(address),
            utxo_value(lovelace, assets),
            datum=RawCBOR(bytes.fromhex(datum.to_cbor_hex())),
        )
        assert min_ada(output) <= lovelace
