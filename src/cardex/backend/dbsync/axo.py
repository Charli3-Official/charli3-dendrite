from datetime import datetime

from cardex.dataclasses.models import Assets
from cardex.backend.dbsync.utils import db_query


def get_axo_target(assets: Assets, block_time: datetime | None = None) -> str | None:
    SELECTOR = """
SELECT DISTINCT txo.address, block.time
FROM (
	SELECT tx.id, tx.block_id
	FROM tx_out txo
	LEFT JOIN tx ON tx.id = txo.tx_id
	WHERE txo.payment_cred = DECODE('55ff0e63efa0694e8065122c552e80c7b51768b7f20917af25752a7c', 'hex')
) as tx
LEFT JOIN block ON block.id = tx.block_id
LEFT JOIN tx_out txo ON tx.id = txo.tx_id
LEFT JOIN ma_tx_out mtxo on txo.id = mtxo.tx_out_id
LEFT JOIN multi_asset ma ON ma.id = mtxo.ident
WHERE ma.policy = %(policy)b AND ma.name = %(name)b
AND txo.payment_cred != DECODE('55ff0e63efa0694e8065122c552e80c7b51768b7f20917af25752a7c', 'hex')"""

    if block_time is not None:
        SELECTOR += """
AND block.time <= %(block_time)s"""

    SELECTOR += """
ORDER BY block.time DESC"""

    policy = bytes.fromhex(assets.unit()[:56])
    name = bytes.fromhex(assets.unit()[56:])
    r = db_query(
        SELECTOR,
        {
            "policy": policy,
            "name": name,
            "block_time": None
            if block_time is None
            else block_time.strftime("%Y-%m-%d %H:%M:%S"),
        },
    )

    if len(r) == 0:
        return None

    return r[0]["address"]
