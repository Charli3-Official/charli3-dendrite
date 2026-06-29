"""Assemble a forward Danogo increase-loan (IncreaseLoanAmount) into a builder.

`build_increase_loan` contributes "borrow more against an existing loan" to a
caller-supplied `pycardano.TransactionBuilder`: the pool + loan script spends, the
novel ``Withdraw(pool_skh)`` orchestration hub the spends delegate to (NOT repay's
``Withdraw(loan_skh)``), the oracle withdraw-zero price calc, the protocol-config /
market / oracle reference inputs, the advanced pool output (idx0, supply paid out),
and the raised loan output (idx1, always present). An increase mints nothing -- the
loan token + owner NFT already exist -- and the borrower supplies no supply token in
(the pool pays it out). The caller owns balancing/evaluation.

The pool datum advances exactly like create-loan (the accrued fee accumulates, it is
NOT swept to a fee output) and the loan datum's outstanding amount rises; both are
synthesized in `datum_synth`. One ``IncreaseLoanAmount`` redeemer instance is reused
byte-for-byte across the pool spend, the loan spend, and the ``Withdraw(pool_skh)``
hub.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Any

from pycardano import Address
from pycardano import Redeemer
from pycardano import TransactionBuilder
from pycardano import TransactionOutput
from pycardano import min_lovelace

from charli3_dendrite.lending.danogo.datums import PoolDatum
from charli3_dendrite.lending.danogo.math import current_interest_index
from charli3_dendrite.lending.danogo.math import current_loan_amount
from charli3_dendrite.lending.danogo.oracles.deployments import (
    deployment_for_oracle_skh,
)
from charli3_dendrite.lending.danogo.oracles.redeemer import OraclePriceCalcRdmr
from charli3_dendrite.lending.danogo.transactions._common import _ref_index
from charli3_dendrite.lending.danogo.transactions._common import _set_validity_window
from charli3_dendrite.lending.danogo.transactions._common import _synth_oracle_redeemer
from charli3_dendrite.lending.danogo.transactions._common import _to_utxo
from charli3_dendrite.lending.danogo.transactions._common import add_actor_funding
from charli3_dendrite.lending.danogo.transactions._common import attach_oracle_withdraw
from charli3_dendrite.lending.danogo.transactions._common import (
    build_pool_script_output,
)
from charli3_dendrite.lending.danogo.transactions._common import collateral_health_value
from charli3_dendrite.lending.danogo.transactions._common import prepare_oracle_withdraw
from charli3_dendrite.lending.danogo.transactions.context import IncreaseLoanSnapshot
from charli3_dendrite.lending.danogo.transactions.context import _loan_collateral_units
from charli3_dendrite.lending.danogo.transactions.datum_synth import (
    synth_pool_datum_increase_loan,
)
from charli3_dendrite.lending.danogo.transactions.datum_synth import (
    update_loan_datum_increase,
)
from charli3_dendrite.lending.danogo.transactions.redeemers import IncreaseLoanAmount
from charli3_dendrite.lending.danogo.transactions.redeemers import NoneVal
from charli3_dendrite.lending.danogo.transactions.redeemers import OutputReference
from charli3_dendrite.lending.danogo.transactions.repay import _loan_collateral_holdings
from charli3_dendrite.lending.danogo.transactions.repay import _loan_output_value
from charli3_dendrite.lending.math import BASIS
from charli3_dendrite.lending.math import bps_mul_ceil
from charli3_dendrite.lending.math import floor_div
from charli3_dendrite.lending.transactions.infra import OUTPUT_MIN_ADA

if TYPE_CHECKING:
    from charli3_dendrite.lending.danogo.datums import PRational
    from charli3_dendrite.lending.danogo.oracles.redeemer import OracleUtxoType
    from charli3_dendrite.lending.danogo.transactions.context import Utxo

# The IncreaseLoanAmount validator applies the same 360-slot validity cap.
_INCREASE_VALIDITY_SLOTS = 360


def _increase_oracle_prep(
    snapshot: IncreaseLoanSnapshot,
    *,
    collateral_units: list[str],
    supply_token: str,
) -> tuple[
    OraclePriceCalcRdmr,
    Utxo,
    Utxo,
    list[tuple[Utxo, OracleUtxoType]],
    int,
    list[PRational] | None,
]:
    """Resolve the oracle Withdraw wiring for an increase-loan.

    Increase prices the loan's locked collateral (the validator reads it to value the
    loan against the NEW larger amount) AND the market's alternative supply tokens --
    exactly the create-loan price set -- so the pool's alt holdings are revalued from
    the oracle (`_alt_supply_update`) and the booked alt-token interest + new per-token
    rates are folded into the pool datum, mirroring create-loan (not repay's
    carry-through).

    Returns the seeded oracle redeemer (its indices filled once the builder ordering is
    final), the chosen global + path configs, the priced source leaves, and the alt
    revaluation (interest + new per-token rates).
    """
    market = snapshot.market_info
    price_units = set(collateral_units) | set(market.alt_supply_tokens)

    # Increase fully revalues the pool's alt holdings from the oracle and books the
    # change as interest (``revalue_alt_supply`` True), mirroring create-loan (not
    # repay's carry-through).
    return prepare_oracle_withdraw(
        snapshot,
        price_units=price_units,
        supply_token=supply_token,
        revalue_alt_supply=True,
    )


def _attach_increase_withdrawals(
    tx_builder: TransactionBuilder,
    snapshot: IncreaseLoanSnapshot,
    *,
    increase: IncreaseLoanAmount,
    oracle_rdmr: OraclePriceCalcRdmr,
    pool_skh: str,
) -> None:
    """Attach the two zero withdrawals an increase drives: the hub + the price calc.

    The ``Withdraw(pool_skh)`` hub is the orchestration point the pool/loan spends
    delegate to -- a zero withdrawal against the pool script's network-tagged reward
    address (``f1 + pool_skh``) carrying the SAME ``IncreaseLoanAmount`` redeemer, run
    under the pool reference script. This is the distinguishing wiring vs repay (which
    delegates through ``Withdraw(loan_skh)``). The oracle ``Withdraw`` runs the
    collateral price calc exactly as create-loan does. Both reward accounts are
    registered at a zero amount.
    """
    attach_oracle_withdraw(
        tx_builder,
        snapshot,
        oracle_rdmr=oracle_rdmr,
        hub=(snapshot.pool_script_ref, increase, pool_skh),
    )


def _finalize_increase_redeemers(
    tx_builder: TransactionBuilder,
    snapshot: IncreaseLoanSnapshot,
    *,
    increase: IncreaseLoanAmount,
    oracle_rdmr: OraclePriceCalcRdmr,
    pool_out: TransactionOutput,
    loan_out: TransactionOutput,
    used_leaves: list[tuple[Utxo, OracleUtxoType]],
    global_config: Utxo,
    path_config: Utxo,
) -> None:
    """Fill the increase redeemer indices from the FINAL (canonical) builder ordering.

    Output indices come from the order-preserving output list (the loan output is
    always present, so ``loan_out_idx`` is a plain int); reference-input indices from
    the canonical out-ref sort the Plutus script context exposes. The ``increase`` data
    is shared by the pool spend, loan spend, and hub Redeemer wrappers, so mutating it
    here updates every role at once. There is no fee output on current markets
    (origination rate 0), so ``fee_out_idx`` stays ``None``.
    """
    ref_index = _ref_index(tx_builder)
    increase.pool_out_idx = tx_builder.outputs.index(pool_out)
    increase.loan_out_idx = tx_builder.outputs.index(loan_out)
    if snapshot.protocol_config.out_ref is not None:
        increase.protocol_cfg_ref_idx = ref_index[snapshot.protocol_config.out_ref]
    if snapshot.market.out_ref is not None:
        increase.market_ref_idx = ref_index[snapshot.market.out_ref]

    # The structured deployment references its priced leaves in path-walk order; the
    # packed deployment sorts them by reference-input index. Preserve the order for the
    # structured deployment so its oracle redeemer reproduces byte-exact.
    deployment = deployment_for_oracle_skh(snapshot.oracle_skh)
    _synth_oracle_redeemer(
        tx_builder,
        oracle_rdmr=oracle_rdmr,
        used_leaves=used_leaves,
        prices=oracle_rdmr.prices,
        global_config=global_config,
        path_config=path_config,
        preserve_leaf_order=deployment.path_config_kind == "structured",
    )


def build_increase_loan(
    tx_builder: TransactionBuilder,
    *,
    snapshot: IncreaseLoanSnapshot,
    borrow_amount: int,
    txn_time: int | None = None,
) -> None:
    """Contribute a forward increase-loan (IncreaseLoanAmount) tx to `tx_builder`.

    Wires (mirroring `build_create_loan`, but spending an existing loan): the pool +
    loan script spends, the novel ``Withdraw(pool_skh)`` orchestration hub (a zero
    withdrawal against the pool script's reward address that the pool/loan spends
    delegate to), the oracle withdraw-zero price calc, the protocol-config / market /
    oracle reference inputs, the advanced pool output (supply paid out), and the raised
    loan output, plus the validity range. One ``IncreaseLoanAmount`` redeemer instance
    is reused byte-for-byte across the pool spend, the loan spend, and the hub. An
    increase mints nothing.

    ``borrow_amount`` is the additional supply token to borrow -- the amount the pool
    pays out (``-pool_changed_amount``). All protocol-derived data (pool/loan datums,
    oracle prices, redeemer indices) is synthesized from `snapshot`; the caller funds
    the borrower input, balances, evaluates, and submits.

    Args:
        tx_builder: The caller's builder to mutate.
        snapshot: Resolved live building blocks for the loan being increased.
        borrow_amount: Additional supply-token amount to borrow against the loan.
        txn_time: POSIX milliseconds for datum synthesis; defaults from the validity
            lower bound (the chain context's current slot).
    """
    pool = snapshot.pool
    if pool.out_ref is None or pool.datum is None or pool.address is None:
        raise ValueError("snapshot pool UTxO is missing its out-ref/datum/address")
    loan = snapshot.loan
    if loan.out_ref is None or loan.datum is None or loan.address is None:
        raise ValueError("snapshot loan UTxO is missing its out-ref/datum/address")
    if borrow_amount <= 0:
        raise ValueError("increase-loan borrow_amount must be positive")

    txn_time = _set_validity_window(
        tx_builder,
        slots=_INCREASE_VALIDITY_SLOTS,
        txn_time=txn_time,
    )

    market = snapshot.market_info
    supply_token = market.supply_token
    loan_skh = snapshot.loan_skh
    pool_skh = snapshot.pool_skh
    market_name = snapshot.market_name

    # The borrowed amount below the market minimum is the one the validator rejects;
    # fail loud here rather than surface a cryptic Ogmios script error.
    if borrow_amount < market.min_tx_amount:
        raise ValueError(
            f"increase of {borrow_amount} (market {market_name}) is below the market "
            f"minimum transaction amount {market.min_tx_amount}",
        )

    # The oracle Withdraw prices the loan's locked collateral (re-valued against the
    # new larger loan amount) plus the market's alt-supply tokens.
    collateral_units = _loan_collateral_units(
        loan,
        loan_skh=loan_skh,
        market_name=market_name,
    )
    (
        oracle_rdmr,
        global_config,
        path_config,
        used_leaves,
        alt_tokens_interest,
        new_alt_rates,
    ) = _increase_oracle_prep(
        snapshot,
        collateral_units=collateral_units,
        supply_token=supply_token,
    )

    # The origination fee is folded into the loan principal (`borrow_delta`) and the
    # pool fee pot. Current mainnet markets charge 0 (rate 0), matching the captured
    # txs; the field is recomputed inside `synth_pool_datum_increase_loan` too.
    loan_origination_fee = bps_mul_ceil(
        borrow_amount,
        market.loan_origination_fee_rate,
    )
    borrow_delta = borrow_amount + loan_origination_fee
    pool_changed_amount = -borrow_amount

    prev_pool_datum = PoolDatum.from_cbor(pool.datum)
    new_pool_datum = synth_pool_datum_increase_loan(
        prev_pool_datum,
        borrow_delta=borrow_delta,
        txn_time=txn_time,
        power_base=market.power_base,
        base_rate=market.base_rate,
        loan_fee_rate=market.loan_fee_rate,
        loan_origination_fee=loan_origination_fee,
        alt_tokens_interest=alt_tokens_interest,
        new_alt_supply_tokens_rate=new_alt_rates,
    )

    loan_in = snapshot.loan_datum
    loan_debt = current_loan_amount(
        loan_amount=loan_in.loan_amount,
        current_index=new_pool_datum.interest_index,
        initial_index=loan_in.initial_interest_index,
    )
    loan_out_datum = update_loan_datum_increase(
        loan_in,
        current_loan_amount=loan_debt,
        pool_changed_amount=pool_changed_amount,
        new_interest_index=new_pool_datum.interest_index,
        loan_origination_fee=loan_origination_fee,
    )

    # One IncreaseLoanAmount redeemer drives the pool spend, the loan spend, and the
    # Withdraw(pool_skh) hub. Indices are placeholders until the final ordering is
    # known; the single data object is shared by every Redeemer wrapper so the in-place
    # fixups below propagate to all roles.
    increase = IncreaseLoanAmount(
        pool_out_idx=0,
        loan_out_idx=0,
        fee_out_idx=NoneVal(),
        protocol_cfg_ref_idx=0,
        market_ref_idx=0,
        pool_in_out_ref=OutputReference(
            transaction_id=bytes.fromhex(pool.out_ref[0]),
            output_index=pool.out_ref[1],
        ),
    )

    # --- pool + loan script spends (reference scripts via add_script_input) --------
    tx_builder.add_script_input(
        _to_utxo(pool),
        script=_to_utxo(snapshot.pool_script_ref),
        redeemer=Redeemer(increase),
    )
    tx_builder.add_script_input(
        _to_utxo(loan),
        script=_to_utxo(snapshot.loan_mint_script_ref),
        redeemer=Redeemer(increase),
    )

    # --- config / market / oracle reference inputs (read-only) --------------------
    tx_builder.reference_inputs.add(_to_utxo(snapshot.protocol_config))
    tx_builder.reference_inputs.add(_to_utxo(snapshot.market))
    tx_builder.reference_inputs.add(_to_utxo(global_config))
    tx_builder.reference_inputs.add(_to_utxo(path_config))
    for leaf, _otype in used_leaves:
        tx_builder.reference_inputs.add(_to_utxo(leaf))

    # --- delegation hub + oracle price calc (both zero withdrawals) ---------------
    _attach_increase_withdrawals(
        tx_builder,
        snapshot,
        increase=increase,
        oracle_rdmr=oracle_rdmr,
        pool_skh=pool_skh,
    )

    # --- outputs: advanced pool (supply paid out), then the raised loan -----------
    pool_out = build_pool_script_output(
        pool,
        supply_token=supply_token,
        supply_delta=pool_changed_amount,
        datum=new_pool_datum,
    )
    tx_builder.add_output(pool_out)

    loan_out = TransactionOutput(
        address=Address.decode(loan.address),
        amount=_loan_output_value(
            loan,
            loan_skh=loan_skh,
            market_name=market_name,
            collateral=None,
        ),
        datum=loan_out_datum,
    )
    loan_out.amount.coin = max(
        loan_out.amount.coin,
        OUTPUT_MIN_ADA,
        min_lovelace(tx_builder.context, output=loan_out),
    )
    tx_builder.add_output(loan_out)

    # --- resolve role indices from the FINAL builder ordering ---------------------
    _finalize_increase_redeemers(
        tx_builder,
        snapshot,
        increase=increase,
        oracle_rdmr=oracle_rdmr,
        pool_out=pool_out,
        loan_out=loan_out,
        used_leaves=used_leaves,
        global_config=global_config,
        path_config=path_config,
    )


def add_increase_funding(
    tx_builder: TransactionBuilder,
    *,
    snapshot: IncreaseLoanSnapshot,
    actor: Address,
    actor_utxo: str,
) -> dict[str, Any]:
    """Add the borrower (funding) input for an increase-loan.

    The borrower proves loan ownership with the owner NFT (qty 1) and supplies
    lovelace for fees/min-ADA; it supplies NO supply token (the pool pays the borrowed
    supply out to the borrower via balancing). This mirrors `add_repay_funding` minus
    the supply-token leg. The input's value is resolved live by Ogmios, so a nominal
    output value is enough here (manual assembly serializes only the out-ref).

    Returns the Ogmios `additionalUtxo` entry for the borrower input: every other
    Danogo input/reference is a freshly resolved live UTxO Ogmios resolves from its own
    ledger, so only this funding input -- a plain, datum-less wallet UTxO that may
    already be spent -- must be supplied explicitly.
    """
    funding: dict[str, int] = {snapshot.owner_nft: 1}
    return add_actor_funding(
        tx_builder,
        actor=actor,
        actor_utxo=actor_utxo,
        funding=funding,
    )


def safe_increase_amount(
    snapshot: IncreaseLoanSnapshot,
    *,
    borrow_amount: int,
    txn_time: int | None = None,
) -> int:
    """Preflight the post-increase health factor + utilization cap for an increase.

    The on-chain IncreaseLoanAmount validator requires (a) the threshold-weighted
    collateral value to strictly exceed the NEW (larger) loan amount, and (b) the
    pool's post-increase utilization to stay within the market's ``util_cap``. This
    fails loud on either rather than surface a cryptic Ogmios script error.

    The loan's existing locked collateral is priced via the live oracle recipes
    (`_forward_prices_and_leaves`) and weighted by the market liquidation threshold
    (bps), giving the threshold-weighted collateral value
    (`total_collateral_val_with_threshold`). The new loan amount is the interest-accrued
    debt plus the requested borrow (plus any origination fee). The utilization is the
    post-increase ``total_borrow / total_supply`` in basis points, compared against
    ``util_cap``. A collateral unit with no resolvable oracle price (or not accepted by
    the market) is a hard error -- it would misprice the loan.

    Returns the threshold-weighted collateral value (the headroom the loan affords).
    """
    market = snapshot.market_info
    if snapshot.pool.datum is None:
        raise ValueError("snapshot pool UTxO is missing its datum")
    prev = PoolDatum.from_cbor(snapshot.pool.datum)
    loan_in = snapshot.loan_datum

    advanced_index = current_interest_index(
        prev.interest_index,
        borrow_apy=prev.borrow_apy,
        interest_time=prev.interest_time,
        txn_time=txn_time if txn_time is not None else prev.interest_time,
    )
    loan_debt = current_loan_amount(
        loan_amount=loan_in.loan_amount,
        current_index=advanced_index,
        initial_index=loan_in.initial_interest_index,
    )
    loan_origination_fee = bps_mul_ceil(
        borrow_amount,
        market.loan_origination_fee_rate,
    )
    new_loan_amount = loan_debt + borrow_amount + loan_origination_fee

    collateral = _loan_collateral_holdings(
        snapshot.loan,
        loan_skh=snapshot.loan_skh,
        market_name=snapshot.market_name,
    )
    health_value = collateral_health_value(snapshot, collateral)
    if health_value <= new_loan_amount:
        raise ValueError(
            f"post-increase collateral value {health_value} does not exceed the new "
            f"loan amount {new_loan_amount}: the loan would be under-collateralized",
        )

    # Utilization cap: the pool's post-increase borrow/supply must stay within the
    # market's cap. Both move by the accrued interest; borrow additionally rises by the
    # borrowed amount (booked as borrow_delta), supply only by the net accrued interest.
    accrued = floor_div(
        prev.total_borrow * (advanced_index - prev.interest_index),
        prev.interest_index,
    )
    fee = bps_mul_ceil(accrued, market.loan_fee_rate)
    new_total_borrow = (
        prev.total_borrow + accrued + (borrow_amount + loan_origination_fee)
    )
    new_total_supply = prev.total_supply + accrued - fee
    if new_total_supply > 0:
        utilization = floor_div(new_total_borrow * BASIS, new_total_supply)
        if utilization > market.util_cap:
            raise ValueError(
                f"post-increase utilization {utilization}bps exceeds the market "
                f"utilization cap {market.util_cap}bps",
            )

    return health_value
