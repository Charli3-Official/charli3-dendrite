"""The dbsync pool UTxO query pages over a deterministic order (offline)."""

import pytest

from charli3_dendrite.backend.dbsync import DbsyncBackend
from charli3_dendrite.lending.units import script_payment_address

ADDRESS = script_payment_address("00" * 28)
ASSET = "11" * 28 + "22"


@pytest.mark.parametrize("historical", [True, False])
@pytest.mark.parametrize("assets", [None, [ASSET]])
def test_pool_utxo_query_orders_before_it_limits(historical, assets):
    backend = DbsyncBackend.__new__(DbsyncBackend)
    query, values = backend._get_pool_utxos(
        addresses=[ADDRESS],
        assets=assets,
        limit=10,
        page=2,
        historical=historical,
    )
    tail = query.rstrip().splitlines()[-3:]
    assert tail == ["ORDER BY txo.id", "LIMIT %(limit)s", "OFFSET %(offset)s"]
    assert (values["limit"], values["offset"]) == (10, 20)
