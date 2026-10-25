"""The shared UTxO helpers: version-aware scripts, datum hashes, record conversion."""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest
from pycardano import Address
from pycardano import DatumHash
from pycardano import Network
from pycardano import PlutusV1Script
from pycardano import PlutusV2Script
from pycardano import PlutusV3Script
from pycardano import TransactionBuilder
from pycardano import TransactionOutput
from pycardano import VerificationKeyHash
from pycardano import plutus_script_hash

from charli3_dendrite.lending.transactions import common
from charli3_dendrite.lending.transactions import utxos
from charli3_dendrite.lending.transactions.infra import EvalContext
from charli3_dendrite.lending.transactions.utxos import Utxo
from tests.lending.liqwid.records import record_info
from tests.lending.liqwid.records import records_of

LIQWID_SCRIPTS = (
    Path(__file__).parents[1]
    / "liqwid"
    / "transactions"
    / "fixtures"
    / "scripts.json.gz"
)
# The Liqwid V2 Ada market's action validator (registry ``LiqwidAdaAnastasiaAuditAction``).
ADA_ACTION_HASH = "278825f2fbb67f3b2418fc239315f8911e4ddf1ddbb14f1f7c9e0393"

ADDRESS = Address(
    payment_part=VerificationKeyHash(bytes(range(28))),
    network=Network.MAINNET,
).encode()
TX_A = "aa" * 32
TX_B = "bb" * 32
POLICY = "cd" * 28
SCRIPT_BYTES = "4e4d01000033222220051200120011"


def _utxo(**overrides: object) -> Utxo:
    values: dict[str, object] = {
        "address": ADDRESS,
        "lovelace": 2_000_000,
        "assets": [],
        "datum": None,
        "out_ref": (TX_A, 0),
    }
    values.update(overrides)
    return Utxo(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("script_type", "cls"),
    [
        ("plutusV1", PlutusV1Script),
        ("plutusV2", PlutusV2Script),
        ("plutusV3", PlutusV3Script),
        (None, PlutusV3Script),
    ],
)
def test_plutus_script_follows_the_recorded_type(
    script_type: str | None,
    cls: type,
) -> None:
    script = utxos.plutus_script(
        _utxo(ref_script=SCRIPT_BYTES, ref_script_type=script_type),
    )
    assert type(script) is cls
    assert bytes(script).hex() == SCRIPT_BYTES


def test_plutus_script_needs_script_bytes() -> None:
    with pytest.raises(ValueError, match="carries no script"):
        utxos.plutus_script(_utxo(ref_script_type="plutusV2"))


def test_plutus_script_refuses_an_unknown_type() -> None:
    with pytest.raises(ValueError, match="timelock"):
        utxos.plutus_script(_utxo(ref_script=SCRIPT_BYTES, ref_script_type="timelock"))


def test_script_hash_of_hashes_by_language() -> None:
    v2 = _utxo(ref_script=SCRIPT_BYTES, ref_script_type="plutusV2")
    v3 = _utxo(ref_script=SCRIPT_BYTES, ref_script_type="plutusV3")
    raw = bytes.fromhex(SCRIPT_BYTES)
    assert (
        utxos.script_hash_of(v2)
        == plutus_script_hash(PlutusV2Script(raw)).payload.hex()
    )
    assert (
        utxos.script_hash_of(v3)
        == plutus_script_hash(PlutusV3Script(raw)).payload.hex()
    )
    assert utxos.script_hash_of(v2) != utxos.script_hash_of(v3)


def test_script_hash_of_a_stand_in_without_a_type() -> None:
    stand_in = _utxo(ref_script=b"stand-in oracle validator".hex())
    expected = plutus_script_hash(PlutusV3Script(b"stand-in oracle validator"))
    assert utxos.script_hash_of(stand_in) == expected.payload.hex()


def test_script_hash_of_needs_a_script() -> None:
    with pytest.raises(ValueError, match="carries no script"):
        utxos.script_hash_of(_utxo())


def test_script_hash_of_the_captured_ada_action_script() -> None:
    if not LIQWID_SCRIPTS.exists():
        pytest.skip("needs captured scripts")
    scripts = json.loads(gzip.decompress(LIQWID_SCRIPTS.read_bytes()))
    hashes = {
        utxos.script_hash_of(
            _utxo(
                ref_script=entry["ref_script"],
                ref_script_type=entry["ref_script_type"],
            ),
        ): entry
        for entry in scripts.values()
    }
    assert ADA_ACTION_HASH in hashes
    assert hashes[ADA_ACTION_HASH]["ref_script_type"] == "plutusV2"


def test_to_pycardano_utxo_keeps_v3_script_bytes() -> None:
    u = _utxo(ref_script=SCRIPT_BYTES, ref_script_type="plutusV3")
    output = utxos.to_pycardano_utxo(u).output
    assert isinstance(output.script, PlutusV3Script)
    assert bytes(output.script).hex() == SCRIPT_BYTES


def test_to_pycardano_utxo_types_a_v2_script() -> None:
    u = _utxo(ref_script=SCRIPT_BYTES, ref_script_type="plutusV2")
    output = utxos.to_pycardano_utxo(u).output
    assert isinstance(output.script, PlutusV2Script)


@pytest.mark.parametrize("script_type", ["timelock", "multisig"])
def test_to_pycardano_utxo_carries_a_native_script(script_type: str) -> None:
    native = "8200581c" + "00" * 28
    u = _utxo(ref_script=native, ref_script_type=script_type)
    output = utxos.to_pycardano_utxo(u).output
    assert isinstance(output.script, PlutusV3Script)
    assert bytes(output.script) == bytes.fromhex(native)


def test_to_pycardano_utxo_carries_a_datum_hash() -> None:
    u = _utxo(datum_hash="ee" * 32)
    output = utxos.to_pycardano_utxo(u).output
    assert output.datum_hash == DatumHash(bytes.fromhex("ee" * 32))
    assert output.datum is None


def test_to_pycardano_utxo_prefers_an_inline_datum() -> None:
    u = _utxo(datum="d87980", datum_hash="ee" * 32)
    output = utxos.to_pycardano_utxo(u).output
    assert output.datum_hash is None
    assert output.datum is not None


def test_ogmios_entry_tags_a_v2_script_and_a_datum_hash() -> None:
    u = _utxo(
        ref_script=SCRIPT_BYTES,
        ref_script_type="plutusV2",
        datum_hash="ee" * 32,
    )
    entry = utxos.ogmios_entry(u)
    assert entry["script"] == {"language": "plutus:v2", "cbor": SCRIPT_BYTES}
    assert entry["datumHash"] == "ee" * 32
    assert "datum" not in entry


def test_ogmios_entry_defaults_to_v3_and_omits_an_absent_hash() -> None:
    entry = utxos.ogmios_entry(_utxo(ref_script=SCRIPT_BYTES, datum="d87980"))
    assert entry["script"]["language"] == "plutus:v3"
    assert entry["datum"] == "d87980"
    assert "datumHash" not in entry


def test_ogmios_entry_sends_only_the_inline_datum() -> None:
    entry = utxos.ogmios_entry(_utxo(datum="d87980", datum_hash="ee" * 32))
    assert entry["datum"] == "d87980"
    assert "datumHash" not in entry


def test_utxo_from_dict_reads_the_datum_hash() -> None:
    record = {
        "address": ADDRESS,
        "lovelace": 1,
        "assets": [[POLICY, "", 2]],
        "datum": None,
        "datum_hash": "ee" * 32,
        "out_ref": [TX_A, 3],
    }
    u = utxos.utxo_from_dict(record)
    assert u.datum_hash == "ee" * 32
    assert u.out_ref == (TX_A, 3)
    assert utxos.utxo_from_dict({**record, "datum_hash": None}).datum_hash is None
    del record["datum_hash"]
    assert utxos.utxo_from_dict(record).datum_hash is None


def test_script_ref_by_hash_finds_a_v2_script() -> None:
    v3 = _utxo(ref_script=SCRIPT_BYTES, out_ref=(TX_A, 0))
    v2 = _utxo(ref_script=SCRIPT_BYTES, ref_script_type="plutusV2", out_ref=(TX_B, 1))
    found = utxos.script_ref_by_hash([_utxo(), v3, v2], utxos.script_hash_of(v2))
    assert found is v2
    with pytest.raises(ValueError, match="no reference script"):
        utxos.script_ref_by_hash([v3], utxos.script_hash_of(v2))


def test_utxo_from_info_reads_a_captured_liqwid_loan() -> None:
    rec = records_of("loan", live=True)[0]
    info = record_info(rec)
    u = utxos.utxo_from_info(info)
    assert u.address == info.address
    assert u.lovelace == info.assets["lovelace"]
    assert u.out_ref == (info.tx_hash, info.tx_index)
    assert isinstance(u.out_ref, tuple)
    assert u.datum == info.datum_cbor
    assert u.ref_script is None
    assert u.ref_script_type is None
    assert u.datum_hash is None
    expected = sorted(
        (unit[:56], unit[56:], qty)
        for unit, qty in info.assets.items()
        if unit != "lovelace"
    )
    assert u.assets == expected
    assert u.assets == sorted(u.assets, key=lambda a: (a[0], a[1]))


def test_utxo_from_info_maps_an_empty_datum_to_none() -> None:
    info = record_info(records_of("loan", live=True)[0], datum_cbor="")
    assert utxos.utxo_from_info(info).datum is None


def test_utxo_from_info_orders_assets_canonically() -> None:
    from charli3_dendrite.dataclasses.models import Assets
    from charli3_dendrite.dataclasses.models import PoolStateInfo

    low, high = "00" * 28, "ff" * 28
    info = PoolStateInfo(
        address=ADDRESS,
        tx_hash=TX_A,
        tx_index=4,
        block_time=0,
        block_index=0,
        block_hash="",
        datum_hash="",
        datum_cbor="",
        assets=Assets(
            root={high + "01": 1, "lovelace": 5, low + "02": 7, low: 3},
        ),
        plutus_v2=False,
    )
    u = utxos.utxo_from_info(info)
    assert u.lovelace == 5
    assert u.assets == [(low, "", 3), (low, "02", 7), (high, "01", 1)]


def test_utxo_to_info_inverts_utxo_from_info() -> None:
    info = record_info(records_of("loan", live=True)[0])
    u = utxos.utxo_from_info(info)
    back = utxos.utxo_to_info(u)
    assert back.address == info.address
    assert (back.tx_hash, back.tx_index) == (info.tx_hash, info.tx_index)
    assert back.datum_cbor == info.datum_cbor
    assert dict(back.assets.root) == dict(info.assets.root)
    assert (back.block_time, back.block_index, back.block_hash) == (0, 0, "")
    assert back.datum_hash == ""
    assert back.plutus_v2 is True
    assert utxos.utxo_from_info(back) == u


def test_utxo_to_info_needs_an_out_ref() -> None:
    with pytest.raises(ValueError, match="out-ref"):
        utxos.utxo_to_info(_utxo(out_ref=None))


def test_out_ref_of() -> None:
    assert common.out_ref_of(_utxo(out_ref=(TX_B, 2))) == (TX_B, 2)
    with pytest.raises(ValueError, match="out-ref"):
        common.out_ref_of(_utxo(out_ref=None))


def test_input_and_reference_indices_follow_ledger_order() -> None:
    builder = TransactionBuilder(EvalContext(last_block_slot=0))
    for u in (_utxo(out_ref=(TX_B, 0)), _utxo(out_ref=(TX_A, 1))):
        builder.add_input(utxos.to_pycardano_utxo(u))
    for u in (_utxo(out_ref=(TX_B, 5)), _utxo(out_ref=(TX_A, 9))):
        builder.reference_inputs.add(utxos.to_pycardano_utxo(u))
    assert common.input_index(builder, (TX_A, 1)) == 0
    assert common.input_index(builder, (TX_B, 0)) == 1
    with pytest.raises(ValueError, match="not found"):
        common.input_index(builder, (TX_A, 7))
    assert common.ref_index(builder) == {(TX_A, 9): 0, (TX_B, 5): 1}


def test_settled_min_ada_covers_its_own_output() -> None:
    address = Address.decode(ADDRESS)
    lovelace = common.settled_min_ada(address, {POLICY: 1}, None)
    output = TransactionOutput(
        address,
        utxos.utxo_value(lovelace, [(POLICY, "", 1)]),
    )
    assert common.min_ada(output) <= lovelace


def test_reward_address_is_a_mainnet_script_stake_address() -> None:
    raw = common.reward_address("ab" * 28)
    assert raw == bytes.fromhex("f1" + "ab" * 28)


def test_seam_modules_declare_their_exports() -> None:
    assert set(utxos.__all__) >= {
        "SCRIPT_LANG",
        "Utxo",
        "plutus_script",
        "script_hash_of",
        "utxo_value",
        "to_pycardano_utxo",
        "ogmios_entry",
        "utxo_from_dict",
        "utxo_from_info",
        "utxo_to_info",
        "script_ref_by_hash",
    }
    assert set(common.__all__) >= {
        "ref_index",
        "input_index",
        "reward_address",
        "out_ref_of",
        "min_ada",
        "settled_min_ada",
    }


_FT_UTXOS = (
    "SCRIPT_LANG",
    "Utxo",
    "utxo_value",
    "to_pycardano_utxo",
    "ogmios_entry",
    "utxo_from_dict",
    "script_ref_by_hash",
)


@pytest.mark.parametrize("name", _FT_UTXOS)
def test_fluidtokens_utxos_re_exports_the_seam(name: str) -> None:
    from charli3_dendrite.lending.fluidtokens.transactions import utxos as ft_utxos

    assert getattr(ft_utxos, name) is getattr(utxos, name)


@pytest.mark.parametrize("name", ["ref_index", "input_index", "reward_address"])
def test_fluidtokens_common_re_exports_the_seam(name: str) -> None:
    from charli3_dendrite.lending.fluidtokens.transactions import _common

    assert getattr(_common, name) is getattr(common, name)


@pytest.mark.parametrize(
    ("name", "seam"),
    [
        ("script_hash_of", utxos),
        ("out_ref_of", common),
        ("min_ada", common),
        ("settled_min_ada", common),
    ],
)
def test_fluidtokens_v4_common_re_exports_the_seam(name: str, seam: object) -> None:
    from charli3_dendrite.lending.fluidtokens_v4.transactions import common as v4

    assert getattr(v4, name) is getattr(seam, name)


def test_fluidtokens_context_aliases_still_resolve() -> None:
    from charli3_dendrite.lending.fluidtokens.transactions import context

    assert context.Utxo is Utxo
    assert context._to_utxo is utxos.to_pycardano_utxo
    assert context._as_utxo is utxos.utxo_from_dict


def test_seam_modules_import_no_protocol_package() -> None:
    for module in (utxos, common):
        source = Path(module.__file__ or "").read_text()
        assert "lending.fluidtokens" not in source
        assert "lending.danogo" not in source
        assert "lending.liqwid" not in source
