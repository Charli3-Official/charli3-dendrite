"""End-to-end `snapshot` over a fake backend serving real mainnet UTxOs.

Exercises discover -> fetch -> parse -> stitch -> build_book without a network by
replaying captured Danogo Float UTxOs (ADA market): the protocol-config datum, the
market-param UTxO, the pool-state UTxO, and an ADA-borrowing loan.
"""

import json
from pathlib import Path

from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dataclasses.models import PoolStateInfo
from charli3_dendrite.dataclasses.models import PoolStateList
from charli3_dendrite.lending.danogo import constants
from charli3_dendrite.lending.danogo.datums import ProtocolDatum
from charli3_dendrite.lending.danogo.loader import snapshot

FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures" / "mainnet_datums.json").read_text()
)


def _assets(asset_rows, value=2_000_000):
    root = {"lovelace": value}
    for policy, name, qty in asset_rows:
        unit = "lovelace" if not policy and not name else policy + name
        root[unit] = qty
    return Assets(root=root)


def _info(address, asset_rows, datum, value=2_000_000):
    return PoolStateInfo(
        address=address,
        tx_hash="ab",
        tx_index=0,
        block_time=1_781_900_000,
        block_index=1,
        block_hash="cd",
        datum_hash="00",
        datum_cbor=datum,
        assets=_assets(asset_rows, value),
        plutus_v2=True,
    )


def _loan_rows_for_market(loan_fixture, loan_skh, market_name):
    """Point a captured loan's identity NFT at `market_name` (the served market).

    A loan is stitched to the market its loan-identity NFT (policy == loan script hash)
    names. Captured loans carry the NFT for their OWN market; these fixtures serve a
    single market, so retarget the identity NFT to it to exercise the name-keyed stitch.
    """
    return [
        (policy, market_name if policy == loan_skh else name, qty)
        for policy, name, qty in loan_fixture["assets"]
    ]


class _FakeBackend:
    """Dispatches get_pool_utxos by config-NFT (resolve) or payment credential."""

    def __init__(self, by_cred, config_row):
        self._by_cred = by_cred
        self._config_row = config_row

    def get_pool_utxos(
        self, addresses, assets=None, limit=1000, page=0, historical=True
    ):
        if assets and constants.PROTOCOL_CONFIG_NFT in assets:
            return PoolStateList(root=[self._config_row])
        from pycardano import Address

        cred = Address.decode(addresses[0]).payment_part.payload.hex()
        return PoolStateList(root=self._by_cred.get(cred, []))


def _backend():
    pd = ProtocolDatum.from_cbor(FIXTURES["protocol_config"]["datum"])
    pool_addr = constants._addr(pd.pool_skh.hex())
    config_pool_addr = constants._addr(pd.config_pool_skh.hex())
    loan_addr = constants._addr(pd.loan_skh.hex())

    config_pool_skh = pd.config_pool_skh.hex()
    market_name = next(
        n for p, n, _ in FIXTURES["market_param"]["assets"] if p == config_pool_skh
    )

    config_row = _info(config_pool_addr, [], FIXTURES["protocol_config"]["datum"])
    by_cred = {
        config_pool_skh: [
            _info(
                config_pool_addr,
                FIXTURES["market_param"]["assets"],
                FIXTURES["market_param"]["datum"],
            )
        ],
        pd.pool_skh.hex(): [
            _info(
                pool_addr,
                FIXTURES["pool_state"]["assets"],
                FIXTURES["pool_state"]["datum"],
            )
        ],
        pd.loan_skh.hex(): [
            _info(
                loan_addr,
                _loan_rows_for_market(
                    FIXTURES["ada_loan"], pd.loan_skh.hex(), market_name
                ),
                FIXTURES["ada_loan"]["datum"],
            )
        ],
    }
    return _FakeBackend(by_cred, config_row)


def test_snapshot_builds_book_from_real_utxos():
    book = snapshot(_backend(), now_ms=1_781_900_000_000)

    # One ADA market/pool discovered and paired.
    assert book.pool is not None
    assert book.pool.borrowable_unit == "lovelace"
    assert book.pool.market.supply_token == "lovelace"
    assert len(book.pool.market.collaterals) == 44

    # The ADA-borrowing loan stitched in, with debt accrued from the pool datum.
    active = book.active_loans()
    assert len(active) == 1
    loan = active[0]
    assert loan.borrowed_unit == "lovelace"
    assert loan.current_debt() >= FIXTURES["ada_loan"]["loan_amount"]


def test_snapshot_skips_loans_without_matching_market():
    # Only the ADA market is served; a USDM-borrowing loan must be dropped, not crash.
    backend = _backend()
    backend._by_cred[
        ProtocolDatum.from_cbor(FIXTURES["protocol_config"]["datum"]).loan_skh.hex()
    ].append(
        _info(
            constants._addr(
                ProtocolDatum.from_cbor(
                    FIXTURES["protocol_config"]["datum"]
                ).loan_skh.hex()
            ),
            FIXTURES["loans"][0]["assets"],
            FIXTURES["loans"][0]["datum"],
        )
    )
    book = snapshot(backend, now_ms=1_781_900_000_000)
    # USDM loan skipped (no USDM market served); only the ADA loan remains.
    assert all(ln.borrowed_unit == "lovelace" for ln in book.active_loans())
    assert len(book.active_loans()) == 1
