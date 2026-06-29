from charli3_dendrite.lending.danogo.oracles.aggregator_datums import (
    OracleGlobalConfig,
)
from charli3_dendrite.lending.danogo.oracles.aggregator_datums import OraclePathDatum


def test_path_datum_roundtrip():
    d = OraclePathDatum(
        anchor=bytes.fromhex("ab" * 28),
        price_paths=[bytes.fromhex("0102"), bytes.fromhex("0304")],
        oracle_sources=[bytes.fromhex("00")],
        deviation_bps=500,
    )
    back = OraclePathDatum.from_cbor(d.to_cbor_hex())
    assert back == d
    assert back.deviation_bps == 500
    assert back.anchor == bytes.fromhex("ab" * 28)
    assert len(back.price_paths) == 2


def test_global_config_is_packed_bytes():
    # Top-level CBOR is a bare byte string (0x4N..), not a Constr.
    packed = bytes(range(20))
    cbor_hex = "54" + packed.hex()  # 0x54 = byte string of length 20
    g = OracleGlobalConfig.from_cbor(cbor_hex)
    assert g.raw == packed
