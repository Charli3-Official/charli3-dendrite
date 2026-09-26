"""Offline: forward-built V4 borrows reproduce the captured mainnet borrows.

Pools that lend a token are built from captured pool datums with stand-in signed
prices.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import replace
from fractions import Fraction
from math import ceil
from pathlib import Path

import cbor2
import pytest
from pycardano import Address
from pycardano import RawPlutusData
from pycardano import Transaction
from pycardano import TransactionBuilder
from pycardano import TransactionOutput

from charli3_dendrite.lending.fluidtokens.datums import Asset
from charli3_dendrite.lending.fluidtokens.oracles.witness import (
    build_oracle_reward_cbor,
)
from charli3_dendrite.lending.fluidtokens.transactions.borrow_terms import (
    min_collateral_amount,
)
from charli3_dendrite.lending.fluidtokens.transactions.utxos import Utxo
from charli3_dendrite.lending.fluidtokens.transactions.utxos import script_ref_by_hash
from charli3_dendrite.lending.fluidtokens.transactions.utxos import utxo_from_dict
from charli3_dendrite.lending.fluidtokens.transactions.utxos import utxo_value
from charli3_dendrite.lending.fluidtokens_v4 import constants as c
from charli3_dendrite.lending.fluidtokens_v4.datums import LoanDatum
from charli3_dendrite.lending.fluidtokens_v4.datums import PoolDatum
from charli3_dendrite.lending.fluidtokens_v4.datums import PoolManagerDatum
from charli3_dendrite.lending.fluidtokens_v4.state import collateral_asset_unit
from charli3_dendrite.lending.fluidtokens_v4.transactions import resolve
from charli3_dendrite.lending.fluidtokens_v4.transactions.borrow import BorrowSnapshot
from charli3_dendrite.lending.fluidtokens_v4.transactions.borrow import PoolBorrow
from charli3_dendrite.lending.fluidtokens_v4.transactions.borrow import build_borrow
from charli3_dendrite.lending.fluidtokens_v4.transactions.common import min_ada
from charli3_dendrite.lending.fluidtokens_v4.transactions.common import script_hash_of
from charli3_dendrite.lending.fluidtokens_v4.transactions.oracle import OracleWitness
from charli3_dendrite.lending.fluidtokens_v4.transactions.redeemers import BorrowData
from charli3_dendrite.lending.fluidtokens_v4.transactions.redeemers import (
    PoolBorrowActionWithdrawRedeemer,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.repay import RepaySnapshot
from charli3_dendrite.lending.fluidtokens_v4.transactions.repay import build_repay
from charli3_dendrite.lending.transactions.infra import EvalContext
from charli3_dendrite.lending.units import script_payment_address
from charli3_dendrite.utility import slot_to_posix_ms
from tests.lending.fluidtokens_v4.transactions.replay import build
from tests.lending.fluidtokens_v4.transactions.replay import captured_output_view
from tests.lending.fluidtokens_v4.transactions.replay import captured_redeemers
from tests.lending.fluidtokens_v4.transactions.replay import fixture
from tests.lending.fluidtokens_v4.transactions.replay import output_view
from tests.lending.fluidtokens_v4.transactions.replay import redeemers

CAPTURES = ["borrow_single", "borrow_multi"]
_ENTITIES = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "entities.json").read_text(),
)
_USDM = "c48cbb3d5e57ed56e276bc45f99ab39abe94e6cd7ac39fb402da47ad0014df105553444d"
_STRIKE = "f13ac4d66b3ee19a6aa0f2a22298737bd907cc95121662fc971b5275535452494b45"
# Lovelace per smallest USDM unit.
_USDM_PRICE = (386_452_445, 100_000_000)


class _StubProvider:
    """A stub provider that raises if fetch_oracle_witness is called."""

    def fetch_oracle_witness(self, **_):  # noqa: ANN002
        raise AssertionError("the price registry must not be called")


@pytest.mark.parametrize("name", CAPTURES)
def test_borrow_redeemers_are_byte_exact(name: str) -> None:
    fix = fixture(name)
    built = build(
        build_borrow, BorrowSnapshot.from_capture(fix), slot=fix["invalid_before"]
    )
    assert redeemers(built.tx) == captured_redeemers(fix)


@pytest.mark.parametrize("name", CAPTURES)
def test_borrow_outputs_and_mints_are_byte_exact(name: str) -> None:
    fix = fixture(name)
    snapshot = BorrowSnapshot.from_capture(fix)
    body = build(build_borrow, snapshot, slot=fix["invalid_before"]).tx.transaction_body
    legs = len(snapshot.legs)
    # Continuing pools, loans, borrower bonds, lender bonds; the wallet change follows.
    assert [output_view(o) for o in body.outputs[: 4 * legs]] == [
        captured_output_view(o) for o in fix["outputs"][: 4 * legs]
    ]
    minted = sorted(
        [bytes(p).hex(), n.payload.hex(), q]
        for p, names in body.mint.items()
        for n, q in names.items()
    )
    assert minted == sorted([p, n, int(q)] for p, n, q in fix["mints"])


def test_borrow_records_the_funding_for_ogmios() -> None:
    fix = fixture("borrow_single")
    snapshot = BorrowSnapshot.from_capture(fix)
    build(build_borrow, snapshot, slot=fix["invalid_before"])
    assert len(snapshot.additional_utxo()) == len(snapshot.funding)


def test_borrow_outputs_must_come_first() -> None:
    fix = fixture("borrow_single")
    tx_builder = TransactionBuilder(EvalContext(last_block_slot=fix["invalid_before"]))
    tx_builder.add_output(
        TransactionOutput(Address.decode(fix["outputs"][-1]["address"]), 2_000_000),
    )
    with pytest.raises(ValueError, match="must come first"):
        build_borrow(tx_builder, snapshot=BorrowSnapshot.from_capture(fix))


def _fixture_backend(
    monkeypatch: pytest.MonkeyPatch,
    fix: dict,
    *,
    pools: list[Utxo] | None = None,
) -> None:
    """Resolve pools, config, scripts and pool managers from ``fix``."""
    inputs = pools or [utxo_from_dict(u) for u in fix["inputs"]]
    refs = [utxo_from_dict(u) for u in fix["ref_inputs"]]
    managers = {
        next(u[56:] for u in rec["assets"] if u.startswith(c.POOL_MANAGER_POLICY)): (
            PoolManagerDatum.from_cbor(rec["datum_cbor"])
        )
        for rec in _ENTITIES["pool_manager"]
    }
    monkeypatch.setattr(
        resolve,
        "resolve_utxo",
        lambda _backend, out_ref, **_: next(u for u in inputs if u.out_ref == out_ref),
    )
    monkeypatch.setattr(
        resolve,
        "resolve_pool_manager",
        lambda _backend, pool_id: managers[pool_id.hex()],
    )
    monkeypatch.setattr(
        resolve,
        "resolve_config_utxo",
        lambda _backend: next(
            u for u in refs if u.holds(c.CONFIG_NFT_POLICY, c.CONFIG_NFT_NAME)
        ),
    )
    monkeypatch.setattr(
        resolve,
        "resolve_script",
        lambda _backend, h: script_ref_by_hash(
            refs,
            h.hex() if isinstance(h, bytes) else h,
        ),
    )


def _from_backend(capture: BorrowSnapshot, **overrides: object) -> BorrowSnapshot:
    kwargs: dict = {
        "borrows": [
            PoolBorrow(
                pool_out_ref=leg.out_ref,
                principal_amount=leg.principal_amount,
                chosen_collateral_index=leg.chosen_collateral_index,
                collateral_amount=leg.collateral_amount,
            )
            for leg in capture.legs
        ],
        "borrower_address": capture.borrower_address,
        "oracles": capture.oracles,
        "funding": capture.funding,
        "valid_from": capture.valid_from,
        "valid_to": capture.valid_to,
    }
    return BorrowSnapshot.from_backend(object(), **(kwargs | overrides))  # type: ignore[arg-type]


@pytest.mark.parametrize("name", CAPTURES)
def test_from_backend_reproduces_the_capture(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    fix = fixture(name)
    _fixture_backend(monkeypatch, fix)
    capture = BorrowSnapshot.from_capture(fix)
    snapshot = _from_backend(capture)
    built = build(build_borrow, snapshot, slot=fix["invalid_before"])
    assert redeemers(built.tx) == captured_redeemers(fix)
    for resolved, captured in zip(snapshot.ordered_legs, capture.ordered_legs):
        assert resolved.lender_bond_datum == captured.lender_bond_datum
        assert resolved.lender_bond_lovelace == captured.lender_bond_lovelace
        assert resolved.borrower_bond_lovelace == captured.borrower_bond_lovelace
        # FluidTokens pads each loan to 3 ADA; the least it needs is less.
        assert resolved.loan_lovelace < captured.loan_lovelace


def test_from_backend_defaults_to_the_least_collateral(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fix = fixture("borrow_single")
    _fixture_backend(monkeypatch, fix)
    capture = BorrowSnapshot.from_capture(fix)
    (leg,) = capture.legs
    snapshot = _from_backend(
        capture,
        borrows=[PoolBorrow(leg.out_ref, leg.principal_amount, 0)],
    )
    reward = capture.oracles[0].reward
    assert snapshot.legs[0].collateral_amount == min_collateral_amount(
        leg.pool_datum,
        chosen_collateral_index=0,
        principal_amount=leg.principal_amount,
        price_num=reward.price_num,
        price_den=reward.price_den,
    )
    with pytest.raises(ValueError, match="below the pool's minimum"):
        _from_backend(
            capture,
            borrows=[
                PoolBorrow(
                    leg.out_ref,
                    leg.principal_amount,
                    0,
                    snapshot.legs[0].collateral_amount - 1,
                ),
            ],
        )


def test_from_backend_refuses_what_the_pool_cannot_lend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fix = fixture("borrow_single")
    capture = BorrowSnapshot.from_capture(fix)
    (leg,) = capture.legs
    _fixture_backend(monkeypatch, fix)
    with pytest.raises(ValueError, match="lends at most"):
        _from_backend(capture, borrows=[PoolBorrow(leg.out_ref, leg.pool.lovelace + 1)])
    with pytest.raises(ValueError, match="below the"):
        _from_backend(capture, borrows=[PoolBorrow(leg.out_ref, leg.pool.lovelace)])
    with pytest.raises(ValueError, match="positive"):
        _from_backend(capture, borrows=[PoolBorrow(leg.out_ref, 0)])


def test_from_backend_refuses_permissioned_pools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fix = fixture("borrow_single")
    capture = BorrowSnapshot.from_capture(fix)
    (leg,) = capture.legs
    gated = replace(
        leg.pool,
        datum=replace(
            PoolDatum.from_cbor(leg.pool.datum),
            permissioned_condition_script_hash=b"\x01" * 28,
        ).to_cbor_hex(),
    )
    _fixture_backend(monkeypatch, fix, pools=[gated])
    with pytest.raises(NotImplementedError, match="permissioned"):
        _from_backend(capture)


def test_from_backend_needs_a_price_for_oracle_priced_pools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fix = fixture("borrow_single")
    _fixture_backend(monkeypatch, fix)
    with pytest.raises(ValueError, match="no oracle witness"):
        _from_backend(BorrowSnapshot.from_capture(fix), oracles=[])


def _entity_pool(principal: str, collateral: str) -> tuple[Utxo, int]:
    """The first captured pool lending ``principal`` against ``collateral``.

    Returns the pool and the index of that collateral option.
    """
    for rec in _ENTITIES["pool"]:
        datum = PoolDatum.from_cbor(rec["datum_cbor"])
        units = [collateral_asset_unit(o) for o in datum.collateral_options]
        lent = datum.common_data.principal_asset.unit()
        if lent == principal and collateral in units and rec["assets"].get(lent):
            tx_hash, index = rec["out_ref"].split("#")
            pool = Utxo(
                address=rec["address"],
                lovelace=rec["assets"]["lovelace"],
                assets=[
                    (u[:56], u[56:], q)
                    for u, q in rec["assets"].items()
                    if u != "lovelace"
                ],
                datum=rec["datum_cbor"],
                out_ref=(tx_hash, int(index)),
            )
            return pool, units.index(collateral)
    raise AssertionError(f"no captured pool lends {principal} against {collateral}")


def _signed_price(
    capture: BorrowSnapshot,
    oracle_token: Asset,
    unit: str,
    price: tuple[int, int],
) -> OracleWitness:
    """A stand-in signed price of ``unit`` covering the capture's validity window.

    The feed holds ``oracle_token`` at the address of a stand-in oracle validator; the
    signature is a placeholder, which only an evaluation would check.
    """
    name = oracle_token.asset_name
    script_ref = Utxo(
        address=capture.config.address,
        lovelace=10_000_000,
        assets=[],
        datum=None,
        ref_script=(b"stand-in oracle validator for " + name).hex(),
        out_ref=(hashlib.blake2b(name, digest_size=32).hexdigest(), 0),
    )
    feed = Utxo(
        address=script_payment_address(script_hash_of(script_ref)),
        lovelace=2_000_000,
        assets=[(oracle_token.policy_id.hex(), name.hex(), 1)],
        datum=None,
        out_ref=(hashlib.blake2b(name, digest_size=32).hexdigest(), 1),
    )
    reward = build_oracle_reward_cbor(
        valid_from_ms=slot_to_posix_ms(capture.valid_from),
        valid_to_ms=slot_to_posix_ms(capture.valid_to),
        collateral_policy=unit[:56],
        collateral_name=unit[56:],
        price_num=price[0],
        price_den=price[1],
        signatures=[(bytes(64), 0)],
    )
    return OracleWitness(feed=feed, script_ref=script_ref, reward_cbor=reward)


def _usdm_price(capture: BorrowSnapshot, pool: Utxo) -> OracleWitness:
    """A stand-in signed USDM price served by the pool's principal oracle token."""
    datum = PoolDatum.from_cbor(pool.datum)
    return _signed_price(
        capture,
        datum.common_data.principal_oracle_asset,
        _USDM,
        _USDM_PRICE,
    )


def _floor(pool: Utxo, index: int, principal: int, collateral: Fraction) -> int:
    """principal x the USDM price / the option's LTV / the collateral price, up."""
    datum = PoolDatum.from_cbor(pool.datum)
    ltv = Fraction(datum.min_collateral[index], datum.min_collateral_divider[index])
    return ceil(principal * Fraction(*_USDM_PRICE) / ltv / collateral)


def _borrow_data(tx: Transaction) -> list[BorrowData]:
    """The borrow action's withdraw redeemer entries, one per leg in input order."""
    position = sorted(_withdraw_scripts(tx)).index(c.POOL_BORROW_ACTION_SKH)
    return PoolBorrowActionWithdrawRedeemer.from_cbor(
        next(
            cbor
            for purpose, index, cbor in redeemers(tx)
            if purpose == "reward" and index == position
        ),
    ).actions_for_each_input


def _withdraw_scripts(tx: Transaction) -> set[str]:
    return {
        Address.from_primitive(account).staking_part.payload.hex()
        for account in tx.transaction_body.withdraws
    }


def _reference_inputs(tx: Transaction) -> list[tuple[str, int]]:
    """The out-refs of the reference inputs, in ledger order."""
    return sorted(
        (bytes(i.transaction_id).hex(), i.index)
        for i in tx.transaction_body.reference_inputs
    )


def _principal_left(pool: Utxo, output: TransactionOutput) -> int:
    """The USDM a continuing pool output holds, checking its ADA is unchanged."""
    assert output.amount.coin == pool.lovelace
    return next(q for p, n, q in _assets(output) if p + n == _USDM)


@pytest.mark.parametrize("share", [100, 10**9], ids=["1%", "one-unit"])
def test_from_backend_borrows_a_token_against_ada(
    monkeypatch: pytest.MonkeyPatch,
    share: int,
) -> None:
    fix = fixture("borrow_single")
    capture = BorrowSnapshot.from_capture(fix)
    pool, index = _entity_pool(_USDM, "lovelace")
    _fixture_backend(monkeypatch, fix, pools=[pool])
    usdm = _usdm_price(capture, pool)
    lent = next(q for p, n, q in pool.assets if p + n == _USDM)
    principal = max(lent // share, 1)
    snapshot = _from_backend(
        capture,
        borrows=[PoolBorrow(pool.out_ref, principal, index)],
        oracles=[usdm],
    )
    (leg,) = snapshot.legs
    assert snapshot.oracle_for(leg) is None
    assert snapshot.principal_oracle_for(leg) is usdm
    assert leg.collateral_amount == _floor(pool, index, principal, Fraction(1))

    tx = build(build_borrow, snapshot, slot=fix["invalid_before"]).tx
    (data,) = _borrow_data(tx)
    assert _reference_inputs(tx)[data.principal_oracle_ref_input_index] == (
        usdm.feed.out_ref
    )
    # ADA collateral is priced 1:1 and its feed index is never read.
    assert data.chosen_collateral_oracle_ref_input_index == 0
    assert _withdraw_scripts(tx) == {
        c.POOL_POLICY,
        c.POOL_BORROW_ACTION_SKH,
        usdm.script_hash,
    }
    continuing, loan = tx.transaction_body.outputs[:2]
    assert _principal_left(pool, continuing) == lent - principal
    # The loan's ADA is its collateral: lovelace and the loan NFT, nothing else.
    assert _assets(loan) == [(c.LOAN_POLICY, leg.loan_id.hex(), 1)]
    assert loan.amount.coin == leg.loan_lovelace >= leg.collateral_amount
    widest = TransactionOutput(
        loan.address,
        loan.amount,
        datum=replace(
            LoanDatum.from_cbor(loan.datum.to_cbor()),
            repaid_installments=2**32 - 1,
            done_recasts=2**32 - 1,
        ),
    )
    if leg.collateral_amount >= min_ada(widest):
        assert leg.loan_lovelace == leg.collateral_amount
    else:
        # Less collateral than an output's minimum ADA is topped up to that minimum.
        assert leg.loan_lovelace == min_ada(widest)


def test_from_backend_borrows_a_token_against_a_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fix = fixture("borrow_single")
    capture = BorrowSnapshot.from_capture(fix)
    (strike,) = capture.oracles
    pool, index = _entity_pool(_USDM, _STRIKE)
    _fixture_backend(monkeypatch, fix, pools=[pool])
    usdm = _usdm_price(capture, pool)
    lent = next(q for p, n, q in pool.assets if p + n == _USDM)
    principal = lent // 100
    snapshot = _from_backend(
        capture,
        borrows=[PoolBorrow(pool.out_ref, principal, index)],
        oracles=[strike, usdm],
    )
    (leg,) = snapshot.legs
    assert snapshot.oracle_for(leg) is strike
    assert snapshot.principal_oracle_for(leg) is usdm
    assert leg.collateral_amount == _floor(pool, index, principal, strike.price)

    tx = build(build_borrow, snapshot, slot=fix["invalid_before"]).tx
    (data,) = _borrow_data(tx)
    refs = _reference_inputs(tx)
    assert refs[data.principal_oracle_ref_input_index] == usdm.feed.out_ref
    assert refs[data.chosen_collateral_oracle_ref_input_index] == strike.feed.out_ref
    assert _withdraw_scripts(tx) == {
        c.POOL_POLICY,
        c.POOL_BORROW_ACTION_SKH,
        usdm.script_hash,
        strike.script_hash,
    }
    continuing, loan = tx.transaction_body.outputs[:2]
    assert _principal_left(pool, continuing) == lent - principal
    assert sorted(_assets(loan)) == sorted(
        [
            (c.LOAN_POLICY, leg.loan_id.hex(), 1),
            (_STRIKE[:56], _STRIKE[56:], leg.collateral_amount),
        ],
    )


def test_legs_share_the_signed_price_of_a_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fix = fixture("borrow_single")
    capture = BorrowSnapshot.from_capture(fix)
    (strike,) = capture.oracles
    against_ada, ada_index = _entity_pool(_USDM, "lovelace")
    against_strike, strike_index = _entity_pool(_USDM, _STRIKE)
    _fixture_backend(monkeypatch, fix, pools=[against_ada, against_strike])
    usdm = _usdm_price(capture, against_ada)
    assert usdm.serves(
        PoolDatum.from_cbor(against_strike.datum).common_data.principal_oracle_asset,
    )
    snapshot = _from_backend(
        capture,
        borrows=[
            PoolBorrow(against_ada.out_ref, 1_000_000, ada_index),
            PoolBorrow(against_strike.out_ref, 1_000_000, strike_index),
        ],
        oracles=[strike, usdm],
    )
    tx = build(build_borrow, snapshot, slot=fix["invalid_before"]).tx
    refs = _reference_inputs(tx)
    legs = _borrow_data(tx)
    assert [data.pool_id for data in legs] == [
        leg.pool_id for leg in snapshot.ordered_legs
    ]
    for leg, data in zip(snapshot.ordered_legs, legs):
        assert refs[data.principal_oracle_ref_input_index] == usdm.feed.out_ref
        collateral = snapshot.oracle_for(leg)
        assert data.chosen_collateral_oracle_ref_input_index == (
            0 if collateral is None else refs.index(collateral.feed.out_ref)
        )
    # One signed price per token, however many legs it serves.
    assert _withdraw_scripts(tx) == {
        c.POOL_POLICY,
        c.POOL_BORROW_ACTION_SKH,
        usdm.script_hash,
        strike.script_hash,
    }


def test_a_borrow_mixes_ada_and_token_principal_legs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fix = fixture("borrow_single")
    capture = BorrowSnapshot.from_capture(fix)
    (strike,) = capture.oracles
    (lends_ada,) = capture.legs
    lends_usdm, ada_index = _entity_pool(_USDM, "lovelace")
    _fixture_backend(monkeypatch, fix, pools=[lends_ada.pool, lends_usdm])
    usdm = _usdm_price(capture, lends_usdm)
    lent = next(q for p, n, q in lends_usdm.assets if p + n == _USDM)
    snapshot = _from_backend(
        capture,
        borrows=[
            PoolBorrow(
                lends_ada.out_ref,
                lends_ada.principal_amount,
                lends_ada.chosen_collateral_index,
            ),
            PoolBorrow(lends_usdm.out_ref, 1_000_000, ada_index),
        ],
        oracles=[strike, usdm],
    )
    tx = build(build_borrow, snapshot, slot=fix["invalid_before"]).tx
    refs = _reference_inputs(tx)
    legs = snapshot.ordered_legs
    entries = _borrow_data(tx)
    assert [data.pool_id for data in entries] == [leg.pool_id for leg in legs]
    by_pool = {
        leg.out_ref: (i, data) for i, (leg, data) in enumerate(zip(legs, entries))
    }
    outputs = tx.transaction_body.outputs

    # ADA principal (priced 1:1, placeholder index) against STRIKE from its feed.
    i, data = by_pool[lends_ada.out_ref]
    assert data.principal_oracle_ref_input_index == 0
    assert refs[data.chosen_collateral_oracle_ref_input_index] == strike.feed.out_ref
    assert (
        outputs[i].amount.coin == lends_ada.pool.lovelace - lends_ada.principal_amount
    )

    # USDM principal from its feed against ADA (placeholder index).
    i, data = by_pool[lends_usdm.out_ref]
    assert refs[data.principal_oracle_ref_input_index] == usdm.feed.out_ref
    assert data.chosen_collateral_oracle_ref_input_index == 0
    assert _principal_left(lends_usdm, outputs[i]) == lent - 1_000_000
    loan = outputs[len(legs) + i]
    assert _assets(loan) == [(c.LOAN_POLICY, legs[i].loan_id.hex(), 1)]

    assert _withdraw_scripts(tx) == {
        c.POOL_POLICY,
        c.POOL_BORROW_ACTION_SKH,
        usdm.script_hash,
        strike.script_hash,
    }


@pytest.mark.parametrize(
    ("collateral", "fetched"),
    [("lovelace", [_USDM]), (_STRIKE, [_STRIKE, _USDM])],
    ids=["ada-collateral", "token-collateral"],
)
def test_from_backend_fetches_each_price_the_pool_reads(
    monkeypatch: pytest.MonkeyPatch,
    collateral: str,
    fetched: list[str],
) -> None:
    fix = fixture("borrow_single")
    capture = BorrowSnapshot.from_capture(fix)
    pool, index = _entity_pool(_USDM, collateral)
    _fixture_backend(monkeypatch, fix, pools=[pool])
    prices = {_USDM: _usdm_price(capture, pool), _STRIKE: capture.oracles[0]}
    asked: list[str] = []

    class _Registry:
        def fetch_oracle_witness(self, *, collateral_unit: str) -> str:
            asked.append(collateral_unit)
            return collateral_unit

    monkeypatch.setattr(
        OracleWitness,
        "from_bundle",
        classmethod(lambda _cls, _backend, unit: prices[unit]),
    )
    snapshot = _from_backend(
        capture,
        borrows=[PoolBorrow(pool.out_ref, 1_000_000, index)],
        oracles=None,
        provider=_Registry(),
    )
    assert sorted(asked) == sorted(fetched)
    assert snapshot.oracles == [prices[unit] for unit in asked]


def test_from_backend_needs_the_principal_price_of_a_token_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fix = fixture("borrow_single")
    capture = BorrowSnapshot.from_capture(fix)
    pool, index = _entity_pool(_USDM, _STRIKE)
    _fixture_backend(monkeypatch, fix, pools=[pool])
    with pytest.raises(ValueError, match="no oracle witness prices the principal"):
        _from_backend(
            capture,
            borrows=[PoolBorrow(pool.out_ref, 1_000_000, index)],
            oracles=capture.oracles,
        )


def test_from_backend_lends_at_most_the_pools_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fix = fixture("borrow_single")
    capture = BorrowSnapshot.from_capture(fix)
    pool, index = _entity_pool(_USDM, "lovelace")
    _fixture_backend(monkeypatch, fix, pools=[pool])
    lent = next(q for p, n, q in pool.assets if p + n == _USDM)
    with pytest.raises(ValueError, match=f"lends at most {lent},"):
        _from_backend(
            capture,
            borrows=[PoolBorrow(pool.out_ref, lent + 1, index)],
            oracles=[_usdm_price(capture, pool)],
        )


def test_from_backend_refuses_a_missing_collateral_option(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fix = fixture("borrow_single")
    _fixture_backend(monkeypatch, fix)
    capture = BorrowSnapshot.from_capture(fix)
    (leg,) = capture.legs
    with pytest.raises(ValueError, match="no collateral option"):
        _from_backend(
            capture,
            borrows=[PoolBorrow(leg.out_ref, leg.principal_amount, 99)],
        )


def test_from_backend_refuses_policy_wide_collateral(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fix = fixture("borrow_single")
    capture = BorrowSnapshot.from_capture(fix)
    (leg,) = capture.legs
    datum = PoolDatum.from_cbor(leg.pool.datum)
    (option, *rest) = datum.collateral_options
    policy_wide = replace(
        option,
        maybe_asset_name=RawPlutusData(cbor2.CBORTag(122, [])),  # None
    )
    pool = replace(
        leg.pool,
        datum=replace(datum, collateral_options=[policy_wide, *rest]).to_cbor_hex(),
    )
    _fixture_backend(monkeypatch, fix, pools=[pool])
    with pytest.raises(NotImplementedError, match="policy-wide collateral"):
        _from_backend(capture, oracles=None, provider=_StubProvider())


def test_from_backend_refuses_pools_sending_borrower_bonds_to_a_script(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fix = fixture("borrow_single")
    capture = BorrowSnapshot.from_capture(fix)
    (leg,) = capture.legs
    datum = PoolDatum.from_cbor(leg.pool.datum)
    pool = replace(
        leg.pool,
        datum=replace(
            datum,
            common_data=replace(
                datum.common_data,
                borrower_bond_destination_script_hash=b"\x01" * 28,
            ),
        ).to_cbor_hex(),
    )
    _fixture_backend(monkeypatch, fix, pools=[pool])
    with pytest.raises(NotImplementedError, match="borrower bonds to a script"):
        _from_backend(capture, oracles=None, provider=_StubProvider())


def _borrowed_loan(
    monkeypatch: pytest.MonkeyPatch,
    pool_edit: Callable[[PoolDatum], PoolDatum] = lambda d: d,
) -> TransactionOutput:
    """The loan output of the resolved borrow_single, its pool's datum edited."""
    fix = fixture("borrow_single")
    capture = BorrowSnapshot.from_capture(fix)
    (leg,) = capture.legs
    pool = replace(
        leg.pool,
        datum=pool_edit(PoolDatum.from_cbor(leg.pool.datum)).to_cbor_hex(),
    )
    _fixture_backend(monkeypatch, fix, pools=[pool])
    snapshot = _from_backend(capture)
    body = build(build_borrow, snapshot, slot=fix["invalid_before"]).tx.transaction_body
    # Continuing pool, then the loan.
    loan = body.outputs[1]
    assert loan.amount.coin == snapshot.legs[0].loan_lovelace
    return loan


def _assets(output: TransactionOutput) -> list[tuple[str, str, int]]:
    return [
        (bytes(policy).hex(), name.payload.hex(), qty)
        for policy, names in output.amount.multi_asset.items()
        for name, qty in names.items()
    ]


def test_the_loan_carries_the_ada_of_its_widest_form(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loan = _borrowed_loan(monkeypatch)
    widest = TransactionOutput(
        loan.address,
        utxo_value(
            loan.amount.coin,
            [
                (p, n, 1 if p == c.LOAN_POLICY else 2**63 - 1)
                for p, n, _ in _assets(loan)
            ],
        ),
        datum=replace(
            LoanDatum.from_cbor(loan.datum.to_cbor()),
            repaid_installments=2**32 - 1,
            done_recasts=2**32 - 1,
        ),
    )
    assert loan.amount.coin >= min_ada(widest)
    # More than the loan needs as it is born: the headroom later actions consume.
    assert loan.amount.coin > min_ada(loan)


def test_a_borrowed_loan_can_repay_its_24th_installment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def with_installments(datum: PoolDatum) -> PoolDatum:
        return replace(
            datum,
            common_data=replace(datum.common_data, installment_period=720),
        )

    loan = _borrowed_loan(monkeypatch, with_installments)
    fix = fixture("repay_single")
    snapshot = RepaySnapshot.from_capture(fix)
    (position,) = snapshot.positions
    # The loan after 23 installments: one more makes its datum a byte longer.
    position.loan = Utxo(
        address=str(loan.address),
        lovelace=loan.amount.coin,
        assets=_assets(loan),
        datum=replace(
            LoanDatum.from_cbor(loan.datum.to_cbor()),
            repaid_installments=23,
        ).to_cbor_hex(),
        out_ref=("cc" * 32, 0),
    )
    position.is_final = False
    position.payment = 1
    position.lender_lovelace = None
    body = build(build_repay, snapshot, slot=fix["invalid_before"]).tx.transaction_body
    continuing = body.outputs[0]
    assert LoanDatum.from_cbor(continuing.datum.to_cbor()).repaid_installments == 24
    assert continuing.amount.coin == loan.amount.coin
    assert continuing.amount.coin >= min_ada(continuing)
