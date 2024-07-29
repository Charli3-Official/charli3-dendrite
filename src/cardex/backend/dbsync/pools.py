from pycardano import Address

from cardex.backend.dbsync.models import PoolSelector
from cardex.backend.dbsync.utils import db_query
from cardex.dataclasses.models import PoolStateList


def get_pool_utxos(
    addresses: list[str],
    assets: list[str] | None = None,
    limit: int = 1000,
    page: int = 0,
    historical: bool = True,
) -> PoolStateList:
    """Get transactions by policy or address.

    Args:
        addresses: A list of addresses for pool or order contracts
        assets: A list of assets used to filter utxos. Defaults to None.
        limit: Number of values to return. Defaults to 1000.
        page: Page of pools to return. Defaults to 0.
        historical: If False, returns current pool states. Defaults to True.

    Returns:
        A list of pool states.
    """
    error_msg = "Either policies or addresses must be defined, not both."

    # Use the pool selector to format the output
    datum_selector = PoolSelector.select()

    # Get txo from pool script address
    datum_selector += """FROM (
    SELECT *
    FROM tx_out
    WHERE tx_out.payment_cred = ANY(%(addresses)b)
) as txo"""

    # If assets are specified, select assets
    if assets is not None:
        datum_selector += """
LEFT JOIN ma_tx_out mtxo ON mtxo.tx_out_id = txo.id
LEFT JOIN multi_asset ma ON ma.id = mtxo.ident
"""

    datum_selector += """
LEFT JOIN tx ON txo.tx_id = tx.id
LEFT JOIN datum ON txo.data_hash = datum.hash
LEFT JOIN block ON tx.block_id = block.id
LEFT JOIN tx_in ON tx_in.tx_out_id = txo.tx_id AND tx_in.tx_out_index = txo.index
WHERE datum.hash IS NOT NULL"""

    if not historical:
        datum_selector += """
AND tx_in.tx_in_id IS NULL"""

    if assets is not None:
        datum_selector += """
AND ma.policy = ANY(%(policies)b) AND ma.name = ANY(%(names)b)"""

    datum_selector += """
LIMIT %(limit)s
OFFSET %(offset)s"""

    values = {
        "limit": limit,
        "offset": page * limit,
        "addresses": [Address.decode(a).payment_part.payload for a in addresses],
    }
    if assets is not None:
        values.update({"policies": [bytes.fromhex(p[:56]) for p in assets]})
        values.update({"names": [bytes.fromhex(p[56:]) for p in assets]})

    r = db_query(datum_selector, values)

    output = PoolSelector.parse(r)

    return output


def get_pool_in_tx(
    tx_hash: str,
    assets: list[str] | None = None,
    addresses: list[str] | None = None,
) -> PoolStateList:
    """Get transactions by policy or address."""

    # Use the pool selector to format the output
    datum_selector = PoolSelector.select()

    datum_selector += """FROM (
    SELECT *
    FROM tx_out
    WHERE tx_out.payment_cred = ANY(%(addresses)b)
) as txo"""

    # If assets are specified, select assets
    if assets is not None:
        datum_selector += """
LEFT JOIN ma_tx_out mtxo ON ma.id = mtxo.ident
LEFT JOIN tx_out txo ON mtxo.tx_out_id = txo.id
"""

    datum_selector += """
LEFT JOIN tx ON txo.tx_id = tx.id
LEFT JOIN datum ON txo.data_hash = datum.hash
LEFT JOIN block ON tx.block_id = block.id
WHERE datum.hash IS NOT NULL AND tx.hash = DECODE(%(tx_hash)s, 'hex')
"""

    if assets is not None:
        datum_selector += """
AND ma.policy = ANY(%(policies)b) AND ma.name = ANY(%(names)b)"""

    values = {
        "tx_hash": tx_hash,
        "addresses": [Address.decode(a).payment_part.payload for a in addresses],
    }
    if assets is not None:
        values.update({"policies": [bytes.fromhex(p[:56]) for p in assets]})
        values.update({"names": [bytes.fromhex(p[56:]) for p in assets]})

    r = db_query(datum_selector, values)

    return PoolSelector.parse(r)


def get_pool_utxos_in_block(block_no: int) -> PoolStateList:
    """Get pool utxos in block."""
    # Use this for gathering all assets for multiple addresses
    datum_selector = (
        PoolSelector.select()
        + """
FROM tx_out txo
LEFT JOIN tx ON txo.tx_id = tx.id
LEFT JOIN datum ON txo.data_hash = datum.hash
LEFT JOIN block ON tx.block_id = block.id
WHERE block.block_no = %(block_no)s AND datum.hash IS NOT NULL
"""
    )
    r = db_query(datum_selector, {"block_no": block_no})

    return PoolSelector.parse(r)
