"""Resolved-UTxO plumbing for the FluidTokens transaction builders.

The protocol-neutral helpers live in :mod:`charli3_dendrite.lending.transactions.utxos`
and are re-exported here for existing callers; this module adds the FluidTokens loan
id derivation.
"""

from __future__ import annotations

import hashlib

from charli3_dendrite.lending.fluidtokens.datums import TxOutRef
from charli3_dendrite.lending.transactions.utxos import SCRIPT_LANG  # noqa: F401
from charli3_dendrite.lending.transactions.utxos import Utxo  # noqa: F401
from charli3_dendrite.lending.transactions.utxos import ogmios_entry  # noqa: F401
from charli3_dendrite.lending.transactions.utxos import script_ref_by_hash  # noqa: F401
from charli3_dendrite.lending.transactions.utxos import to_pycardano_utxo  # noqa: F401
from charli3_dendrite.lending.transactions.utxos import utxo_from_dict  # noqa: F401
from charli3_dendrite.lending.transactions.utxos import utxo_value  # noqa: F401


def loan_id_from_out_ref(out_ref: tuple[str, int]) -> bytes:
    """The loan id minted from an origin out-ref: ``blake2b_224`` of its CBOR.

    The bond policy names each minted NFT (loan / borrower bond / lender bond) after
    this id, derived by hashing the serialized ``OutputReference`` of the spent pool (or
    request) UTxO. The out-ref is encoded exactly as the bond-mint redeemer encodes it
    (a ``TxOutRef`` ``Constr0([tx_id, index])``) so the derived name matches the mint.
    """
    tx_ref = TxOutRef(tx_id=bytes.fromhex(out_ref[0]), index=out_ref[1])
    return hashlib.blake2b(tx_ref.to_cbor(), digest_size=28).digest()
