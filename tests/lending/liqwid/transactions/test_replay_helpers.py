"""The Liqwid replay helpers: fixture loading, dbsync settings and replay views."""

from __future__ import annotations

import ast
import copy
import re
from pathlib import Path
from typing import Any

import pytest
from cbor2 import CBORTag
from pycardano import Address
from pycardano import Asset
from pycardano import AssetName
from pycardano import DatumHash
from pycardano import MultiAsset
from pycardano import Network
from pycardano import PlutusV2Script
from pycardano import RawCBOR
from pycardano import RawPlutusData
from pycardano import Redeemer
from pycardano import TransactionBuilder
from pycardano import TransactionId
from pycardano import TransactionInput
from pycardano import TransactionOutput
from pycardano import UTxO
from pycardano import Value
from pycardano import VerificationKeyHash
from pycardano import Withdrawals
from pycardano import plutus_script_hash

from tests.lending.liqwid.transactions import replay
from tests.lending.liqwid.transactions.replay import Built
from tests.lending.liqwid.transactions.replay import OutputView
from tests.lending.liqwid.transactions.replay import assert_replays
from tests.lending.liqwid.transactions.replay import attach_scripts
from tests.lending.liqwid.transactions.replay import build
from tests.lending.liqwid.transactions.replay import captured_mints
from tests.lending.liqwid.transactions.replay import captured_protocol_outputs
from tests.lending.liqwid.transactions.replay import captured_redeemers
from tests.lending.liqwid.transactions.replay import captured_reference_inputs
from tests.lending.liqwid.transactions.replay import captured_required_signers
from tests.lending.liqwid.transactions.replay import captured_validity
from tests.lending.liqwid.transactions.replay import captured_withdrawals
from tests.lending.liqwid.transactions.live import dbsync_backend
from tests.lending.liqwid.transactions.live import dsn_parts
from tests.lending.liqwid.transactions.replay import dbsync_connect_kwargs
from tests.lending.liqwid.transactions.replay import detach_scripts
from tests.lending.liqwid.transactions.replay import fixture
from tests.lending.liqwid.transactions.replay import fixture_utxos
from tests.lending.liqwid.transactions.replay import labels
from tests.lending.liqwid.transactions.replay import ogmios_entries
from tests.lending.liqwid.transactions.replay import mints
from tests.lending.liqwid.transactions.replay import protocol_outputs
from tests.lending.liqwid.transactions.replay import redeemers
from tests.lending.liqwid.transactions.replay import reference_inputs
from tests.lending.liqwid.transactions.replay import required_signers
from tests.lending.liqwid.transactions.replay import validity
from tests.lending.liqwid.transactions.replay import withdrawals

CAPTURE = Path(replay.__file__).parent / "fixtures" / "_capture_txs.py"
DBSYNC_PARTS = (
    "DBSYNC_DSN",
    "DBSYNC_HOST",
    "DBSYNC_PORT",
    "DBSYNC_DB_NAME",
    "DBSYNC_USER",
    "DBSYNC_PASS",
)

# --- dbsync connection settings -------------------------------------------------


@pytest.fixture()
def no_dbsync_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in DBSYNC_PARTS:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_a_quoted_driver_dsn_becomes_a_plain_conninfo(no_dbsync_env):
    no_dbsync_env.setenv("DBSYNC_DSN", '"postgresql+asyncpg://u:p@h:5432/db"')
    assert dbsync_connect_kwargs() == {"conninfo": "postgresql://u:p@h:5432/db"}


def test_a_plain_dsn_is_kept(no_dbsync_env):
    no_dbsync_env.setenv("DBSYNC_DSN", "postgresql://u:p@h:5432/db")
    assert dbsync_connect_kwargs() == {"conninfo": "postgresql://u:p@h:5432/db"}


def test_a_single_quoted_dsn_is_unquoted(no_dbsync_env):
    no_dbsync_env.setenv("DBSYNC_DSN", "'postgresql+psycopg://u:p@h/db'")
    assert dbsync_connect_kwargs() == {"conninfo": "postgresql://u:p@h/db"}


def test_without_a_dsn_the_parts_default_as_the_v3_capture(no_dbsync_env):
    assert dbsync_connect_kwargs() == {
        "host": "localhost",
        "port": 5432,
        "dbname": "cexplorer",
        "user": "cexplorer",
        "password": "",
        "connect_timeout": 30,
    }


def test_without_a_dsn_the_parts_are_read(no_dbsync_env):
    no_dbsync_env.setenv("DBSYNC_HOST", "db.example")
    no_dbsync_env.setenv("DBSYNC_PORT", "6543")
    no_dbsync_env.setenv("DBSYNC_DB_NAME", "chain")
    no_dbsync_env.setenv("DBSYNC_USER", "reader")
    no_dbsync_env.setenv("DBSYNC_PASS", "secret")
    assert dbsync_connect_kwargs() == {
        "host": "db.example",
        "port": 6543,
        "dbname": "chain",
        "user": "reader",
        "password": "secret",
        "connect_timeout": 30,
    }


@pytest.fixture()
def restored_dbsync_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """No ``DBSYNC_*`` variable set; each is restored after the test."""
    for name in DBSYNC_PARTS:
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)
    return monkeypatch


def test_a_dsn_gives_the_backend_variables():
    assert dsn_parts("postgresql://u:p@h:6543/db") == {
        "DBSYNC_HOST": "h",
        "DBSYNC_PORT": "6543",
        "DBSYNC_DB_NAME": "db",
        "DBSYNC_USER": "u",
        "DBSYNC_PASS": "p",
    }
    assert dsn_parts("postgresql://h/db")["DBSYNC_PORT"] == "5432"


def test_the_live_backend_reads_the_dsn(restored_dbsync_env):
    restored_dbsync_env.setenv("DBSYNC_DSN", '"postgresql+asyncpg://u:p@h:6543/db"')
    backend = dbsync_backend()
    assert (
        backend.DBSYNC_HOST,
        backend.DBSYNC_PORT,
        backend.DBSYNC_DB_NAME,
        backend.DBSYNC_USER,
        backend.DBSYNC_PASS,
    ) == ("h", "6543", "db", "u", "p")
    assert backend.POOL is None  # nothing is connected until the first query


def test_without_a_dsn_the_live_backend_reads_the_parts(restored_dbsync_env):
    restored_dbsync_env.setenv("DBSYNC_HOST", "db.example")
    restored_dbsync_env.setenv("DBSYNC_USER", "reader")
    backend = dbsync_backend()
    assert (backend.DBSYNC_HOST, backend.DBSYNC_USER) == ("db.example", "reader")


# --- fixture loading ------------------------------------------------------------

SCRIPT_HEX = "4e4d01000033222220051200120011"
REF_TX = "aa" * 32
WALLET = str(
    Address(payment_part=VerificationKeyHash(b"\x07" * 28), network=Network.MAINNET),
)


def _utxo_dict(
    out_ref: list[Any],
    *,
    ref_script: str | None = None,
    ref_script_type: str | None = None,
) -> dict[str, Any]:
    return {
        "address": WALLET,
        "lovelace": 2_000_000,
        "assets": [],
        "datum": None,
        "ref_script": ref_script,
        "ref_script_type": ref_script_type,
        "out_ref": out_ref,
    }


def _captured_tx(label: str) -> dict[str, Any]:
    return {
        "label": label,
        "tx_hash": "bb" * 32,
        "block_time": 1_700_000_000,
        "fee": 400_000,
        "invalid_before": None,
        "invalid_hereafter": None,
        "inputs": [_utxo_dict(["cc" * 32, 0])],
        "outputs": [],
        "ref_inputs": [
            _utxo_dict([REF_TX, 3], ref_script_type="plutusV2"),
            _utxo_dict(["dd" * 32, 1]),
        ],
        "mints": [],
        "redeemers": [],
        "required_signers": [],
        "withdrawals": [],
    }


@pytest.fixture()
def captured(monkeypatch: pytest.MonkeyPatch) -> dict[str, dict[str, Any]]:
    txs = {
        "supply_ada": _captured_tx("supply_ada"),
        "supply_djed": _captured_tx("supply_djed"),
        "redeem_ada": _captured_tx("redeem_ada"),
    }
    scripts = {
        f"{REF_TX}#3": {"ref_script": SCRIPT_HEX, "ref_script_type": "plutusV2"},
    }
    monkeypatch.setattr(replay, "_load_txs", lambda: txs)
    monkeypatch.setattr(replay, "_load_scripts", lambda: scripts)
    return txs


def test_fixture_reattaches_the_reference_script_by_out_ref(captured):
    fix = fixture("supply_ada")
    ref = fix["ref_inputs"][0]
    assert ref["ref_script"] == SCRIPT_HEX
    assert ref["ref_script_type"] == "plutusV2"
    assert fix["ref_inputs"][1]["ref_script"] is None


def test_fixture_returns_a_copy(captured):
    fixture("supply_ada")["inputs"].clear()
    assert captured["supply_ada"]["ref_inputs"][0]["ref_script"] is None
    assert len(fixture("supply_ada")["inputs"]) == 1


def test_an_unknown_label_names_itself(captured):
    with pytest.raises(KeyError, match="no_such_label"):
        fixture("no_such_label")


def test_labels_filter_by_prefix_and_sort(captured):
    assert labels() == ["redeem_ada", "supply_ada", "supply_djed"]
    assert labels("supply_") == ["supply_ada", "supply_djed"]
    assert labels("close_") == []


def test_labels_are_empty_before_any_capture(monkeypatch, tmp_path):
    monkeypatch.setattr(replay, "FIXTURES", tmp_path)
    replay._load_txs.cache_clear()
    try:
        assert labels() == []
    finally:
        replay._load_txs.cache_clear()


def test_fixture_utxos_are_the_inputs_then_the_references(captured):
    utxos = fixture_utxos(fixture("supply_ada"))
    assert [u.out_ref for u in utxos] == [("cc" * 32, 0), (REF_TX, 3), ("dd" * 32, 1)]
    assert utxos[1].ref_script == SCRIPT_HEX
    assert utxos[1].ref_script_type == "plutusV2"


def test_detach_then_attach_restores_the_fixture():
    fix = _captured_tx("x")
    fix["ref_inputs"][0]["ref_script"] = SCRIPT_HEX
    original = copy.deepcopy(fix)
    scripts = detach_scripts(fix)
    assert scripts == {
        f"{REF_TX}#3": {"ref_script": SCRIPT_HEX, "ref_script_type": "plutusV2"},
    }
    assert fix["ref_inputs"][0]["ref_script"] is None
    assert fix["ref_inputs"][0]["ref_script_type"] == "plutusV2"
    attach_scripts(fix, scripts)
    assert fix == original


def test_the_capture_script_parses_and_lists_every_job():
    tree = ast.parse(CAPTURE.read_text())
    jobs = next(
        node.value
        for node in tree.body
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        and getattr(
            node.targets[0] if isinstance(node, ast.Assign) else node.target,
            "id",
            None,
        )
        == "JOBS"
    )
    table = ast.literal_eval(jobs)
    assert len(table) == 45
    assert all(re.fullmatch("[0-9a-f]{64}", h) for h in table.values())
    assert len(set(table.values())) == len(table)


# --- replay views ---------------------------------------------------------------

SCRIPT = PlutusV2Script(bytes.fromhex(SCRIPT_HEX))
SCRIPT_HASH = plutus_script_hash(SCRIPT)
CREDENTIAL = SCRIPT_HASH.payload.hex()
PROTOCOL = Address(payment_part=SCRIPT_HASH, network=Network.MAINNET)
REWARD = Address(staking_part=SCRIPT_HASH, network=Network.MAINNET)
SIGNER = "03" * 28
SCRIPT_REF = ("01" * 32, 0)
SPENT = ("02" * 32, 3)
PLAIN_REF = ("04" * 32, 1)
CONSTR0 = "d87980"
# A definite and an indefinite datum: a decode through pycardano would re-encode both.
DEFINITE_DATUM = "d8798101"
LIST_DATUM = "9f0102ff"
DATUM_HASH = "05" * 32
QTOKEN = CREDENTIAL + b"q".hex()


def _input(out_ref: tuple[str, int], output: TransactionOutput) -> UTxO:
    return UTxO(
        TransactionInput(TransactionId(bytes.fromhex(out_ref[0])), out_ref[1]), output
    )


def _contribute(tx_builder: TransactionBuilder, *, snapshot: object) -> None:
    script_ref = _input(
        SCRIPT_REF, TransactionOutput(PROTOCOL, 2_000_000, script=SCRIPT)
    )
    spent = _input(
        SPENT,
        TransactionOutput(PROTOCOL, 5_000_000, datum=RawCBOR(bytes.fromhex(CONSTR0))),
    )
    unit = Redeemer(RawPlutusData(CBORTag(121, [])))
    tx_builder.add_script_input(spent, script=script_ref, redeemer=unit)
    tx_builder.mint = MultiAsset({SCRIPT_HASH: Asset({AssetName(b"q"): -5})})
    tx_builder.add_minting_script(
        script_ref, redeemer=Redeemer(RawPlutusData(CBORTag(121, [])))
    )
    tx_builder.withdrawals = Withdrawals({bytes(REWARD): 0})
    tx_builder.add_withdrawal_script(script_ref, redeemer=Redeemer(RawPlutusData(1)))
    tx_builder.required_signers = [VerificationKeyHash(bytes.fromhex(SIGNER))]
    tx_builder.reference_inputs.add(
        _input(PLAIN_REF, TransactionOutput(Address.decode(WALLET), 1_000_000)),
    )
    tx_builder.validity_start = 10
    tx_builder.ttl = 2_000
    tx_builder.add_output(
        TransactionOutput(
            PROTOCOL,
            Value(3_000_000, MultiAsset({SCRIPT_HASH: Asset({AssetName(b"q"): 7})})),
            datum=RawCBOR(bytes.fromhex(DEFINITE_DATUM)),
        ),
    )
    tx_builder.add_output(
        TransactionOutput(
            PROTOCOL, 4_000_000, datum=RawCBOR(bytes.fromhex(LIST_DATUM))
        ),
    )
    tx_builder.add_output(
        TransactionOutput(
            PROTOCOL, 1_500_000, datum_hash=DatumHash(bytes.fromhex(DATUM_HASH))
        ),
    )
    tx_builder.add_output(TransactionOutput(Address.decode(WALLET), 9_000_000))


@pytest.fixture()
def built() -> Built:
    return build(_contribute, object(), slot=1_000)


def _out(
    lovelace: int, assets: list[list[Any]], datum: str | None, **extra: Any
) -> dict[str, Any]:
    return {
        "address": str(PROTOCOL),
        "lovelace": lovelace,
        "assets": assets,
        "datum": datum,
        "ref_script": None,
        "ref_script_type": None,
        "out_ref": ["ee" * 32, 0],
        **extra,
    }


@pytest.fixture()
def fix() -> dict[str, Any]:
    """The capture the hand-built transaction replays, as the capture script writes it."""
    return {
        "label": "hand_written",
        "tx_hash": "ee" * 32,
        "block_time": 1_700_000_000,
        "fee": 400_000,
        "invalid_before": 10,
        "invalid_hereafter": 2_000,
        "inputs": [],
        "outputs": [
            _out(3_000_000, [[CREDENTIAL, b"q".hex(), "7"]], DEFINITE_DATUM),
            _out(4_000_000, [], LIST_DATUM),
            _out(1_500_000, [], None, datum_hash=DATUM_HASH),
            {
                "address": WALLET,
                "lovelace": 8_000_000,
                "assets": [],
                "datum": None,
                "ref_script": None,
                "ref_script_type": None,
                "out_ref": ["ee" * 32, 3],
            },
        ],
        "ref_inputs": [
            {"out_ref": list(SCRIPT_REF)},
            {"out_ref": list(PLAIN_REF)},
        ],
        "mints": [[CREDENTIAL, b"q".hex(), -5]],
        "redeemers": [
            {
                "purpose": "spend",
                "script_hash": CREDENTIAL,
                "index": 0,
                "cbor": CONSTR0,
            },
            {"purpose": "mint", "script_hash": CREDENTIAL, "index": 0, "cbor": CONSTR0},
            {"purpose": "reward", "script_hash": CREDENTIAL, "index": 0, "cbor": "01"},
        ],
        "required_signers": [SIGNER],
        "withdrawals": [[CREDENTIAL, 0]],
    }


def test_redeemers(built, fix):
    expected = {("spend", 0, CONSTR0), ("mint", 0, CONSTR0), ("reward", 0, "01")}
    assert redeemers(built) == expected
    assert captured_redeemers(fix) == expected


def test_redeemer_bytes_are_kept_in_the_array_form():
    # [body {}, witnesses {5: [[0, 2, Constr0 [1] (indefinite), [1, 1]]]}, true, null]
    tx = "84" "a0" "a1" "05" "81" "84" "00" "02" "d8799f01ff" "820101" "f5" "f6"
    assert redeemers(Built(tx)) == {("spend", 2, "d8799f01ff")}


def test_protocol_outputs_keep_datum_bytes_and_skip_other_credentials(built, fix):
    expected = [
        OutputView(str(PROTOCOL), 1_500_000, frozenset(), None, DATUM_HASH),
        OutputView(
            str(PROTOCOL), 3_000_000, frozenset({(QTOKEN, 7)}), DEFINITE_DATUM, None
        ),
        OutputView(str(PROTOCOL), 4_000_000, frozenset(), LIST_DATUM, None),
    ]
    assert protocol_outputs(built, {CREDENTIAL}) == expected
    assert captured_protocol_outputs(fix, {CREDENTIAL}) == expected
    assert protocol_outputs(built, {"00" * 28}) == []
    assert captured_protocol_outputs(fix, {"00" * 28}) == []


def test_mints(built, fix):
    assert mints(built) == captured_mints(fix) == {(QTOKEN, -5)}


def test_withdrawals(built, fix):
    assert withdrawals(built) == captured_withdrawals(fix) == {(CREDENTIAL, 0)}


def test_required_signers(built, fix):
    assert required_signers(built) == captured_required_signers(fix) == [SIGNER]


def test_reference_inputs(built, fix):
    assert (
        reference_inputs(built)
        == captured_reference_inputs(fix)
        == {
            SCRIPT_REF,
            PLAIN_REF,
        }
    )


def test_validity(built, fix):
    assert validity(built) == captured_validity(fix) == (10, 2_000)


def test_empty_views_of_a_bare_transaction():
    tx = "84" "a0" "a0" "f5" "f6"
    bare = Built(tx)
    assert redeemers(bare) == set()
    assert mints(bare) == set()
    assert withdrawals(bare) == set()
    assert required_signers(bare) == []
    assert reference_inputs(bare) == set()
    assert validity(bare) == (None, None)
    assert protocol_outputs(bare, {CREDENTIAL}) == []


def test_a_matching_replay_passes(built, fix):
    assert_replays(built, fix, credentials={CREDENTIAL})


@pytest.mark.parametrize(
    ("view", "change"),
    [
        ("redeemers", lambda f: f["redeemers"][2].update(cbor="00")),
        ("protocol outputs", lambda f: f["outputs"][0].update(datum="d8799f01ff")),
        ("protocol outputs", lambda f: f["outputs"][2].update(datum_hash="06" * 32)),
        ("mints", lambda f: f["mints"][0].__setitem__(2, -6)),
        ("withdrawals", lambda f: f["withdrawals"][0].__setitem__(1, 1)),
        ("required signers", lambda f: f["required_signers"].clear()),
        ("reference inputs", lambda f: f["ref_inputs"].pop()),
        ("validity", lambda f: f.update(invalid_hereafter=2_001)),
    ],
)
def test_a_differing_replay_names_the_view(built, fix, view, change):
    change(fix)
    with pytest.raises(AssertionError, match=view):
        assert_replays(built, fix, credentials={CREDENTIAL})


def test_wallet_outputs_are_not_compared(built, fix):
    fix["outputs"][3]["lovelace"] = 1
    assert_replays(built, fix, credentials={CREDENTIAL})


def test_ogmios_entries_keep_one_entry_per_out_ref(captured):
    utxos = fixture_utxos(fixture("supply_ada"))
    entries = ogmios_entries(utxos + utxos[:1])
    assert [(e["transaction"]["id"], e["index"]) for e in entries] == [
        u.out_ref for u in utxos
    ]
    assert entries[1]["script"] == {"language": "plutus:v2", "cbor": SCRIPT_HEX}
