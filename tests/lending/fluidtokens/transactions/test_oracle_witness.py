"""Offline: the signed oracle-reward redeemer parser against captured txs.

BORROW and MODIFY_COLLATERAL drive a signed "oracle reward" (``Withdraw``) script.
These tests locate that redeemer in each captured fixture by elimination -- mirroring
``BorrowSnapshot.from_capture`` / ``ChangeCollateralSnapshot.from_capture`` -- and assert
:class:`OracleReward` extracts the exact signed window / token / price, that the
signature is 64 bytes, and that the ms<->slot window helpers round-trip.
"""
from __future__ import annotations

import json
from pathlib import Path

import cbor2  # type: ignore[import-not-found]
import pytest
from pycardano import RawPlutusData

from charli3_dendrite.lending.fluidtokens.constants import (
    LOAN_CHANGE_COLLATERAL_ACTION_SKH,
)
from charli3_dendrite.lending.fluidtokens.constants import LOAN_POLICY
from charli3_dendrite.lending.fluidtokens.constants import LOAN_SPEND_SKH
from charli3_dendrite.lending.fluidtokens.constants import POOL_POLICY
from charli3_dendrite.lending.fluidtokens.oracles.witness import (
    build_oracle_reward_cbor,
)
from charli3_dendrite.lending.fluidtokens.oracles.witness import OracleReward
from charli3_dendrite.utility import posix_ms_to_slot
from charli3_dendrite.utility import slot_to_posix_ms

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text())


def _change_collateral_oracle_cbor(fix: dict) -> str:
    # Elimination set from ``ChangeCollateralSnapshot.from_capture``.
    known = {LOAN_SPEND_SKH, LOAN_POLICY, LOAN_CHANGE_COLLATERAL_ACTION_SKH}
    reward = next(
        r
        for r in fix["redeemers"]
        if r["purpose"] == "reward" and r["script_hash"] not in known
    )
    return reward["cbor"]


def _borrow_oracle_cbor(fix: dict) -> str:
    # ``BorrowSnapshot.from_capture``: the reward redeemer that is not the pool policy.
    reward = next(
        r
        for r in fix["redeemers"]
        if r["purpose"] == "reward" and r["script_hash"] != POOL_POLICY
    )
    return reward["cbor"]


def test_parse_change_collateral_oracle_reward() -> None:
    fix = _fixture("change_collateral.json")
    cbor = _change_collateral_oracle_cbor(fix)

    reward = OracleReward.parse(cbor)

    assert reward.valid_from_ms == 1782719701389
    assert reward.valid_to_ms == 1782722701389
    assert reward.collateral_policy == (
        "f13ac4d66b3ee19a6aa0f2a22298737bd907cc95121662fc971b5275"
    )
    assert reward.collateral_name == "535452494b45"  # "STRIKE"
    assert reward.price_num == 452964186
    assert reward.price_den == 100000000
    assert len(reward.signature) == 64
    assert reward.cbor == cbor


def test_parse_borrow_oracle_reward() -> None:
    fix = _fixture("borrow_pool.json")
    cbor = _borrow_oracle_cbor(fix)

    reward = OracleReward.parse(cbor)

    assert reward.valid_from_ms == 1782794100599
    assert reward.valid_to_ms == 1782797100599
    assert reward.collateral_policy == (
        "279c909f348e533da5808898f87f9a14bb2c3dfbbacccd631d927a3f"
    )
    assert reward.collateral_name == "534e454b"  # "SNEK"
    assert reward.price_num == 220485
    assert reward.price_den == 100
    assert len(reward.signature) == 64
    assert reward.cbor == cbor


@pytest.mark.parametrize("slot", [56_332_800, 100_000_000, 123_456_789, 150_000_000])
def test_slot_ms_round_trip(slot: int) -> None:
    assert posix_ms_to_slot(slot_to_posix_ms(slot)) == slot


def test_tx_validity_slots_is_covered_by_signed_window() -> None:
    fix = _fixture("borrow_pool.json")
    reward = OracleReward.parse(_borrow_oracle_cbor(fix))

    from_slot, to_slot = reward.tx_validity_slots()

    # The window is the widest one fully inside the signed ms window: both boundary
    # slots start within it, and it is self-covered by construction.
    assert slot_to_posix_ms(from_slot) >= reward.valid_from_ms
    assert slot_to_posix_ms(to_slot) <= reward.valid_to_ms
    assert reward.contains_slot_window(from_slot, to_slot)
    # The next slot below the start is no longer covered (start ceils the ms bound).
    assert slot_to_posix_ms(from_slot - 1) < reward.valid_from_ms


def test_contains_slot_window_rejects_out_of_range() -> None:
    fix = _fixture("borrow_pool.json")
    reward = OracleReward.parse(_borrow_oracle_cbor(fix))
    from_slot, to_slot = reward.tx_validity_slots()

    # A window that starts one slot before / ends one slot after the signed window
    # is not covered.
    assert not reward.contains_slot_window(from_slot - 1, to_slot)
    assert not reward.contains_slot_window(from_slot, to_slot + 1)


def test_parse_rejects_non_oracle_cbor() -> None:
    # A bare integer is valid cbor but not an oracle-reward Constr.
    with pytest.raises(ValueError):
        OracleReward.parse("00")


def _decode_reward_parts(cbor_hex: str) -> dict:
    """Decode a captured oracle-reward redeemer into its message parts + signatures."""
    top = cbor2.loads(bytes.fromhex(cbor_hex))
    signed = top.value
    message, price_num, price_den = signed[0].value
    valid_from_ms, valid_to_ms, token = message.value
    policy, name = token.value
    signatures = [(sig.value[0], sig.value[1]) for sig in signed[1]]
    return {
        "valid_from_ms": valid_from_ms,
        "valid_to_ms": valid_to_ms,
        "collateral_policy": policy.hex(),
        "collateral_name": name.hex(),
        "price_num": price_num,
        "price_den": price_den,
        "signatures": signatures,
    }


@pytest.mark.parametrize(
    ("fixture", "oracle_cbor"),
    [
        ("change_collateral.json", _change_collateral_oracle_cbor),
        ("borrow_pool.json", _borrow_oracle_cbor),
    ],
)
def test_build_oracle_reward_matches_capture_on_chain(fixture, oracle_cbor) -> None:
    """A reward rebuilt from decoded parts is on-chain-equivalent to the capture.

    The raw bytes differ (captures use indefinite-length arrays), but the tx builder
    round-trips the redeemer through ``RawPlutusData``; asserting the normalized forms
    match proves a reconstructed witness serializes on-chain identically -- so the same
    signatures verify over the same ``serialise_data(message)``.
    """
    captured = oracle_cbor(_fixture(fixture))
    rebuilt = build_oracle_reward_cbor(**_decode_reward_parts(captured))

    assert (
        RawPlutusData.from_cbor(rebuilt).to_cbor()
        == RawPlutusData.from_cbor(captured).to_cbor()
    )
    # And the rebuilt witness parses back to the same signed values.
    assert (
        OracleReward.parse(rebuilt).valid_from_ms
        == OracleReward.parse(
            captured,
        ).valid_from_ms
    )


def test_build_oracle_reward_rejects_bad_signature() -> None:
    with pytest.raises(ValueError, match="64 bytes"):
        build_oracle_reward_cbor(
            valid_from_ms=1,
            valid_to_ms=2,
            collateral_policy="ab" * 28,
            collateral_name="534e454b",
            price_num=1,
            price_den=1,
            signatures=[(b"\x00" * 10, 0)],
        )

    with pytest.raises(ValueError, match="at least one signature"):
        build_oracle_reward_cbor(
            valid_from_ms=1,
            valid_to_ms=2,
            collateral_policy="ab" * 28,
            collateral_name="534e454b",
            price_num=1,
            price_den=1,
            signatures=[],
        )
