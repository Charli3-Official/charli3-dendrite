"""Synthesize the Danogo ``OraclePriceCalcRdmr`` from resolved leaves + prices.

The forward create-loan builder resolves the concrete oracle leaf UTxOs live and
computes their collateral prices; this module assembles those parts into the
on-chain redeemer. The ``oracle_idxs`` field is rebuilt from the ``(target,
otype, idx)`` of each leaf tuple (in list order). The resolved ``OracleLeaf`` is
a placeholder accepted for caller convenience — the assembly step (Task 4) keeps
it on the same tuple shape for its own use, but it is intentionally not consumed
here. The price/rate/source/path data pass straight through.
"""

from __future__ import annotations

from charli3_dendrite.lending.danogo.oracles.redeemer import OracleIndex
from charli3_dendrite.lending.danogo.oracles.redeemer import OraclePriceCalcRdmr
from charli3_dendrite.lending.danogo.oracles.redeemer import OracleUtxoType
from charli3_dendrite.lending.danogo.oracles.redeemer import UTxOTarget
from charli3_dendrite.lending.danogo.oracles.reproduce import OracleLeaf


def synthesize_oracle_redeemer(
    *,
    leaves: list[tuple[UTxOTarget, OracleUtxoType, int, OracleLeaf]],
    prices: dict[str, dict[str, tuple[int, int]]],
    borrow_rates: dict[str, int],
    oracle_source_idx: int,
    oracle_path_idxs: list[int],
) -> OraclePriceCalcRdmr:
    """Assemble an ``OraclePriceCalcRdmr`` from resolved leaves and computed prices.

    ``oracle_idxs`` is rebuilt from the ``(target, otype, idx)`` of each ``leaves``
    tuple, in list order. The ``OracleLeaf`` element is a placeholder accepted for
    caller convenience (Task 4's assembly step uses it); it is intentionally not
    consumed here. The remaining fields pass through unchanged.
    """
    oracle_idxs: list[OracleIndex] = [
        (target, otype, idx) for target, otype, idx, _leaf in leaves
    ]
    return OraclePriceCalcRdmr(
        oracle_source_idx=oracle_source_idx,
        oracle_path_idxs=oracle_path_idxs,
        oracle_idxs=oracle_idxs,
        prices=prices,
        borrow_rates=borrow_rates,
    )
