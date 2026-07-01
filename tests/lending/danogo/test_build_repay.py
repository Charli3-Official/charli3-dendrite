"""Offline structural test for the forward repay (DecreaseLoanAmount) builder.

`build_repay` contributes a repay transaction to a caller-supplied
`pycardano.TransactionBuilder`: the pool + loan script spends, the novel
``Withdraw(loan_skh)`` orchestration hub, the oracle withdraw-zero price calc, the
advanced pool output, the always-present fee output, the reduced loan output (partial
repay only), and the loan + owner-NFT burn (full repay only). The caller owns the
chain context, balancing, and evaluation, so this test asserts that the builder ends
up WIRED and that the synthesized datums / redeemer indices are internally consistent
(the live ex-units are proven on Ogmios in the e2e task). It runs fully offline against
a network-free chain context and a `RepaySnapshot` reconstructed from the captured
decrease-loan fixtures (full + partial).
"""

from __future__ import annotations

import pytest
from pycardano import Address
from pycardano import Network
from pycardano import ScriptHash
from pycardano import TransactionBuilder
from pycardano import TransactionOutput

from charli3_dendrite.lending.danogo.transactions._common import (
    _forward_prices_and_leaves,
)
from charli3_dendrite.lending.danogo.transactions._common import _pool_output_value
from charli3_dendrite.lending.danogo.transactions.build import add_repay_funding
from charli3_dendrite.lending.danogo.transactions.build import build_repay
from charli3_dendrite.lending.danogo.transactions.context import RepaySnapshot
from charli3_dendrite.lending.danogo.transactions.context import _loan_collateral_units
from charli3_dendrite.lending.danogo.transactions.redeemers import DecreaseLoanAmount
from charli3_dendrite.lending.danogo.transactions.redeemers import NoneVal
from charli3_dendrite.lending.danogo.transactions.redeemers import SomeInt
from charli3_dendrite.lending.danogo.transactions.repay import _loan_output_value
from charli3_dendrite.lending.danogo.transactions.repay import safe_collateral_for_repay
from charli3_dendrite.lending.transactions.infra import OUTPUT_MIN_ADA

FULL = "decrease_loan_tx.json"
PARTIAL = "decrease_loan_partial_tx.json"
REMOVE_COLLAT = "decrease_loan_remove_collat_tx.json"
ADD_COLLAT = "decrease_loan_add_collat_tx.json"
# A real lovelace-supply (ADA) pool, captured for deposit/withdraw -- the only
# Danogo market shape where the supply token IS the coin, so the pool output's
# lovelace carries the repaid supply delta.
LOVELACE_POOL = "topup_zero_held_alt_tx.json"


def _build(
    offline_ctx, snapshot: RepaySnapshot, fix: dict
) -> TransactionBuilder:  # noqa: ANN001
    """Forward-build a repay from the fixture's realized repayment + transaction time.

    Using the captured ``pool_change_amount`` and ``txn_time`` lets the synthesized
    pool/loan datums reproduce the captured ones byte-exact (interest accrues to the
    same instant), so the structural assertions can pin both the wiring and the datum
    bytes.
    """
    realized = fix["realized"]
    tx_builder = TransactionBuilder(offline_ctx)
    build_repay(
        tx_builder,
        snapshot=snapshot,
        amount=realized["pool_change_amount"],
        txn_time=realized["txn_time"],
    )
    return tx_builder


def _decrease_redeemer(tx_builder: TransactionBuilder) -> DecreaseLoanAmount:
    """The shared `DecreaseLoanAmount` redeemer data, read from a pool/loan spend."""
    for redeemer in tx_builder._inputs_to_redeemers.values():
        if isinstance(redeemer.data, DecreaseLoanAmount):
            return redeemer.data
    raise AssertionError("no DecreaseLoanAmount spend redeemer was wired")


def _decrease_instances(tx_builder: TransactionBuilder) -> list[DecreaseLoanAmount]:
    """Every `DecreaseLoanAmount` redeemer data across spend / mint / withdrawal."""
    out: list[DecreaseLoanAmount] = [
        r.data
        for r in tx_builder._inputs_to_redeemers.values()
        if isinstance(r.data, DecreaseLoanAmount)
    ]
    out += [
        r.data
        for _s, r in tx_builder._minting_script_to_redeemers
        if r is not None and isinstance(r.data, DecreaseLoanAmount)
    ]
    out += [
        r.data
        for _s, r in tx_builder._withdrawal_script_to_redeemers
        if r is not None and isinstance(r.data, DecreaseLoanAmount)
    ]
    return out


def _flat_value(output) -> dict[str, int]:  # noqa: ANN001
    """A TransactionOutput's native assets as a flat ``unit -> qty`` map."""
    return {
        bytes(policy).hex() + bytes(name).hex(): qty
        for policy, names in output.amount.multi_asset.data.items()
        for name, qty in names.items()
    }


def _mint_triples(mint) -> dict[str, int]:  # noqa: ANN001
    return {
        bytes(policy).hex() + bytes(name).hex(): qty
        for policy, names in mint.data.items()
        for name, qty in names.items()
    }


def _reward(skh: str) -> bytes:
    return bytes(
        Address(staking_part=ScriptHash(bytes.fromhex(skh)), network=Network.MAINNET),
    )


def _fee_output_address(fix: dict) -> str:
    """The captured fee output's address: the lone datum-less supply-token output."""
    supply = fix["supply_token"]
    fee_amount = fix["realized"]["fee_output_amount"]
    return next(
        u["address"]
        for u in fix["outputs"].values()
        if not u.get("datum")
        and [(p + n, int(q)) for p, n, q in u["assets"]] == [(supply, fee_amount)]
    )


# --- spends: pool + loan, identified by their out-refs ---------------------------


@pytest.mark.parametrize("name", [FULL, PARTIAL])
def test_repay_spends_pool_and_loan(offline_ctx, repay_snap, name):  # noqa: ANN001
    fix, snap = repay_snap(name)
    tx_builder = _build(offline_ctx, snap, fix)

    spent = {
        (u.input.transaction_id.payload.hex(), u.input.index) for u in tx_builder.inputs
    }
    assert snap.pool.out_ref in spent
    assert snap.loan.out_ref in spent
    # Exactly the two script inputs are contributed by the builder (the borrower
    # funding input is added separately by `add_repay_funding`).
    assert len(tx_builder.inputs) == 2


# --- the Withdraw(loan_skh) hub + oracle Withdraw, both zero withdrawals ----------


@pytest.mark.parametrize("name", [FULL, PARTIAL])
def test_loan_hub_and_oracle_withdrawals_present(
    offline_ctx,  # noqa: ANN001
    repay_snap,  # noqa: ANN001
    name,  # noqa: ANN001
):
    fix, snap = repay_snap(name)
    tx_builder = _build(offline_ctx, snap, fix)

    withdrawals = tx_builder.withdrawals.to_primitive()
    loan_hub = _reward(snap.loan_skh)
    oracle = _reward(snap.oracle_skh)
    # The novel loan-script hub (f1 + loan_skh) and the oracle reward address are both
    # present as ZERO withdrawals (the withdraw-zero orchestration / pricing trick).
    assert withdrawals.get(loan_hub) == 0
    assert withdrawals.get(oracle) == 0
    # The reward accounts are network-tagged stake-script addresses (header 0xf1).
    assert loan_hub[0] == 0xF1
    assert oracle[0] == 0xF1


@pytest.mark.parametrize("name", [FULL, PARTIAL])
def test_one_decrease_redeemer_shared_across_roles(
    offline_ctx,  # noqa: ANN001
    repay_snap,  # noqa: ANN001
    name,  # noqa: ANN001
):
    fix, snap = repay_snap(name)
    tx_builder = _build(offline_ctx, snap, fix)

    instances = _decrease_instances(tx_builder)
    # Pool spend + loan spend + the hub always carry it; a full repay adds the mint
    # burn. Every reference is the SAME object, so its CBOR is byte-identical across
    # roles -- matching the captured tx where one redeemer drives all four purposes.
    full_repay = fix["variant"] == "full"
    assert len(instances) == (4 if full_repay else 3)
    first = instances[0]
    assert all(inst is first for inst in instances)
    assert all(inst.to_cbor() == first.to_cbor() for inst in instances)


# --- outputs: advanced pool (+ datum), fee output, loan output iff partial ---------


def test_full_repay_pool_output_and_datum(offline_ctx, repay_snap):  # noqa: ANN001
    fix, snap = repay_snap(FULL)
    tx_builder = _build(offline_ctx, snap, fix)
    realized = fix["realized"]

    decrease = _decrease_redeemer(tx_builder)
    pool_out = tx_builder.outputs[decrease.pool_out_idx]
    # The advanced pool datum reproduces the captured pool_out datum byte-exact.
    assert pool_out.datum.to_cbor().hex() == fix["pool_out_datum"]
    # The pool's supply-token holdings rise by the repayment net of the swept fee.
    supply = fix["supply_token"]
    before = next(q for p, n, q in snap.pool.assets if p + n == supply)
    after = _flat_value(pool_out)[supply]
    assert after - before == realized["pool_supply_holdings_delta"]


def test_partial_repay_pool_and_loan_datums(offline_ctx, repay_snap):  # noqa: ANN001
    fix, snap = repay_snap(PARTIAL)
    tx_builder = _build(offline_ctx, snap, fix)

    decrease = _decrease_redeemer(tx_builder)
    pool_out = tx_builder.outputs[decrease.pool_out_idx]
    assert pool_out.datum.to_cbor().hex() == fix["pool_out_datum"]

    assert isinstance(decrease.loan_out_idx, SomeInt)
    loan_out = tx_builder.outputs[decrease.loan_out_idx.value]
    # The reduced loan datum reproduces the captured loan_out datum byte-exact, and the
    # loan's value (loan token + locked collateral) is carried through unchanged.
    assert loan_out.datum.to_cbor().hex() == fix["loan_out_datum"]
    carried = _flat_value(loan_out)
    for policy, name, qty in snap.loan.assets:
        assert carried.get(policy + name) == qty


@pytest.mark.parametrize("name", [FULL, PARTIAL])
def test_fee_output_amount_and_address(
    offline_ctx,  # noqa: ANN001
    repay_snap,  # noqa: ANN001
    name,  # noqa: ANN001
):
    fix, snap = repay_snap(name)
    tx_builder = _build(offline_ctx, snap, fix)
    realized = fix["realized"]

    decrease = _decrease_redeemer(tx_builder)
    # A fee output is ALWAYS present on repay (fee_out_idx = Some).
    assert isinstance(decrease.fee_out_idx, SomeInt)
    fee_out = tx_builder.outputs[decrease.fee_out_idx.value]
    assert (
        _flat_value(fee_out).get(fix["supply_token"]) == realized["fee_output_amount"]
    )
    # It pays the market's fee-recipient address (the captured fee output's address).
    assert str(fee_out.address) == _fee_output_address(fix)


# --- mint burn iff full; no mint on partial --------------------------------------


def test_full_repay_burns_loan_token_and_owner_nft(
    offline_ctx,  # noqa: ANN001
    repay_snap,  # noqa: ANN001
):
    fix, snap = repay_snap(FULL)
    tx_builder = _build(offline_ctx, snap, fix)

    assert tx_builder.mint is not None
    minted = _mint_triples(tx_builder.mint)
    loan_token = snap.loan_skh + snap.market_name
    # Exactly the two -1 burns the captured full repay records, nothing else.
    assert minted == {loan_token: -1, snap.owner_nft: -1}


def test_partial_repay_has_no_mint(offline_ctx, repay_snap):  # noqa: ANN001
    fix, snap = repay_snap(PARTIAL)
    tx_builder = _build(offline_ctx, snap, fix)
    assert tx_builder.mint is None


def test_full_repay_omits_loan_output(offline_ctx, repay_snap):  # noqa: ANN001
    fix, snap = repay_snap(FULL)
    tx_builder = _build(offline_ctx, snap, fix)
    decrease = _decrease_redeemer(tx_builder)
    # A full repay closes the loan: no loan output (loan_out_idx = None).
    assert isinstance(decrease.loan_out_idx, NoneVal)
    # Only the advanced pool + the fee output are contributed.
    assert len(tx_builder.outputs) == 2


# --- finalized redeemer indices derive from the assembled ordering ----------------


def test_full_repay_redeemer_indices(offline_ctx, repay_snap):  # noqa: ANN001
    fix, snap = repay_snap(FULL)
    tx_builder = _build(offline_ctx, snap, fix)
    decrease = _decrease_redeemer(tx_builder)
    decoded = fix["decrease_loan_redeemer_decoded"]

    # The pool output leads the builder's output set, matching the captured full repay,
    # and a full repay carries no loan output (loan_out_idx = None, as captured).
    assert decrease.pool_out_idx == decoded["pool_out_idx"]
    assert isinstance(decrease.loan_out_idx, NoneVal)
    assert decoded["loan_out_idx"] is None
    # The fee index resolves to the assembled fee output.
    assert isinstance(decrease.fee_out_idx, SomeInt)
    fee_out = tx_builder.outputs[decrease.fee_out_idx.value]
    assert _flat_value(fee_out).get(fix["supply_token"]) == (
        fix["realized"]["fee_output_amount"]
    )
    assert str(fee_out.address) == _fee_output_address(fix)
    # The pool-input out-ref the redeemer carries is the spent pool input.
    assert (
        decrease.pool_in_out_ref.transaction_id.hex(),
        decrease.pool_in_out_ref.output_index,
    ) == snap.pool.out_ref
    _assert_ref_indices_resolve(tx_builder, decrease, snap)


def test_partial_repay_redeemer_indices(offline_ctx, repay_snap):  # noqa: ANN001
    fix, snap = repay_snap(PARTIAL)
    tx_builder = _build(offline_ctx, snap, fix)
    decrease = _decrease_redeemer(tx_builder)
    decoded = fix["decrease_loan_redeemer_decoded"]
    realized = fix["realized"]

    # Pool + loan output indices match the captured partial repay output ordering.
    assert decrease.pool_out_idx == decoded["pool_out_idx"]
    assert isinstance(decrease.loan_out_idx, SomeInt)
    assert decrease.loan_out_idx.value == decoded["loan_out_idx"]
    # The fee output is always present; its absolute index drifts from the captured tx
    # because the live partial repay carried an extra borrower output ahead of the fee
    # (added by balancing, not by the structural build), so what must hold is that the
    # index resolves -- in this build's ordering -- to the fee output.
    assert isinstance(decrease.fee_out_idx, SomeInt)
    fee_out = tx_builder.outputs[decrease.fee_out_idx.value]
    assert (
        _flat_value(fee_out).get(fix["supply_token"]) == realized["fee_output_amount"]
    )
    assert str(fee_out.address) == _fee_output_address(fix)
    assert (
        decrease.pool_in_out_ref.transaction_id.hex(),
        decrease.pool_in_out_ref.output_index,
    ) == snap.pool.out_ref
    _assert_ref_indices_resolve(tx_builder, decrease, snap)


def _assert_ref_indices_resolve(
    tx_builder: TransactionBuilder,
    decrease: DecreaseLoanAmount,
    snap: RepaySnapshot,
) -> None:
    """The reference indices point at the protocol-config + market reference inputs.

    The builder prunes the oracle reference set to the on-chain shape, so the absolute
    reference index values legitimately drift from the captured tx; what must hold is
    that each index resolves -- in the canonical out-ref ordering the Plutus script
    context exposes -- to the right UTxO.
    """
    refs = sorted(
        (u.input for u in tx_builder.reference_inputs),
        key=lambda i: (bytes(i.transaction_id), i.index),
    )
    cfg = refs[decrease.protocol_cfg_ref_idx]
    mkt = refs[decrease.market_ref_idx]
    assert (cfg.transaction_id.payload.hex(), cfg.index) == snap.protocol_config.out_ref
    assert (mkt.transaction_id.payload.hex(), mkt.index) == snap.market.out_ref


# --- validity window + funding ---------------------------------------------------


@pytest.mark.parametrize("name", [FULL, PARTIAL])
def test_validity_window_within_repay_bound(
    offline_ctx,  # noqa: ANN001
    repay_snap,  # noqa: ANN001
    name,  # noqa: ANN001
):
    fix, snap = repay_snap(name)
    tx_builder = _build(offline_ctx, snap, fix)
    assert tx_builder.validity_start is not None
    assert tx_builder.ttl is not None
    assert (tx_builder.ttl - tx_builder.validity_start) * 1000 <= 360_000


@pytest.mark.parametrize("name", [FULL, PARTIAL])
def test_repay_funding_adds_borrower_input(
    offline_ctx,  # noqa: ANN001
    actor_addr,  # noqa: ANN001
    repay_snap,  # noqa: ANN001
    name,  # noqa: ANN001
):
    fix, snap = repay_snap(name)
    tx_builder = _build(offline_ctx, snap, fix)
    realized = fix["realized"]

    entry = add_repay_funding(
        tx_builder,
        snapshot=snap,
        actor=actor_addr,
        actor_utxo="ab" * 32 + "#0",
        amount=realized["pool_change_amount"],
    )
    # The borrower input was added (pool + loan spends, then the funding input).
    assert len(tx_builder.inputs) == 3
    # The Ogmios additionalUtxo entry resolves the borrower funding out-ref and carries
    # the owner NFT (proof of loan ownership) plus the repaid supply token.
    assert entry["transaction"]["id"] == "ab" * 32
    owner_policy, owner_name = snap.owner_nft[:56], snap.owner_nft[56:]
    assert entry["value"][owner_policy][owner_name] == 1
    supply = snap.market_info.supply_token
    assert entry["value"][supply[:56]][supply[56:]] == realized["pool_change_amount"]


# --- partial repay that ALSO modifies collateral (add / remove) -------------------
#
# These fixtures are mainnet partial repays that ride the same DecreaseLoanAmount
# redeemer and change the collateral locked in the loan output (remove -9,000,000,000
# of a Danogo dToken; add +33,663,413,118 of the same dToken). Both keep the loan
# input's lovelace and modify the SAME collateral unit already on the loan, so the
# priced collateral set is the loan input's unchanged.


def _target_collateral(fix: dict) -> dict[str, int]:
    """The TARGET absolute collateral the loan output should lock (from the fixture)."""
    return {
        k: int(v) for k, v in fix["realized"]["collateral"]["collateral_out"].items()
    }


def _captured_loan_output(fix: dict, snap: RepaySnapshot) -> dict[str, int]:
    """The captured loan output's native assets (loan token + locked collateral)."""
    loan_token = snap.loan_skh + snap.market_name
    for u in fix["outputs"].values():
        assets = {p + n: int(q) for p, n, q in u["assets"]}
        if assets.get(loan_token) == 1:
            return assets
    raise AssertionError("captured loan output not found")


def _native_assets(value) -> dict[str, int]:  # noqa: ANN001
    """A pycardano `Value`'s native assets as a flat ``unit -> qty`` map."""
    return {
        bytes(policy).hex() + bytes(name).hex(): qty
        for policy, names in value.multi_asset.data.items()
        for name, qty in names.items()
    }


@pytest.mark.parametrize("name", [REMOVE_COLLAT, ADD_COLLAT])
def test_collateral_mod_loan_output_value_byte_exact(repay_snap, name):  # noqa: ANN001
    fix, snap = repay_snap(name)
    target = _target_collateral(fix)

    value = _loan_output_value(
        snap.loan,
        loan_skh=snap.loan_skh,
        market_name=snap.market_name,
        collateral=target,
    )
    # The loan output's native assets (loan token + modified collateral) reproduce the
    # captured loan_out byte-exact, and the loan input's lovelace is carried through.
    assert _native_assets(value) == _captured_loan_output(fix, snap)
    assert value.coin == snap.loan.lovelace


@pytest.mark.parametrize("name", [REMOVE_COLLAT, ADD_COLLAT])
def test_no_collateral_target_keeps_input_collateral(repay_snap, name):  # noqa: ANN001
    fix, snap = repay_snap(name)

    # Falsy collateral (the backward-compatible default) carries the loan INPUT's
    # holdings forward unchanged -- the reduced output is NOT the captured (modified)
    # one, confirming a plain repay is unaffected.
    default = _loan_output_value(
        snap.loan,
        loan_skh=snap.loan_skh,
        market_name=snap.market_name,
        collateral=None,
    )
    expected = {policy + name_: qty for policy, name_, qty in snap.loan.assets}
    assert _native_assets(default) == expected

    empty = _loan_output_value(
        snap.loan,
        loan_skh=snap.loan_skh,
        market_name=snap.market_name,
        collateral={},
    )
    assert _native_assets(empty) == expected


def test_collateral_target_rejected_on_full_repay(
    offline_ctx, repay_snap
):  # noqa: ANN001
    fix, snap = repay_snap(REMOVE_COLLAT)
    tx_builder = TransactionBuilder(offline_ctx)
    # A request at or above the loan's accrued debt is a full repay, which releases all
    # collateral -- so a collateral target there is rejected.
    debt = fix["realized"]["current_loan_amount"]
    with pytest.raises(ValueError, match="partial repay"):
        build_repay(
            tx_builder,
            snapshot=snap,
            amount=debt * 2,
            collateral=_target_collateral(fix),
            txn_time=fix["realized"]["txn_time"],
        )


def test_added_collateral_is_funded_by_borrower(  # noqa: ANN001
    offline_ctx,
    actor_addr,
    repay_snap,
):
    fix, snap = repay_snap(ADD_COLLAT)
    tx_builder = TransactionBuilder(offline_ctx)
    target = _target_collateral(fix)

    entry = add_repay_funding(
        tx_builder,
        snapshot=snap,
        actor=actor_addr,
        actor_utxo="ab" * 32 + "#0",
        amount=fix["realized"]["pool_change_amount"],
        collateral=target,
    )
    # Each positive collateral delta (added collateral) is funded by the borrower.
    delta = {
        k: int(v) for k, v in fix["realized"]["collateral"]["collateral_delta"].items()
    }
    for unit, added in delta.items():
        assert added > 0
        assert entry["value"][unit[:56]][unit[56:]] == added
    # The owner NFT (proof of ownership) and the repaid supply token are still carried.
    assert entry["value"][snap.owner_nft[:56]][snap.owner_nft[56:]] == 1
    supply = snap.market_info.supply_token
    assert (
        entry["value"][supply[:56]][supply[56:]]
        == fix["realized"]["pool_change_amount"]
    )


def test_removed_collateral_is_not_funded(  # noqa: ANN001
    offline_ctx,
    actor_addr,
    repay_snap,
):
    fix, snap = repay_snap(REMOVE_COLLAT)
    tx_builder = TransactionBuilder(offline_ctx)
    target = _target_collateral(fix)

    entry = add_repay_funding(
        tx_builder,
        snapshot=snap,
        actor=actor_addr,
        actor_utxo="ab" * 32 + "#0",
        amount=fix["realized"]["pool_change_amount"],
        collateral=target,
    )
    # Removed collateral (a negative delta) returns to the borrower via balancing, so
    # it is NOT part of the funding value the borrower supplies.
    unit = next(iter(target))
    assert fix["realized"]["collateral"]["collateral_delta"][unit] < 0
    assert unit[56:] not in entry["value"].get(unit[:56], {})


def test_oracle_prices_target_collateral_set(repay_snap):  # noqa: ANN001
    fix, snap = repay_snap(REMOVE_COLLAT)
    target = _target_collateral(fix)
    input_units = _loan_collateral_units(
        snap.loan,
        loan_skh=snap.loan_skh,
        market_name=snap.market_name,
    )

    # The captured collateral mods change a unit already on the loan, so the target set
    # unions to the loan input's units -- the priced set (and its leaves) is byte
    # identical to a plain repay, and every target unit is priced.
    assert set(target) == set(input_units)
    prices, _ = _forward_prices_and_leaves(snap, set(target))
    quote_prices = prices.get(snap.market_info.supply_token, {})
    for unit in target:
        assert unit in quote_prices


def test_hf_guard_accepts_captured_target(repay_snap):  # noqa: ANN001
    fix, snap = repay_snap(REMOVE_COLLAT)
    target = _target_collateral(fix)
    realized = fix["realized"]
    loan_out_amount = realized["current_loan_amount"] - realized["pool_change_amount"]

    # The captured (valid) target's threshold-weighted collateral value exceeds the
    # reduced loan amount.
    headroom = safe_collateral_for_repay(
        snap,
        collateral=target,
        loan_out_amount=loan_out_amount,
    )
    assert headroom > loan_out_amount


def test_hf_guard_rejects_undercollateralized_target(repay_snap):  # noqa: ANN001
    fix, snap = repay_snap(REMOVE_COLLAT)
    unit = next(iter(_target_collateral(fix)))
    # An obviously-insufficient target (a single collateral unit) cannot cover the
    # accrued loan, so the preflight fails loud.
    with pytest.raises(ValueError, match="under-collateralized"):
        safe_collateral_for_repay(
            snap,
            collateral={unit: 1},
            loan_out_amount=fix["realized"]["current_loan_amount"],
        )


# --- lovelace-supply pool output carries the repaid supply delta ------------------
#
# Every repay fixture above is a native-token-supply market, where the supply token is
# NOT the coin: the pool output's lovelace is just the carried min-ADA balance, so
# flooring the computed coin and flooring the pool's pre-action lovelace are identical
# (and the captured byte-exact assertions above prove that path is unchanged). The ONE
# market shape that distinguishes them is a lovelace-supply (ADA) market: the supply
# token IS the coin, so a repay's returned principal + fees lands in the coin.
# `_pool_output_value` already applies that delta, so the builder must floor THAT
# computed coin -- never reset it to the pool's pre-action lovelace, which would drop
# the repaid ADA. A captured ADA-market repay does not exist, so this exercises the
# lovelace branch offline against a real lovelace-supply pool.


def test_lovelace_supply_pool_output_carries_repay_delta(topup_snap):  # noqa: ANN001
    _fix, snap = topup_snap(LOVELACE_POOL)
    pool = snap.pool
    # A repay returns principal + swept-fee-net supply to the pool: for an ADA market
    # that is a POSITIVE lovelace supply delta added to the pool's coin.
    supply_delta = 100_000_000
    pool_out = TransactionOutput(
        address=Address.decode(pool.address),
        amount=_pool_output_value(pool, "lovelace", supply_delta),
    )
    # The fix (mirrors `_add_repay_outputs`): floor the ALREADY-COMPUTED coin, which
    # carries the supply delta.
    pool_out.amount.coin = max(pool_out.amount.coin, OUTPUT_MIN_ADA)

    # The repaid ADA lands in the pool output's coin -- it is NOT dropped.
    assert pool_out.amount.coin == pool.lovelace + supply_delta
    # The pre-fix reset to the pool's pre-action lovelace would have dropped the delta,
    # so the two diverge: this is the regression the fix guards against.
    assert pool_out.amount.coin != max(pool.lovelace, OUTPUT_MIN_ADA)
