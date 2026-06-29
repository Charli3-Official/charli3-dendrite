"""Assemble + verify the Danogo oracle withdrawal redeemer for a create-loan tx.

The ``OraclePriceCalcRdmr`` carries protocol-internal data (source/path indices,
borrow rates) whose external source locators we cannot fully resolve. So the builder
embeds the sourced redeemer bytes verbatim and independently *verifies* that each
``prices`` entry is reproducible from the referenced leaves via ``reproduce_price``.
"""

from __future__ import annotations

from charli3_dendrite.lending.danogo.oracles.redeemer import OraclePriceCalcRdmr
from charli3_dendrite.lending.danogo.oracles.redeemer import UTxOTarget
from charli3_dendrite.lending.danogo.oracles.reproduce import OracleLeaf
from charli3_dendrite.lending.danogo.oracles.reproduce import reproduce_price
from charli3_dendrite.lending.danogo.transactions.context import CreateLoanContext


def _leaves(ctx: CreateLoanContext, rdmr: OraclePriceCalcRdmr) -> list[OracleLeaf]:
    pools = {
        UTxOTarget.REF: ctx.ref_inputs,
        UTxOTarget.IN: ctx.inputs,
        UTxOTarget.OUT: ctx.outputs,
    }
    leaves: list[OracleLeaf] = []
    for target, otype, index in rdmr.oracle_idxs:
        u = pools[target][index]
        assets = tuple((p, n, q) for p, n, q in u.assets)
        if u.lovelace:
            assets = (*assets, ("", "", u.lovelace))
        leaves.append(OracleLeaf(otype=otype, datum=u.datum, assets=assets))
    return leaves


def assemble_oracle_redeemer(
    ctx: CreateLoanContext,
) -> tuple[OraclePriceCalcRdmr, dict]:
    """Return the embeddable redeemer + a coverage report of reproduced prices."""
    if not ctx.oracle_redeemer_cbor:
        raise ValueError("context has no oracle redeemer to embed")
    rdmr = OraclePriceCalcRdmr.from_cbor(bytes.fromhex(ctx.oracle_redeemer_cbor))
    leaves = _leaves(ctx, rdmr)

    total = 0
    covered = 0
    misses: list[tuple[str, str]] = []
    for quote, inner in rdmr.prices.items():
        for collat, rate in inner.items():
            total += 1
            if reproduce_price(quote_unit=quote, target=rate, leaves=leaves):
                covered += 1
            else:
                misses.append((quote, collat))
    return rdmr, {"total": total, "covered": covered, "misses": misses}
