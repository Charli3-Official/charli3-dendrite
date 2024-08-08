from pycardano import Address

from cardex.backend.dbsync.models import UTxOSelector
from cardex.backend.dbsync.utils import db_query
from cardex.dataclasses.models import ScriptReference


def get_script_from_address(address: Address) -> ScriptReference:
    SCRIPT_SELECTOR = UTxOSelector.select()

    SCRIPT_SELECTOR += """
FROM script s
LEFT JOIN tx_out ON s.id = tx_out.reference_script_id
LEFT JOIN tx ON tx.id = tx_out.tx_id
LEFT JOIN datum ON tx_out.inline_datum_id = datum.id
LEFT JOIN block on block.id = tx.block_id
LEFT JOIN tx_in ON tx_in.tx_out_id = tx_out.tx_id AND tx_in.tx_out_index = tx_out.index
WHERE s.hash = %(address)b AND tx_in.tx_in_id IS NULL
ORDER BY block.time DESC
LIMIT 1
"""
    r = db_query(SCRIPT_SELECTOR, {"address": address.payment_part.payload})

    if r[0]["assets"] is not None and r[0]["assets"][0]["lovelace"] is None:
        r[0]["assets"] = None

    return UTxOSelector.parse(r[0])


def get_datum_from_address(
    address: Address,
    asset: str | None = None,
) -> ScriptReference:
    kwargs = {"address": address.payment_part.payload}

    if asset is not None:
        kwargs.update(
            {
                "policy": bytes.fromhex(asset[:56]),
                "name": bytes.fromhex(asset[56:]),
            },
        )

    SCRIPT_SELECTOR = UTxOSelector.select()

    SCRIPT_SELECTOR += """
FROM tx_out
LEFT JOIN ma_tx_out mtxo ON mtxo.tx_out_id = tx_out.id
LEFT JOIN multi_asset ma ON ma.id = mtxo.ident
LEFT JOIN tx ON tx.id = tx_out.tx_id
LEFT JOIN datum ON tx_out.inline_datum_id = datum.id
LEFT JOIN block on block.id = tx.block_id
LEFT JOIN script s ON s.id = tx_out.reference_script_id
LEFT JOIN tx_in txi ON tx_out.tx_id = txi.tx_out_id AND tx_out.index = txi.tx_out_index
WHERE tx_out.payment_cred = %(address)b AND txi.tx_in_id IS NULL"""

    if asset is not None:
        SCRIPT_SELECTOR += """
AND policy = %(policy)b AND name = %(name)b
"""

    SCRIPT_SELECTOR += """
AND tx_out.inline_datum_id IS NOT NULL
ORDER BY block.time DESC
LIMIT 1
"""
    r = db_query(SCRIPT_SELECTOR, kwargs)

    if r[0]["assets"] is not None and r[0]["assets"][0]["lovelace"] is None:
        r[0]["assets"] = None

    return UTxOSelector.parse(r[0])
