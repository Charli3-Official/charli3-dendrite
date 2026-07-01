import json
from pathlib import Path

from charli3_dendrite.lending.danogo.oracles.redeemer import OracleUtxoType
from charli3_dendrite.lending.danogo.oracles.redeemer import OraclePriceCalcRdmr
from charli3_dendrite.lending.danogo.oracles.redeemer import UTxOTarget
from charli3_dendrite.lending.danogo.oracles.reproduce import OracleLeaf
from charli3_dendrite.lending.danogo.transactions.context import CreateLoanContext
from charli3_dendrite.lending.danogo.transactions.oracle_synth import (
    synthesize_oracle_redeemer,
)

FIX = json.loads(
    (Path(__file__).parent / "fixtures" / "create_loan_tx.json").read_text(),
)


def _leaves_from_ctx(
    ctx: CreateLoanContext,
    captured: OraclePriceCalcRdmr,
) -> list[tuple[UTxOTarget, OracleUtxoType, int, OracleLeaf]]:
    pools = {
        UTxOTarget.REF: ctx.ref_inputs,
        UTxOTarget.OUT: ctx.outputs,
        UTxOTarget.IN: ctx.inputs,
    }
    out: list[tuple[UTxOTarget, OracleUtxoType, int, OracleLeaf]] = []
    for target, otype, idx in captured.oracle_idxs:
        utxo = pools[target][idx]
        # Mirror transactions/oracle_redeemer.py::_leaves: include the UTxO's
        # lovelace balance as a ("", "", coin) asset so ADA-quoted leaves resolve.
        assets = tuple((p, n, q) for p, n, q in utxo.assets)
        if utxo.lovelace:
            assets = (*assets, ("", "", utxo.lovelace))
        leaf = OracleLeaf(otype=otype, datum=utxo.datum, assets=assets)
        out.append((target, otype, idx, leaf))
    return out


def test_synth_reproduces_captured_prices():
    ctx = CreateLoanContext.from_fixture(FIX)
    captured = OraclePriceCalcRdmr.from_cbor(bytes.fromhex(ctx.oracle_redeemer_cbor))
    synth = synthesize_oracle_redeemer(
        leaves=_leaves_from_ctx(ctx, captured),
        prices=captured.prices,
        borrow_rates=captured.borrow_rates,
        oracle_source_idx=captured.oracle_source_idx,
        oracle_path_idxs=captured.oracle_path_idxs,
    )
    assert synth.oracle_idxs == captured.oracle_idxs
    assert synth.oracle_source_idx == captured.oracle_source_idx
    assert synth.oracle_path_idxs == captured.oracle_path_idxs
    assert synth.borrow_rates == captured.borrow_rates
    assert synth.prices == captured.prices

    roundtrip = OraclePriceCalcRdmr.from_cbor(synth.to_cbor())
    assert roundtrip.oracle_idxs == captured.oracle_idxs
    assert roundtrip.prices == captured.prices
