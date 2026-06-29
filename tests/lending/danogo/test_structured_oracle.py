"""Structured oracle config decoding, the deployment registry, and config selection.

Covers the newer Danogo oracle deployment's structured ``Constr0`` global / path config
datums (byte-exact round-trip), the ``oracle_skh``-keyed deployment registry, and the
deployment-aware global / path config selection -- while asserting the original (packed)
deployment's selection is byte-identical.
"""

from __future__ import annotations

import json
from pathlib import Path

import cbor2  # type: ignore[import-not-found]
import pytest

from charli3_dendrite.lending.danogo.oracles.deployments import (
    deployment_for_oracle_skh,
)
from charli3_dendrite.lending.danogo.oracles.structured_datums import (
    StructuredOracleGlobalConfig,
)
from charli3_dendrite.lending.danogo.oracles.structured_datums import (
    StructuredOraclePathConfig,
)
from charli3_dendrite.lending.danogo.transactions._common import _select_global_config
from charli3_dendrite.lending.danogo.transactions._common import _select_path_config

FIXTURES_DIR = Path(__file__).parent / "fixtures"

# The structured (newer) deployment's collateral-ADD repay fixture: ref input idx5 is
# the structured global source config, idx6 the structured USDM routing path config.
ADD_FIXTURE = "decrease_loan_add_collat_tx.json"
PARTIAL_FIXTURE = "decrease_loan_partial_tx.json"

_PACKED_ORACLE_SKH = "012a6bd4ae76261c1d3b5067caa4010f781f5c1c64ce2779bba2f90a"
_STRUCTURED_ORACLE_SKH = "2eb7e9be6a1fff3e3e33d2b05007488f199c895a051b3ee371a95f6c"

# The packed deployment's global-config NFT (the long-standing anchor) -- the original
# deployment must keep selecting exactly this UTxO.
_PACKED_GLOBAL_NFT = (
    "bd23e2977937bb6054f625e5b566e28e294dcd8f7e8d0d33979ed90c"
    "1e5e4d7ac0ba99f5f4b3d3c7764054ccce87ef497407785a4ee04d2beadb0b78"
)
_STRUCTURED_GLOBAL_NFT = (
    "bd23e2977937bb6054f625e5b566e28e294dcd8f7e8d0d33979ed90c"
    "b9205bb92e33323ee4477966237ec461a99ce528255cdd843dd84017f5224872"
)
_STRUCTURED_USDM_PATH_NFT = (
    "bd23e2977937bb6054f625e5b566e28e294dcd8f7e8d0d33979ed90c"
    "5b5efe00c6871eb8aa46a129ef97e8e300e81de4acb76bc05d55c09a567dfec4"
)

# The USDM supply token the structured ADD fixture's market / path config prices toward.
_USDM_SUPPLY = (
    "c48cbb3d5e57ed56e276bc45f99ab39abe94e6cd7ac39fb402da47ad" "0014df105553444d"
)
# The dToken collateral whose price the ADD redeemer carries, and its routing path
# (`source 24` forward then `source 18` reversed) -- confirmed against the redeemer.
_DTOKEN = (
    "94dca24a1f1fcc2ff51cd90f32f4fe9e786d861a2dbf7d27598d26e8",
    "f04403181fbd051edd971af67b85f6c6fe1d9d98949a80b9f3803a14",
)


def _ref_input_datum(fixture: str, index: int) -> bytes:
    fix = json.loads((FIXTURES_DIR / fixture).read_text())
    return bytes.fromhex(fix["ref_inputs"][index]["datum"])


def test_structured_global_config_roundtrips_byte_exact() -> None:
    raw = _ref_input_datum(ADD_FIXTURE, 5)
    config = StructuredOracleGlobalConfig.from_cbor(raw)
    assert config.to_cbor() == raw
    assert config.deviation_bps == 500
    assert len(config.sources) == 92


def test_structured_global_source_locators_resolve_redeemer_sources() -> None:
    config = StructuredOracleGlobalConfig.from_cbor(_ref_input_datum(ADD_FIXTURE, 5))
    # Path hops `24` (Danogo pool) and `18` (Indigo) name concrete on-chain tokens that
    # match the Danogo pool / Indigo reference inputs the ADD redeemer references.
    assert config.sources[24].locator() == (
        "814de8a99452972a9fa9fe2c0f59f49697f208005c001ecac1ddfd57",
        "f04403181fbd051edd971af67b85f6c6fe1d9d98949a80b9f3803a14",
    )
    assert config.sources[18].locator() == (
        "e3455f2715338b454fb853442f72dc03b98396854f97510027fe22ff",
        "695553443230323231313138313935393036",
    )


def test_structured_path_config_roundtrips_byte_exact() -> None:
    raw = _ref_input_datum(ADD_FIXTURE, 6)
    config = StructuredOraclePathConfig.from_cbor(raw)
    assert config.to_cbor() == raw
    assert config.supply_unit == _USDM_SUPPLY


def test_structured_path_config_dtoken_and_lovelace_paths() -> None:
    config = StructuredOraclePathConfig.from_cbor(_ref_input_datum(ADD_FIXTURE, 6))
    by_token = {(e.policy.hex(), e.name.hex()): e for e in config.entries}
    assert by_token[_DTOKEN].paths == (((24, False), (18, True)),)
    lovelace = next(e for e in config.entries if e.unit == "lovelace")
    assert lovelace.paths == (((18, True),),)


def test_packed_global_config_datum_is_not_structured() -> None:
    # The original deployment's global config is a packed byte string, not a Constr0,
    # so the structured decoder rejects it (decode kinds stay disjoint).
    raw = _ref_input_datum(PARTIAL_FIXTURE, 6)
    with pytest.raises(ValueError):
        StructuredOracleGlobalConfig.from_cbor(raw)


def test_deployment_registry_resolves_both_deployments() -> None:
    packed = deployment_for_oracle_skh(_PACKED_ORACLE_SKH)
    assert packed.path_config_kind == "packed"
    assert packed.path_selection == "anchored"
    assert packed.global_config_nft == _PACKED_GLOBAL_NFT

    structured = deployment_for_oracle_skh(_STRUCTURED_ORACLE_SKH)
    assert structured.path_config_kind == "structured"
    assert structured.path_selection == "supply_token"
    assert structured.global_config_nft == _STRUCTURED_GLOBAL_NFT


def test_deployment_registry_raises_on_unknown_skh() -> None:
    with pytest.raises(ValueError):
        deployment_for_oracle_skh("00" * 28)


def test_select_global_config_structured_deployment(repay_snap) -> None:  # noqa: ANN001
    _fix, snapshot = repay_snap(ADD_FIXTURE)
    selected = _select_global_config(snapshot)
    assert selected.holds(_STRUCTURED_GLOBAL_NFT[:56], _STRUCTURED_GLOBAL_NFT[56:])


def test_select_global_config_packed_deployment_unchanged(
    repay_snap,
) -> None:  # noqa: ANN001
    _fix, snapshot = repay_snap(PARTIAL_FIXTURE)
    selected = _select_global_config(snapshot)
    assert selected.holds(_PACKED_GLOBAL_NFT[:56], _PACKED_GLOBAL_NFT[56:])


def test_select_path_config_structured_by_supply_token(
    repay_snap,
) -> None:  # noqa: ANN001
    _fix, snapshot = repay_snap(ADD_FIXTURE)
    selected = _select_path_config(
        snapshot,
        supply_token=snapshot.market_info.supply_token,
        used_leaves=[],
    )
    assert selected.holds(
        _STRUCTURED_USDM_PATH_NFT[:56],
        _STRUCTURED_USDM_PATH_NFT[56:],
    )
    # The selected UTxO's datum is the USDM structured path config.
    assert (
        StructuredOraclePathConfig.from_cbor(selected.datum).supply_unit == _USDM_SUPPLY
    )


def test_encode_plutus_uses_indefinite_arrays_and_maps() -> None:
    # Guards the on-chain CBOR convention the byte-exact round-trip depends on: a
    # non-empty array is indefinite-length and an empty Constr payload is a definite
    # empty array.
    from charli3_dendrite.lending.danogo.oracles.structured_datums import encode_plutus

    assert encode_plutus([1, 2]) == b"\x9f\x01\x02\xff"
    assert encode_plutus(cbor2.CBORTag(121, [])) == b"\xd8\x79\x80"
