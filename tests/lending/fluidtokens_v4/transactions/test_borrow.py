"""Offline: forward-built V4 borrows reproduce the captured mainnet borrows."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import cbor2
import pytest
from pycardano import Address
from pycardano import RawPlutusData
from pycardano import TransactionBuilder
from pycardano import TransactionOutput

from charli3_dendrite.lending.fluidtokens.datums import Asset
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
from charli3_dendrite.lending.fluidtokens_v4.transactions import resolve
from charli3_dendrite.lending.fluidtokens_v4.transactions.borrow import BorrowSnapshot
from charli3_dendrite.lending.fluidtokens_v4.transactions.borrow import PoolBorrow
from charli3_dendrite.lending.fluidtokens_v4.transactions.borrow import build_borrow
from charli3_dendrite.lending.fluidtokens_v4.transactions.common import min_ada
from charli3_dendrite.lending.fluidtokens_v4.transactions.repay import RepaySnapshot
from charli3_dendrite.lending.fluidtokens_v4.transactions.repay import build_repay
from charli3_dendrite.lending.transactions.infra import EvalContext
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


def test_from_backend_refuses_ada_collateral(monkeypatch: pytest.MonkeyPatch) -> None:
    fix = fixture("borrow_single")
    capture = BorrowSnapshot.from_capture(fix)
    (leg,) = capture.legs
    datum = PoolDatum.from_cbor(leg.pool.datum)
    (option, *rest) = datum.collateral_options
    ada = replace(
        option,
        policy_id=b"",
        maybe_asset_name=RawPlutusData(cbor2.CBORTag(121, [b""])),  # Some("")
        oracle_token_asset=Asset(policy_id=b"", asset_name=b""),
    )
    pool = replace(
        leg.pool,
        datum=replace(
            datum,
            collateral_options=[ada, *rest],
        ).to_cbor_hex(),
    )
    _fixture_backend(monkeypatch, fix, pools=[pool])
    with pytest.raises(NotImplementedError, match="ADA collateral"):
        _from_backend(capture, oracles=None, provider=_StubProvider())


def test_from_backend_refuses_token_principal_pools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fix = fixture("borrow_single")
    capture = BorrowSnapshot.from_capture(fix)
    (leg,) = capture.legs
    token_principal = next(
        PoolDatum.from_cbor(rec["datum_cbor"]).common_data.principal_asset
        for rec in _ENTITIES["pool"]
        if PoolDatum.from_cbor(rec["datum_cbor"]).common_data.principal_asset.unit()
        != "lovelace"
    )
    datum = PoolDatum.from_cbor(leg.pool.datum)
    pool = replace(
        leg.pool,
        datum=replace(
            datum,
            common_data=replace(
                datum.common_data,
                principal_asset=token_principal,
            ),
        ).to_cbor_hex(),
    )
    _fixture_backend(monkeypatch, fix, pools=[pool])
    with pytest.raises(NotImplementedError, match="lend a token"):
        _from_backend(capture, oracles=None, provider=_StubProvider())


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
