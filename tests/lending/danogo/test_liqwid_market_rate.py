"""Both Danogo reads of a Liqwid MarketState rate are its decoded qToken rate.

The redeemer-driven reproduction folds the underlying's cent scale into the rate;
the structured path takes it raw (the underlying's price is a separate hop there).
Both read ``decode_market_state(...).qtoken_rate``.
"""

from __future__ import annotations

from fractions import Fraction

from charli3_dendrite.lending.danogo.oracles.redeemer import OracleUtxoType
from charli3_dendrite.lending.danogo.oracles.reproduce import OracleLeaf
from charli3_dendrite.lending.danogo.oracles.reproduce import leaf_forward_rates
from charli3_dendrite.lending.danogo.transactions._common import (
    _STRUCTURED_KIND_LIQWID_MARKET,
)
from charli3_dendrite.lending.danogo.transactions._common import _structured_hop_rate
from charli3_dendrite.lending.danogo.transactions.context import _utxo_from_info
from charli3_dendrite.lending.liqwid.datums import decode_market_state
from tests.lending.liqwid.records import parsed_records
from tests.lending.records_common import record_info

# Cents per unit of the underlying's price, folded into the reproduction's rate.
_UNDERLYING_SCALE = 100


def test_both_market_state_reads_use_the_decoded_qtoken_rate():
    states = parsed_records("market_state", live=True)
    assert states
    for rec, _ in states:
        datum = rec["datum_cbor"]
        rate = Fraction(*decode_market_state(datum).qtoken_rate)
        leaf = OracleLeaf(otype=OracleUtxoType.TLIQWID_MARKET_STATE, datum=datum)
        assert leaf_forward_rates(leaf) == [("liqmkt", rate / _UNDERLYING_SCALE)]
        structured = _structured_hop_rate(
            _utxo_from_info(record_info(rec)),
            _STRUCTURED_KIND_LIQWID_MARKET,
            OracleUtxoType.TLIQWID_MARKET_STATE,
        )
        assert structured == rate
