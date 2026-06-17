"""Known-answer tests for the Splash spot-order beacon derivation.

The fixtures are taken verbatim from the Splash protocol SDK
(``protocol-sdk`` ``spotOrderDatum.spec.ts`` and ``spotOrderBeacon.spec.ts``),
so a passing test means dendrite reproduces the encoding and beacon that the
on-chain contract expects.
"""

from charli3_dendrite.dataclasses.datums import AssetClass
from charli3_dendrite.dataclasses.datums import PlutusFullAddress
from charli3_dendrite.dexs.amm.splash import Rationale
from charli3_dendrite.dexs.amm.splash import SplashOrderDatum
from charli3_dendrite.dexs.amm.splash import _canonical_plutus_cbor
from pycardano import Address
from pycardano import Network
from pycardano import VerificationKeyHash

EXECUTOR = bytes.fromhex(
    "5cb2c968e5d1c7197a6ce7615967310a375545d9bc65063a964335b2",
)
PAYMENT_KH = "74104cd5ca6288c1dd2e22ee5c874fdcfc1b81897462d91153496430"
STAKE_KH = "de7866fe5068ebf3c87dcdb568da528da5dcb5f659d9b60010e7450f"
SEED_TX = "02b493cbd8eb40c3d5ed58372949c6d7c48afcf77416cf25189b118f53af19a4"
EXPECTED_BEACON = "384dc40d3a4361798c5b9eba65245ce972c6b6807034ab51b098d573"


def _address() -> Address:
    return Address(
        payment_part=VerificationKeyHash(bytes.fromhex(PAYMENT_KH)),
        staking_part=VerificationKeyHash(bytes.fromhex(STAKE_KH)),
        network=Network.MAINNET,
    )


def test_spot_order_datum_canonical_cbor():
    """Canonical CBOR matches the SDK ``spotOrderDatum.spec.ts`` fixture."""
    datum = SplashOrderDatum(
        tag=bytes.fromhex("00"),
        beacon=bytes.fromhex(
            "73fc8e44a4c04433c4e5870982e7d94867e9be28e501bff03e4ac0cf",
        ),
        in_asset=AssetClass(
            policy=bytes.fromhex(
                "fb4f75d1ad4eb5c21efd5a32a90c076e63a79daccf25afe4ccd4f714",
            ),
            asset_name=bytes.fromhex("24504f50534e454b"),
        ),
        tradable_input=998457,
        cost_per_ex_step=900000,
        min_marginal_output=249614250,
        output=AssetClass(policy=b"", asset_name=b""),
        base_price=Rationale(numerator=1000, denominator=1),
        fee=0,
        redeemer_address=PlutusFullAddress.from_address(_address()),
        cancel_pkh=bytes.fromhex(PAYMENT_KH),
        permitted_executors=[EXECUTOR],
    )

    expected = (
        "d8798c4100581c73fc8e44a4c04433c4e5870982e7d94867e9be28e501bff03e4ac0cf"
        "d87982581cfb4f75d1ad4eb5c21efd5a32a90c076e63a79daccf25afe4ccd4f714"
        "4824504f50534e454b1a000f3c391a000dbba01a0ee0cfaad879824040"
        "d879821903e80100d87982d87981581c74104cd5ca6288c1dd2e22ee5c874fdc"
        "fc1b81897462d91153496430d87981d87981d87981581cde7866fe5068ebf3c8"
        "7dcdb568da528da5dcb5f659d9b60010e7450f581c74104cd5ca6288c1dd2e22"
        "ee5c874fdcfc1b81897462d9115349643081581c5cb2c968e5d1c7197a6ce761"
        "5967310a375545d9bc65063a964335b2"
    )

    assert _canonical_plutus_cbor(datum).hex() == expected


def _beacon_fixture_datum() -> SplashOrderDatum:
    return SplashOrderDatum(
        tag=bytes.fromhex("00"),
        beacon=bytes(28),
        in_asset=AssetClass(policy=b"", asset_name=b""),
        tradable_input=1000000,
        cost_per_ex_step=900000,
        min_marginal_output=138502,
        output=AssetClass(
            policy=bytes.fromhex(
                "cebbd6a8ca954b7fc7a346d0baed4182e0358059f38065de279fb822",
            ),
            asset_name=bytes.fromhex("43617264616e6f20436174"),
        ),
        base_price=Rationale(
            numerator=13850243829651554,
            denominator=100000000000000000,
        ),
        fee=0,
        redeemer_address=PlutusFullAddress.from_address(_address()),
        cancel_pkh=bytes.fromhex(PAYMENT_KH),
        permitted_executors=[EXECUTOR],
    )


def test_compute_beacon():
    """Beacon matches the SDK ``spotOrderBeacon.spec.ts`` fixture."""
    beacon = _beacon_fixture_datum().compute_beacon(
        SEED_TX,
        seed_index=1,
        order_index=0,
    )

    assert beacon.hex() == EXPECTED_BEACON


def test_compute_beacon_accepts_bytes_tx_hash():
    """A bytes seed tx hash gives the same result as the hex form."""
    datum = _beacon_fixture_datum()

    assert datum.compute_beacon(SEED_TX, 1, 0) == datum.compute_beacon(
        bytes.fromhex(SEED_TX),
        1,
        0,
    )


def test_with_beacon_is_idempotent():
    """``with_beacon`` zeroes the field internally, so re-stamping is stable."""
    stamped = _beacon_fixture_datum().with_beacon(SEED_TX, 1, 0)

    assert stamped.beacon.hex() == EXPECTED_BEACON
    # recomputing on the already-stamped datum yields the same beacon
    assert stamped.with_beacon(SEED_TX, 1, 0).beacon == stamped.beacon
