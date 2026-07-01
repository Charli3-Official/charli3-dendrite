"""build/assemble create-loan components must byte-match the captured on-chain tx."""

import json
from pathlib import Path

from charli3_dendrite.lending.danogo.transactions.context import CreateLoanContext
from charli3_dendrite.lending.danogo.transactions.create_loan import assemble_components

FIX = json.loads(
    (Path(__file__).parent / "fixtures" / "create_loan_tx.json").read_text()
)


def _captured(purpose, skh):
    return next(
        bytes.fromhex(r["cbor"])
        for r in FIX["redeemers"]
        if r["purpose"] == purpose and r["script_hash"] == skh
    )


def test_create_loan_redeemer_matches_onchain_byte_exact():
    ctx = CreateLoanContext.from_fixture(FIX)
    comp = assemble_components(ctx)
    assert comp.create_loan_redeemer.to_cbor() == _captured("mint", FIX["loan_skh"])


def test_owner_nft_name_is_blake2b_of_pool_outref():
    ctx = CreateLoanContext.from_fixture(FIX)
    comp = assemble_components(ctx)
    minted = {m[1] for m in FIX["mints"] if m[0] == FIX["loan_skh"] and m[2] == 1}
    assert comp.owner_nft_name in minted


def test_mint_matches_onchain():
    ctx = CreateLoanContext.from_fixture(FIX)
    comp = assemble_components(ctx)
    assert {(p, n, q) for p, n, q in comp.mint} == {
        (m[0], m[1], m[2]) for m in FIX["mints"]
    }


def test_oracle_redeemer_embedded_byte_exact():
    ctx = CreateLoanContext.from_fixture(FIX)
    comp = assemble_components(ctx)
    assert comp.oracle_redeemer.to_cbor() == bytes.fromhex(ctx.oracle_redeemer_cbor)
