"""The diff and request helpers of the manual Liqwid API cross-check script.

Every transaction here is built offline from a captured fixture: a capture's own
build stands in for Liqwid's, and a snapshot changed the way these builders differ
from Liqwid's app stands in for ours. No network, dbsync or Ogmios is used.
"""

from __future__ import annotations

import dataclasses
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import cbor2
import pytest
from pycardano import Address

from charli3_dendrite.lending.liqwid.deployment import default_deployment
from charli3_dendrite.lending.liqwid.transactions.borrow import BorrowSnapshot
from charli3_dendrite.lending.liqwid.transactions.borrow import build_borrow
from charli3_dendrite.lending.liqwid.transactions.supply import RedeemSnapshot
from charli3_dendrite.lending.liqwid.transactions.supply import SupplySnapshot
from charli3_dendrite.lending.liqwid.transactions.supply import build_redeem
from charli3_dendrite.lending.liqwid.transactions.supply import build_supply
from charli3_dendrite.lending.liqwid.transactions.terms import Flow
from charli3_dendrite.lending.liqwid.transactions.terms import redeem_payout
from charli3_dendrite.lending.units import out_ref_text
from tests.lending.liqwid.transactions import _api_crosscheck as xcheck
from tests.lending.liqwid.transactions.replay import Built
from tests.lending.liqwid.transactions.replay import block_slot
from tests.lending.liqwid.transactions.replay import build
from tests.lending.liqwid.transactions.replay import fixture
from tests.lending.liqwid.transactions.replay import fixture_utxos

SCRIPT = Path(xcheck.__file__)
QADA = "a04ce7a52545e5e33c2867e148898d9e667a69602285f6a1298f9d68"
QNIGHT = "c45fa8aefc662c003a32be67f6a4652d8ce56bd9e54d7696efd40c86"


def _lookup(fix: dict[str, Any]) -> dict[str, Any]:
    return {out_ref_text(u.out_ref): u for u in fixture_utxos(fix)}


def _views(
    label: str,
    snapshot: Any,  # noqa: ANN401
    changed: Any,  # noqa: ANN401
    contribute: Any,  # noqa: ANN401
) -> tuple[xcheck.TxView, xcheck.TxView]:
    """The views of a capture's build (the API side) and of ``changed`` (ours)."""
    fix = fixture(label)
    slot = block_slot(fix)
    lookup = _lookup(fix)
    api = build(contribute, snapshot, slot=slot)
    ours = build(contribute, changed, slot=slot)
    return xcheck.tx_view(api.cbor, lookup), xcheck.tx_view(ours.cbor, lookup)


def _statuses(rows: list[xcheck.Row]) -> dict[str, tuple[str, str]]:
    return {row.key: (row.status, row.note) for row in xcheck.differences(rows)}


def _with_body(built: Built, **changes: Any) -> Built:  # noqa: ANN401
    """``built`` with body fields replaced (``k<number>=value``)."""
    tx = cbor2.loads(bytes.fromhex(built.cbor))
    for key, value in changes.items():
        tx[0][int(key[1:])] = value
    return Built(cbor2.dumps(tx).hex())


# --- views -------------------------------------------------------------------


def test_supply_view_names_every_role() -> None:
    fix = fixture("supply_ada")
    built = build(build_supply, SupplySnapshot.from_capture(fix), slot=block_slot(fix))
    view = xcheck.tx_view(built.cbor, _lookup(fix))

    assert set(view.references) == {
        "MarketParams Ada",
        "MarketState Ada",
        "reference script: Ada action validator",
        "reference script: Ada qToken policy",
    }
    assert set(view.spends) == {"action UTxO Ada"}
    assert view.redeemers == {
        "spend action UTxO Ada": ("d87980",),
        "mint Ada qToken policy": ("d87980",),
    }
    assert set(view.mints) == {QADA}
    assert set(view.outputs) == {"action UTxO Ada"}


def test_open_view_names_the_health_set_and_the_staking_withdrawal() -> None:
    fix = fixture("open_single")
    built = build(build_borrow, BorrowSnapshot.from_capture(fix), slot=block_slot(fix))
    view = xcheck.tx_view(built.cbor, _lookup(fix))

    assert set(view.references) == {
        "CollateralParams Ada / qNIGHT",
        "MarketParams Ada",
        "MarketState Ada",
        "MarketState NIGHT",
        "oracle feed NIGHT",
        "oracle feed lovelace",
        "reference script: Ada action validator",
        "reference script: Ada borrow staking",
        "reference script: Ada loan-token policy",
    }
    assert view.redeemers["reward Ada borrow staking"] == ("00",)
    assert set(view.outputs) == {"action UTxO Ada", "loan Ada"}


def test_an_out_ref_the_lookup_lacks_reads_as_unresolved() -> None:
    fix = fixture("supply_ada")
    built = build(build_supply, SupplySnapshot.from_capture(fix), slot=block_slot(fix))

    view = xcheck.tx_view(built.cbor, {})

    assert set(view.references) == {"unresolved"}
    assert set(view.spends) == {"unresolved"}


# --- diff --------------------------------------------------------------------


def test_identical_builds_have_no_differences() -> None:
    snapshot = SupplySnapshot.from_capture(fixture("supply_ada"))
    api, ours = _views("supply_ada", snapshot, snapshot, build_supply)

    rows = xcheck.compare(api, ours, flow=Flow.SUPPLY)

    assert rows
    assert xcheck.differences(rows) == []
    assert xcheck.render(rows).endswith(
        f"{len(rows)} items: {len(rows)} same, 0 expected, 0 unexpected",
    )


def test_supply_signer_and_upper_bound_are_expected() -> None:
    snapshot = SupplySnapshot.from_capture(fixture("supply_ada"))
    changed = dataclasses.replace(
        snapshot,
        required_signers=(),
        valid_to_ms=snapshot.valid_to_ms - 60_000,
    )
    api, ours = _views("supply_ada", snapshot, changed, build_supply)

    rows = xcheck.compare(api, ours, flow=Flow.SUPPLY)

    assert _statuses(rows) == {
        "required signers": (xcheck.EXPECTED, xcheck.NOTE_SIGNER),
        "upper bound (slot)": (xcheck.EXPECTED, xcheck.NOTE_TTL),
    }
    assert xcheck.unexpected(rows) == []


def test_a_signer_dropped_on_a_loan_flow_is_unexpected() -> None:
    snapshot = SupplySnapshot.from_capture(fixture("supply_ada"))
    changed = dataclasses.replace(snapshot, required_signers=())
    api, ours = _views("supply_ada", snapshot, changed, build_supply)

    rows = xcheck.compare(api, ours, flow=Flow.OPEN)

    assert _statuses(rows) == {"required signers": (xcheck.DIFFERS, "")}


def test_redeem_payout_margin_is_expected() -> None:
    snapshot = RedeemSnapshot.from_capture(fixture("redeem_ada"))
    rate = snapshot.records.state_datum.qtoken_rate
    changed = dataclasses.replace(
        snapshot,
        payout=redeem_payout(snapshot.qtokens, rate),
    )
    assert changed.payout == snapshot.payout + 1
    api, ours = _views("redeem_ada", snapshot, changed, build_redeem)

    rows = xcheck.compare(api, ours, flow=Flow.REDEEM)

    assert _statuses(rows) == {
        "action UTxO Ada lovelace": (xcheck.EXPECTED, xcheck.NOTE_REDEEM),
        "action UTxO Ada datum": (xcheck.EXPECTED, xcheck.NOTE_REDEEM),
    }
    lovelace = next(r for r in rows if r.key == "action UTxO Ada lovelace")
    assert lovelace.api - lovelace.ours == 1


def test_redeem_burn_difference_is_expected() -> None:
    snapshot = RedeemSnapshot.from_capture(fixture("redeem_ada"))
    rate = snapshot.records.state_datum.qtoken_rate
    qtokens = snapshot.qtokens + 1
    changed = dataclasses.replace(
        snapshot,
        qtokens=qtokens,
        payout=redeem_payout(qtokens, rate),
    )
    api, ours = _views("redeem_ada", snapshot, changed, build_redeem)

    rows = xcheck.compare(api, ours, flow=Flow.REDEEM)

    assert xcheck.unexpected(rows) == []
    assert _statuses(rows)[f"qAda ({QADA})"] == (xcheck.EXPECTED, xcheck.NOTE_REDEEM)


def test_loan_lovelace_byte_is_expected() -> None:
    snapshot = BorrowSnapshot.from_capture(fixture("open_single"))
    changed = dataclasses.replace(snapshot, loan_lovelace=None)
    api, ours = _views("open_single", snapshot, changed, build_borrow)

    rows = xcheck.compare(api, ours, flow=Flow.OPEN)

    assert _statuses(rows) == {
        "loan Ada lovelace": (xcheck.EXPECTED, xcheck.NOTE_LOAN_BYTE),
    }
    row = next(r for r in rows if r.key == "loan Ada lovelace")
    assert row.api - row.ours == xcheck.COINS_PER_BYTE


def test_loan_lovelace_beyond_one_byte_is_unexpected() -> None:
    snapshot = BorrowSnapshot.from_capture(fixture("open_single"))
    changed = dataclasses.replace(
        snapshot,
        loan_lovelace=snapshot.loan_lovelace + 2 * xcheck.COINS_PER_BYTE,
    )
    api, ours = _views("open_single", snapshot, changed, build_borrow)

    rows = xcheck.compare(api, ours, flow=Flow.OPEN)

    assert _statuses(rows) == {"loan Ada lovelace": (xcheck.DIFFERS, "")}


def test_datum_array_encoding_is_expected() -> None:
    snapshot = BorrowSnapshot.from_capture(fixture("open_indefinite"))
    assert snapshot.indefinite
    changed = dataclasses.replace(snapshot, indefinite=None)
    api, ours = _views("open_indefinite", snapshot, changed, build_borrow)

    rows = xcheck.compare(api, ours, flow=Flow.OPEN)

    statuses = _statuses(rows)
    assert statuses
    assert all(status == xcheck.EXPECTED for status, _ in statuses.values())
    datums = {k: v for k, v in statuses.items() if k.endswith(" datum")}
    assert datums
    assert all(note == xcheck.NOTE_ENCODING for _, note in datums.values())


def test_a_wrong_mint_is_unexpected_and_rendered_with_both_sides() -> None:
    snapshot = SupplySnapshot.from_capture(fixture("supply_ada"))
    changed = dataclasses.replace(snapshot, qtokens=snapshot.qtokens - 1)
    api, ours = _views("supply_ada", snapshot, changed, build_supply)

    rows = xcheck.compare(api, ours, flow=Flow.SUPPLY)

    assert _statuses(rows) == {
        f"qAda ({QADA})": (xcheck.DIFFERS, ""),
        "action UTxO Ada datum": (xcheck.DIFFERS, ""),
    }
    text = xcheck.render(rows, title="supply")
    assert text.startswith("supply\n== reference inputs")
    assert f"  DIFFERS   qAda ({QADA})" in text
    assert f"            api   {snapshot.qtokens}" in text
    assert f"            ours  {snapshot.qtokens - 1}" in text
    assert text.endswith("0 expected, 2 unexpected")


def test_a_missing_reference_is_unexpected() -> None:
    fix = fixture("supply_ada")
    built = build(build_supply, SupplySnapshot.from_capture(fix), slot=block_slot(fix))
    body = cbor2.loads(bytes.fromhex(built.cbor))[0]
    refs = list(body[18].value if isinstance(body[18], cbor2.CBORTag) else body[18])
    dropped = _with_body(built, k18=refs[1:])
    lookup = _lookup(fix)

    rows = xcheck.compare(
        xcheck.tx_view(built.cbor, lookup),
        xcheck.tx_view(dropped.cbor, lookup),
        flow=Flow.SUPPLY,
    )

    (row,) = xcheck.differences(rows)
    assert row.section == "reference inputs"
    assert row.status == xcheck.DIFFERS
    assert row.ours is None


# --- request helpers ---------------------------------------------------------


@pytest.mark.parametrize(
    ("flow", "field", "input_type"),
    [
        (Flow.SUPPLY, "supply", "SupplyTransactionInput"),
        (Flow.REDEEM, "withdraw", "WithdrawTransactionInput"),
        (Flow.OPEN, "borrow", "BorrowTransactionInput"),
        (Flow.INCREASE, "modifyBorrow", "ModifyBorrowTransactionInput"),
        (Flow.REPAY, "modifyBorrow", "ModifyBorrowTransactionInput"),
        (Flow.CLOSE, "modifyBorrow", "ModifyBorrowTransactionInput"),
        (Flow.COLLATERAL, "modifyBorrow", "ModifyBorrowTransactionInput"),
    ],
)
def test_build_query(flow: Flow, field: str, input_type: str) -> None:
    name, query = xcheck.build_query(flow)

    assert name == field
    assert f"$input: {input_type}!" in query
    assert f"{field}(input: $input) {{ cbor }}" in query


def test_api_collaterals_name_the_borrow_market_and_the_qtoken_policy() -> None:
    named = "ab" * 28 + "01"

    assert xcheck.api_collaterals("Ada", {QNIGHT: 200, named: 5, QADA: 0}) == [
        {"id": f"Ada.{'ab' * 28}", "amount": 5, "tokenName": "01"},
        {"id": f"Ada.{QNIGHT}", "amount": 200},
    ]


def test_api_loan_id() -> None:
    assert xcheck.api_loan_id("ab" * 32 + "#13") == "ab" * 32 + "-13"


@pytest.mark.parametrize(
    ("flow", "amount", "target"),
    [
        (Flow.INCREASE, 97, 1097),
        (Flow.REPAY, 100, 900),
        (Flow.CLOSE, 5000, 0),
        (Flow.COLLATERAL, 0, 1000),
    ],
)
def test_modify_borrow_target(flow: Flow, amount: int, target: int) -> None:
    assert xcheck.modify_borrow_target(flow, amount=amount, current_debt=1000) == target


def test_modify_borrow_target_refuses_a_market_flow() -> None:
    with pytest.raises(ValueError, match="not a loan modification"):
        xcheck.modify_borrow_target(Flow.SUPPLY, amount=1, current_debt=1)


def test_parse_collateral_reads_units_and_market_names() -> None:
    deployment = default_deployment()

    assert xcheck.parse_collateral(
        ["NIGHT=5", f"{QADA}=7", "NIGHT=1"],
        deployment,
    ) == {QNIGHT: 6, QADA: 7}
    with pytest.raises(ValueError, match="UNIT=QUANTITY"):
        xcheck.parse_collateral(["NIGHT"], deployment)
    with pytest.raises(KeyError):
        xcheck.parse_collateral(["NOPE=1"], deployment)


def test_cip30_utxo_is_an_input_output_pair() -> None:
    fix = fixture("supply_ada")
    wallet = next(u for u in fixture_utxos(fix) if not u.datum and not u.ref_script)

    decoded = cbor2.loads(bytes.fromhex(xcheck.cip30_utxo(wallet)))

    assert len(decoded) == 2
    tx_id, index = decoded[0]
    assert (tx_id.hex(), index) == wallet.out_ref
    assert str(Address.from_primitive(decoded[1][0])) == wallet.address


class _Response(io.BytesIO):
    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def test_request_build_posts_the_query_and_returns_the_cbor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent = []

    def urlopen(request: Any, timeout: float) -> _Response:  # noqa: ANN401
        sent.append((request.full_url, json.loads(request.data), timeout))
        answer = {"data": {"liqwid": {"transactions": {"withdraw": {"cbor": "84"}}}}}
        return _Response(json.dumps(answer).encode())

    monkeypatch.setattr(xcheck.urllib.request, "urlopen", urlopen)

    cbor = xcheck.request_build(Flow.REDEEM, {"amount": 5}, url="https://x", timeout=3)

    assert cbor == "84"
    ((url, body, timeout),) = sent
    assert (url, timeout) == ("https://x", 3)
    assert body["variables"] == {"input": {"amount": 5}}
    assert "withdraw(input: $input)" in body["query"]


def test_request_build_raises_on_api_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def urlopen(request: Any, timeout: float) -> _Response:  # noqa: ANN401, ARG001
        answer = {"errors": [{"message": "Can't perform action"}], "data": None}
        return _Response(json.dumps(answer).encode())

    monkeypatch.setattr(xcheck.urllib.request, "urlopen", urlopen)

    with pytest.raises(RuntimeError, match="Can't perform action"):
        xcheck.request_build(Flow.SUPPLY, {})


def test_the_script_prints_its_help_without_a_backend() -> None:
    root = Path(__file__).resolve().parents[4]
    env = {
        "HOME": os.environ.get("HOME", ""),
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": f"{root / 'src'}:{root}",
    }

    done = subprocess.run(  # noqa: S603
        [sys.executable, str(SCRIPT), "--help"],
        capture_output=True,
        text=True,
        env=env,
        cwd=root,
        check=False,
        timeout=120,
    )

    assert done.returncode == 0, done.stderr
    for command in ("supply", "redeem", "borrow", "increase", "repay", "collateral"):
        assert command in done.stdout
