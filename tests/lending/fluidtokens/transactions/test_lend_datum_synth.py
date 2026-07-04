"""synth_loan_datum_from_request maps a RequestDatum to a fresh LoanDatum; bounds math."""

from __future__ import annotations

from charli3_dendrite.lending.fluidtokens.datums import RequestDatum
from charli3_dendrite.lending.fluidtokens.transactions.datum_synth import (
    request_principal_bounds,
    synth_loan_datum_from_request,
)

# RequestDatum of the request filled by mainnet tx 043a6f047cca... (spent input
# 320c8a93...#12). Captured inline datum (hex) — permissionless (perm hash = "NONE"),
# ADA principal, static (dynamic_collateral_price = False).
_REQUEST_DATUM = (
    "d8799f444e4f4e45d87980d8799fd8799f4040ffd8799f4040ff1901901944700200d87980"
    "d87b9f0005ff0000d87980ffd8799f581ccb339ced1c2acc6482fa244290109d8ada8492cc"
    "7e7e818fff12e2bfffd8799fd8799f581ccb339ced1c2acc6482fa244290109d8ada8492cc"
    "7e7e818fff12e2bfffd8799fd8799fd8799f581c0fdd2f61c10bbbd3db38796ebc7d7880a1"
    "2ea196381b92fef3f91805ffffffffd8799f581c5d16cc1a177b5d9ba9cfa9793b07e60f1f"
    "b70fea1f8aef064415d114d8799f43494147ffd8799f581c93794f9b7f3dc632cb889c7aec"
    "7d334f016f532e64f16141b6895f5b496f7261636c65494147ffff09011b00000002540be4"
    "00d879801b000001a07ab48b0f00ff"
)


def test_synth_loan_datum_uses_request_origin_and_carries_terms() -> None:
    datum = RequestDatum.from_cbor(bytes.fromhex(_REQUEST_DATUM))
    request_id = b"\x00" + b"\x11" * 28
    loan = synth_loan_datum_from_request(
        request_datum=datum,
        request_id=request_id,
        given_principal_amount=10_000_000_000,
        lend_date=1_779_796_738_000,
    )
    assert loan.origin_id == b"REQUEST" + request_id
    assert loan.principal_amount == 10_000_000_000
    assert loan.lend_date == 1_779_796_738_000
    assert loan.done_recasts == 0
    assert loan.repaid_installments == 0
    assert loan.interest_rate == datum.common_data.interest_rate
    assert loan.collateral == datum.collateral


def test_request_principal_bounds_ceils_the_min_and_returns_max() -> None:
    datum = RequestDatum.from_cbor(bytes.fromhex(_REQUEST_DATUM))
    # min = ceil(collateral_amount * min_principal_divider / min_principal)
    lo, hi = request_principal_bounds(datum, collateral_amount=1_000)
    expected_lo = -(-(1_000 * datum.min_principal_divider) // datum.min_principal)
    assert lo == expected_lo
    assert hi == datum.max_principal


def test_synth_reproduces_captured_loan_datum() -> None:
    import json
    from pathlib import Path

    from charli3_dendrite.lending.fluidtokens.transactions.context import LendSnapshot
    from charli3_dendrite.utility import slot_to_posix_ms

    fix = json.loads(
        (Path(__file__).parent / "fixtures" / "lend.json").read_text(),
    )
    snap = LendSnapshot.from_capture(fix)
    loan = synth_loan_datum_from_request(
        request_datum=snap.request_datum,
        request_id=snap.request_id,
        given_principal_amount=snap.given_principal_amount,
        lend_date=slot_to_posix_ms(snap.valid_to),
    )
    assert loan.to_cbor().hex() == fix["loan_out_datum"]
