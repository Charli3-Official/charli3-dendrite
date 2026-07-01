"""Synthesize the TopupWithdraw pool datum + dToken mint/burn, pinned byte-exact.

Fully offline: the before/after pool datums (CBOR) and the realized intermediates
are embedded in the deposit/withdraw fixtures, so no backend/network is required.
"""

import json
from pathlib import Path

from charli3_dendrite.lending.danogo.datums import PoolDatum
from charli3_dendrite.lending.danogo.transactions.datum_synth import mint_burn_dtoken
from charli3_dendrite.lending.danogo.transactions.datum_synth import (
    synth_pool_datum_topup_withdraw,
)
from charli3_dendrite.lending.math import floor_div

FIX_DIR = Path(__file__).parent / "fixtures"


def _pool_before_after(fixture: str) -> tuple[str, str, dict]:
    fix = json.loads((FIX_DIR / fixture).read_text())
    return fix["pool_in_datum"], fix["pool_out_datum"], fix["realized"]


def _check_synth(fixture: str) -> None:
    before, after, realized = _pool_before_after(fixture)
    prev = PoolDatum.from_cbor(bytes.fromhex(before))
    got, minted_dtoken = synth_pool_datum_topup_withdraw(
        prev,
        pool_changed_amount=realized["pool_changed_amount"],
        withdraw_fee=realized["withdraw_fee"],
        txn_time=realized["txn_time"],
        power_base=realized["power_base"],
        base_rate=realized["base_rate"],
        loan_fee_rate=realized["loan_fee_rate"],
        new_alt_supply_tokens_rate=None,
    )
    # The advanced datum is byte-exact against the captured output, and the dToken
    # mint/burn the synth returns equals the real on-chain delta -- both from one call.
    assert got.to_cbor().hex() == after
    assert minted_dtoken == realized["mint_burn_dtoken"]


def test_synth_pool_datum_topup_matches_captured():
    _check_synth("topup_tx.json")


def test_synth_pool_datum_withdraw_matches_captured():
    _check_synth("withdraw_tx.json")


def _pool_alt_holding(fix: dict, alt_unit: str) -> int:
    """Quantity of the alt-supply token the spent pool UTxO holds in the fixture."""
    pool_addr = fix["outputs"]["0"]["address"]
    policy, name = alt_unit[:56], alt_unit[56:]
    pool_in = next(u for u in fix["inputs"] if u["address"] == pool_addr)
    return next(int(q) for p, n, q in pool_in["assets"] if p == policy and n == name)


def test_synth_pool_datum_alt_topup_matches_captured():
    """Byte-exact reproduction of an alt-supply-token deposit's pool datum.

    The alt market re-prices its alternative supply token from the oracle on every
    deposit, so the synth is driven with the captured re-priced rate and the
    ``alt_tokens_interest`` booked from re-valuing the pool's alt holding,
    ``floor(amount * (new_rate - old_rate))``. The advanced datum must reproduce the
    captured output byte-exact -- including the non-empty ``alt_supply_tokens_rate``
    indefinite-array (``9f..ff``) encoding.
    """
    fix = json.loads((FIX_DIR / "topup_alt_tx.json").read_text())
    prev = PoolDatum.from_cbor(bytes.fromhex(fix["pool_in_datum"]))
    out = PoolDatum.from_cbor(bytes.fromhex(fix["pool_out_datum"]))
    realized = fix["realized"]

    prev_rates = list(prev.alt_supply_tokens_rate)
    new_rates = list(out.alt_supply_tokens_rate)
    assert prev_rates, "alt fixture must carry a non-empty rate list"

    # The alt-supply token unit is the pool holding that is neither the USDM supply
    # token nor the market/pool NFT; reproduce its re-valuation interest term.
    alt_unit = (
        "73f29518da0013a671458d52624a4828c5b5bedaff8a950b0063b1cf"
        "57acdd5c0dfbdec9d2d611f57f42951b78ef8fe7dc1935b9ce19d839"
    )
    amount = _pool_alt_holding(fix, alt_unit)
    alt_tokens_interest = 0
    for prev_rate, new_rate in zip(prev_rates, new_rates):
        delta_num = new_rate.num * prev_rate.denom - prev_rate.num * new_rate.denom
        alt_tokens_interest += floor_div(
            amount * delta_num,
            new_rate.denom * prev_rate.denom,
        )

    got, minted_dtoken = synth_pool_datum_topup_withdraw(
        prev,
        pool_changed_amount=realized["pool_changed_amount"],
        withdraw_fee=realized["withdraw_fee"],
        txn_time=realized["txn_time"],
        power_base=realized["power_base"],
        base_rate=realized["base_rate"],
        loan_fee_rate=realized["loan_fee_rate"],
        alt_tokens_interest=alt_tokens_interest,
        new_alt_supply_tokens_rate=new_rates,
    )
    assert got.to_cbor().hex() == fix["pool_out_datum"]
    assert minted_dtoken == realized["mint_burn_dtoken"]


def test_mint_burn_dtoken_bootstrap_and_ratio():
    assert (
        mint_burn_dtoken(
            pool_changed_amount=1000,
            withdraw_fee=0,
            total_supply_before=0,
            circulating_dtoken=0,
        )
        == 1000
    )
    assert (
        mint_burn_dtoken(
            pool_changed_amount=1000,
            withdraw_fee=0,
            total_supply_before=2000,
            circulating_dtoken=4000,
        )
        == 2000
    )


def test_mint_burn_dtoken_subtracts_withdraw_fee_before_ratio():
    """`withdraw_fee` is subtracted from the supply delta BEFORE the pro-rata math.

    All three captured fixtures have ``withdraw_fee == 0``, so this fee-subtraction
    path is otherwise unverified. Pin the direction: ``floor((1000 - 100) * 4000 /
    2000) == 1800`` (subtracting after the ratio would instead give 1900).
    """
    assert (
        mint_burn_dtoken(
            pool_changed_amount=1000,
            withdraw_fee=100,
            total_supply_before=2000,
            circulating_dtoken=4000,
        )
        == 1800
    )
