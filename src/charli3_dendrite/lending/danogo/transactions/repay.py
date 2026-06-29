"""Assemble a forward Danogo repay (DecreaseLoanAmount) into a `TransactionBuilder`.

`build_repay` contributes a borrower repayment to a caller-supplied
`pycardano.TransactionBuilder`: the pool + loan script spends, the novel
``Withdraw(loan_skh)`` orchestration hub the spends (and full-repay burn) delegate to,
the oracle withdraw-zero price calc, the protocol-config / market / oracle reference
inputs, the updated pool + always-present fee outputs, the reduced loan output (partial
only), and the loan + owner-NFT burn (full only). A full repay clears the debt and
closes the loan; a partial repay reduces it. The caller owns balancing/evaluation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Any

from pycardano import Address
from pycardano import Asset
from pycardano import AssetName
from pycardano import MultiAsset
from pycardano import Redeemer
from pycardano import ScriptHash
from pycardano import TransactionBuilder
from pycardano import TransactionOutput
from pycardano import Value
from pycardano import min_lovelace

from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.lending.danogo.datums import PoolDatum
from charli3_dendrite.lending.danogo.math import current_interest_index
from charli3_dendrite.lending.danogo.math import current_loan_amount
from charli3_dendrite.lending.danogo.oracles.deployments import (
    deployment_for_oracle_skh,
)
from charli3_dendrite.lending.danogo.oracles.redeemer import OraclePriceCalcRdmr
from charli3_dendrite.lending.danogo.oracles.redeemer import OracleUtxoType
from charli3_dendrite.lending.danogo.transactions._common import _market_fee_address
from charli3_dendrite.lending.danogo.transactions._common import _pool_holding
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
from charli3_dendrite.lending.danogo.transactions.context import RepaySnapshot
from charli3_dendrite.lending.danogo.transactions.context import _loan_collateral_units
from charli3_dendrite.lending.danogo.transactions.datum_synth import (
    synth_pool_datum_decrease_loan,
)
from charli3_dendrite.lending.danogo.transactions.datum_synth import (
    update_loan_datum_repay,
)
from charli3_dendrite.lending.danogo.transactions.redeemers import DecreaseLoanAmount
from charli3_dendrite.lending.danogo.transactions.redeemers import NoneVal
from charli3_dendrite.lending.danogo.transactions.redeemers import OptionInt
from charli3_dendrite.lending.danogo.transactions.redeemers import OutputReference
from charli3_dendrite.lending.danogo.transactions.redeemers import SomeInt
from charli3_dendrite.lending.transactions.infra import OUTPUT_MIN_ADA
from charli3_dendrite.utility import asset_to_value

if TYPE_CHECKING:
    from charli3_dendrite.lending.danogo.datums import LoanDatum
    from charli3_dendrite.lending.danogo.datums import PRational
    from charli3_dendrite.lending.danogo.transactions.context import Utxo

# The DecreaseLoanAmount (repay) validator applies the same 360-slot validity cap.
_REPAY_VALIDITY_SLOTS = 360


def _loan_collateral_holdings(
    loan: Utxo,
    *,
    loan_skh: str,
    market_name: str,
) -> dict[str, int]:
    """The loan UTxO's locked collateral as a ``unit -> quantity`` map.

    Every native asset the loan carries except the market loan token
    (``loan_skh`` + ``market_name``); the (min-ADA) lovelace balance is not a
    collateral asset and is excluded.
    """
    holdings: dict[str, int] = {}
    for policy, name, qty in loan.assets:
        if policy == loan_skh and name == market_name:
            continue
        holdings[policy + name] = holdings.get(policy + name, 0) + qty
    return holdings


def _loan_output_value(
    loan: Utxo,
    *,
    loan_skh: str,
    market_name: str,
    collateral: dict[str, int] | None,
) -> Value:
    """The reduced loan output's value: loan token + collateral + the input's ADA.

    The loan token (qty 1) and the loan input's lovelace are always carried forward.
    ``collateral`` gives the TARGET absolute collateral amounts the loan output should
    lock; a falsy ``collateral`` (``None`` or ``{}``) keeps the loan input's current
    collateral unchanged. The result is canonically ordered (`Assets` sorts by unit),
    so it reproduces the on-chain loan-output value byte-exact.
    """
    root: dict[str, int] = {"lovelace": loan.lovelace}
    root[loan_skh + market_name] = 1
    target = (
        collateral
        if collateral
        else _loan_collateral_holdings(
            loan,
            loan_skh=loan_skh,
            market_name=market_name,
        )
    )
    for unit, qty in target.items():
        root[unit] = root.get(unit, 0) + qty
    return asset_to_value(Assets(**root))


def _loan_burn(loan_skh: str, market_name: str, owner_name: str) -> MultiAsset:
    """Burn -1 market loan token + -1 owner NFT (both under the loan script policy).

    Built as a `MultiAsset` directly so the negative quantities are preserved (a full
    repay closes the loan, retiring both tokens it was opened with).
    """
    return MultiAsset(
        {
            ScriptHash(bytes.fromhex(loan_skh)): Asset(
                {
                    AssetName(bytes.fromhex(market_name)): -1,
                    AssetName(bytes.fromhex(owner_name)): -1,
                },
            ),
        },
    )


def _pool_holds_alt_supply(snapshot: RepaySnapshot) -> bool:
    """True if the pool actually holds any of the market's alternative supply tokens."""
    return any(
        _pool_holding(snapshot.pool, alt) > 0
        for alt in snapshot.market_info.alt_supply_tokens
    )


def _repay_oracle_prep(
    snapshot: RepaySnapshot,
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
    """Resolve the oracle Withdraw wiring for a repay.

    Repay always re-prices the loan's locked collateral (the validator reads it to
    value the loan), reusing the same forward-pricing machinery create-loan does. It
    additionally re-prices the pool's alternative supply-token holdings -- booking the
    change as interest, exactly as create-loan -- only when the pool actually holds
    some; a pool holding none re-prices nothing on the alt side (the captured repays),
    so the alt rate list is carried through and ``alt_tokens_interest`` is 0.

    Returns the seeded oracle redeemer (its indices filled once the builder ordering
    is final), the chosen global + path configs, the priced source leaves, and the
    alt revaluation (interest + new per-token rates).
    """
    market = snapshot.market_info
    holds_alt = _pool_holds_alt_supply(snapshot)
    price_units = set(collateral_units)
    if holds_alt:
        price_units |= set(market.alt_supply_tokens)

    # Repay carries the pool's alt-supply rates through unchanged
    # (``revalue_alt_supply`` False): the captured repays re-price the held alt tokens
    # to the rate they already carry, so the booked alt-token interest is 0 and the
    # prior rate list is preserved (the byte-exact pool datum target). The held alt
    # tokens are still priced (in ``price_units``) so the oracle Withdraw, which
    # verifies every priced asset against its source leaves, accepts them.
    return prepare_oracle_withdraw(
        snapshot,
        price_units=price_units,
        supply_token=supply_token,
        revalue_alt_supply=False,
    )


def _require_market_datum(snapshot: RepaySnapshot) -> str:
    """The market UTxO's datum CBOR, or raise if the snapshot lacks it."""
    if snapshot.market.datum is None:
        raise ValueError("snapshot market UTxO is missing its datum")
    return snapshot.market.datum


def _attach_repay_withdrawals(
    tx_builder: TransactionBuilder,
    snapshot: RepaySnapshot,
    *,
    decrease: DecreaseLoanAmount,
    oracle_rdmr: OraclePriceCalcRdmr,
    loan_skh: str,
) -> None:
    """Attach the two zero withdrawals a repay drives: the hub + the oracle price calc.

    The ``Withdraw(loan_skh)`` hub is the orchestration point the pool/loan spends
    (and, on full repay, the mint burn) delegate to -- a zero withdrawal against the
    loan script's network-tagged reward address (``f1 + loan_skh``) carrying the SAME
    ``DecreaseLoanAmount`` redeemer. The oracle ``Withdraw`` runs the collateral price
    calc exactly as create-loan does. Both reward accounts are registered at a zero
    amount.
    """
    attach_oracle_withdraw(
        tx_builder,
        snapshot,
        oracle_rdmr=oracle_rdmr,
        hub=(snapshot.loan_mint_script_ref, decrease, loan_skh),
    )


def _finalize_repay_redeemers(
    tx_builder: TransactionBuilder,
    snapshot: RepaySnapshot,
    *,
    decrease: DecreaseLoanAmount,
    oracle_rdmr: OraclePriceCalcRdmr,
    pool_out: TransactionOutput,
    loan_out: TransactionOutput | None,
    fee_out: TransactionOutput,
    used_leaves: list[tuple[Utxo, OracleUtxoType]],
    global_config: Utxo,
    path_config: Utxo,
) -> None:
    """Fill the repay redeemer indices from the FINAL (canonical) builder ordering.

    Output indices come from the order-preserving output list (the loan output is
    omitted on a full repay, so ``loan_out_idx`` stays ``None``); reference-input
    indices from the canonical out-ref sort the Plutus script context exposes. The
    ``decrease`` data is shared by the pool spend, loan spend, mint burn, and hub
    Redeemer wrappers, so mutating it here updates every role at once.
    """
    ref_index = _ref_index(tx_builder)
    decrease.pool_out_idx = tx_builder.outputs.index(pool_out)
    decrease.loan_out_idx = (
        NoneVal() if loan_out is None else SomeInt(tx_builder.outputs.index(loan_out))
    )
    decrease.fee_out_idx = SomeInt(tx_builder.outputs.index(fee_out))
    if snapshot.protocol_config.out_ref is not None:
        decrease.protocol_cfg_ref_idx = ref_index[snapshot.protocol_config.out_ref]
    if snapshot.market.out_ref is not None:
        decrease.market_ref_idx = ref_index[snapshot.market.out_ref]

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


def _add_repay_outputs(
    tx_builder: TransactionBuilder,
    *,
    snapshot: RepaySnapshot,
    pool: Utxo,
    loan: Utxo,
    new_pool_datum: PoolDatum,
    loan_in: LoanDatum,
    full_repay: bool,
    supply_token: str,
    pool_change_amount: int,
    fee_output_amount: int,
    loan_debt: int,
    loan_skh: str,
    market_name: str,
    collateral: dict[str, int] | None,
) -> tuple[TransactionOutput, TransactionOutput | None, TransactionOutput]:
    """Build + add the repay outputs: updated pool, (partial) loan, and the fee output.

    The pool's raw supply-token holdings rise by the repayment net of the swept fee
    (the borrower pays the full ``pool_change_amount``; the fee output takes the rest).
    A partial repay also emits the reduced loan UTxO -- carrying the TARGET collateral
    (`collateral`, absolute amounts) or, when falsy, the loan input's current
    collateral unchanged; a full repay omits it. The fee output -- always present --
    pays ``fee_output_amount`` supply tokens to the market fee-recipient address.
    Returns the three outputs (the loan output is ``None`` on a full repay) so the
    caller resolves the redeemer's output indices from the final ordering.
    """
    if pool.address is None or loan.address is None:
        raise ValueError("snapshot pool/loan UTxO is missing its address")
    pool_out = build_pool_script_output(
        pool,
        supply_token=supply_token,
        supply_delta=pool_change_amount - fee_output_amount,
        datum=new_pool_datum,
    )
    tx_builder.add_output(pool_out)

    loan_out: TransactionOutput | None = None
    if not full_repay:
        loan_out_datum = update_loan_datum_repay(
            loan_in,
            current_loan_amount=loan_debt,
            pool_change_amount=pool_change_amount,
            new_interest_index=new_pool_datum.interest_index,
        )
        loan_out = TransactionOutput(
            address=Address.decode(loan.address),
            amount=_loan_output_value(
                loan,
                loan_skh=loan_skh,
                market_name=market_name,
                collateral=collateral,
            ),
            datum=loan_out_datum,
        )
        loan_out.amount.coin = max(
            loan_out.amount.coin,
            OUTPUT_MIN_ADA,
            min_lovelace(tx_builder.context, output=loan_out),
        )
        tx_builder.add_output(loan_out)

    fee_addr = _market_fee_address(_require_market_datum(snapshot))
    fee_key = "lovelace" if supply_token == "lovelace" else supply_token
    fee_out = TransactionOutput(
        fee_addr,
        asset_to_value(Assets(**{fee_key: fee_output_amount})),
    )
    fee_out.amount.coin = max(
        fee_out.amount.coin,
        min_lovelace(tx_builder.context, output=fee_out),
    )
    tx_builder.add_output(fee_out)
    return pool_out, loan_out, fee_out


def build_repay(
    tx_builder: TransactionBuilder,
    *,
    snapshot: RepaySnapshot,
    amount: int,
    collateral: dict[str, int] | None = None,
    txn_time: int | None = None,
) -> None:
    """Contribute a forward repay (DecreaseLoanAmount) transaction to `tx_builder`.

    Wires (mirroring `build_create_loan`): the pool + loan script spends, the novel
    ``Withdraw(loan_skh)`` orchestration hub (a zero withdrawal against the loan
    script's reward address that the pool/loan spends and the loan mint-burn delegate
    to), the oracle withdraw-zero price calc, the protocol-config / market / oracle
    reference inputs, the updated pool output, the always-present fee output, the loan
    output (partial repay only), and the loan + owner-NFT burn (full repay only), plus
    the validity range. One ``DecreaseLoanAmount`` redeemer instance is reused
    byte-for-byte across the pool spend, the loan spend, the mint burn, and the hub.

    ``amount`` is the requested repayment (the redeemer's ``pool_change_amount``). It
    drives a full vs partial repay: a request at or above the loan's accrued debt is a
    full repay (clamped to the debt, closing the loan and burning its tokens); a
    smaller request is a partial repay (it must be at least the market's minimum
    transaction amount, leaving a reduced loan UTxO open). All protocol-derived data
    (pool/loan datums, fee amount, oracle prices, redeemer indices) is synthesized
    from `snapshot`; the caller funds the borrower input, balances, evaluates, and
    submits.

    A partial repay may also MODIFY the loan's collateral in the same transaction:
    ``collateral`` is the TARGET absolute collateral the loan output should lock
    (``unit -> quantity``). A falsy ``collateral`` (``None`` or ``{}``) leaves the
    collateral unchanged -- the loan input's current holdings are carried forward
    (the backward-compatible default). Collateral modification is partial-only: a
    full repay releases all collateral, so a target on a full repay raises. Adding
    collateral is funded by the borrower (`add_repay_funding`); removed collateral
    returns to the borrower via balancing. The oracle ``Withdraw`` prices the
    post-modification collateral set (the loan input's units unioned with the
    target's), and the caller should preflight the post-repay health factor with
    `safe_collateral_for_repay`.

    Args:
        tx_builder: The caller's builder to mutate.
        snapshot: Resolved live building blocks for the loan being repaid.
        amount: Supply-token amount to repay (the redeemer's ``pool_change_amount``);
            clamped to the loan's accrued debt for a full repay.
        collateral: Target absolute collateral amounts for the loan output (partial
            repay only); falsy leaves the collateral unchanged.
        txn_time: POSIX milliseconds for datum synthesis; defaults from the validity
            lower bound (the chain context's current slot).
    """
    pool = snapshot.pool
    if pool.out_ref is None or pool.datum is None or pool.address is None:
        raise ValueError("snapshot pool UTxO is missing its out-ref/datum/address")
    loan = snapshot.loan
    if loan.out_ref is None or loan.datum is None or loan.address is None:
        raise ValueError("snapshot loan UTxO is missing its out-ref/datum/address")

    txn_time = _set_validity_window(
        tx_builder,
        slots=_REPAY_VALIDITY_SLOTS,
        txn_time=txn_time,
    )

    market = snapshot.market_info
    supply_token = market.supply_token
    loan_skh = snapshot.loan_skh
    market_name = snapshot.market_name

    # The loan's accrued debt at repay time decides full vs partial. The interest
    # index advances purely from the accrual interval (independent of the repayment),
    # so the debt is known before the amount is clamped.
    prev_pool_datum = PoolDatum.from_cbor(pool.datum)
    advanced_index = current_interest_index(
        prev_pool_datum.interest_index,
        borrow_apy=prev_pool_datum.borrow_apy,
        interest_time=prev_pool_datum.interest_time,
        txn_time=txn_time,
    )
    loan_in = snapshot.loan_datum
    loan_debt = current_loan_amount(
        loan_amount=loan_in.loan_amount,
        current_index=advanced_index,
        initial_index=loan_in.initial_interest_index,
    )

    full_repay = amount >= loan_debt
    if full_repay and collateral:
        raise ValueError(
            "collateral modification is only valid on a partial repay; a full repay "
            "(amount >= the loan's accrued debt) releases all collateral",
        )
    if full_repay:
        pool_change_amount = loan_debt
    else:
        pool_change_amount = amount
        # A partial repay below the market minimum is the one the validator rejects;
        # fail loud here rather than surface a cryptic Ogmios script error.
        if pool_change_amount < market.min_tx_amount:
            raise ValueError(
                f"partial repay of {pool_change_amount} (market {market_name}) is "
                f"below the market minimum transaction amount {market.min_tx_amount}",
            )

    # The oracle Withdraw prices the POST-modification collateral set: the loan input's
    # units unioned with the target's keys. With no modification (or a same-unit one,
    # as the captured collateral-mod txs are) this is exactly the loan input's units,
    # keeping the priced/leaf set byte-identical to a plain repay.
    collateral_units = _loan_collateral_units(
        loan,
        loan_skh=loan_skh,
        market_name=market_name,
    )
    if collateral:
        collateral_units = sorted(set(collateral_units) | set(collateral))
    (
        oracle_rdmr,
        global_config,
        path_config,
        used_leaves,
        alt_tokens_interest,
        new_alt_rates,
    ) = _repay_oracle_prep(
        snapshot,
        collateral_units=collateral_units,
        supply_token=supply_token,
    )

    # The repayment pays down borrow; the fee pot is swept to the fee output.
    new_pool_datum, fee_output_amount = synth_pool_datum_decrease_loan(
        prev_pool_datum,
        pool_change_amount=pool_change_amount,
        txn_time=txn_time,
        power_base=market.power_base,
        base_rate=market.base_rate,
        loan_fee_rate=market.loan_fee_rate,
        alt_tokens_interest=alt_tokens_interest,
        new_alt_supply_tokens_rate=new_alt_rates,
    )

    # One DecreaseLoanAmount redeemer drives the pool spend, the loan spend, the loan
    # mint-burn (full repay), and the Withdraw(loan_skh) hub. Indices are placeholders
    # until the final ordering is known; the single data object is shared by every
    # Redeemer wrapper so the in-place fixups below propagate to all roles.
    loan_out_idx: OptionInt = NoneVal() if full_repay else SomeInt(0)
    decrease = DecreaseLoanAmount(
        pool_out_idx=0,
        loan_out_idx=loan_out_idx,
        fee_out_idx=SomeInt(0),
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
        redeemer=Redeemer(decrease),
    )
    tx_builder.add_script_input(
        _to_utxo(loan),
        script=_to_utxo(snapshot.loan_mint_script_ref),
        redeemer=Redeemer(decrease),
    )

    # --- config / market / oracle reference inputs (read-only) --------------------
    tx_builder.reference_inputs.add(_to_utxo(snapshot.protocol_config))
    tx_builder.reference_inputs.add(_to_utxo(snapshot.market))
    tx_builder.reference_inputs.add(_to_utxo(global_config))
    tx_builder.reference_inputs.add(_to_utxo(path_config))
    for leaf, _otype in used_leaves:
        tx_builder.reference_inputs.add(_to_utxo(leaf))

    # --- mint burn (full repay only): -1 loan token + -1 owner NFT ----------------
    if full_repay:
        tx_builder.add_minting_script(
            _to_utxo(snapshot.loan_mint_script_ref),
            redeemer=Redeemer(decrease),
        )
        owner_name = snapshot.owner_nft[len(loan_skh) :]
        burn_multi = _loan_burn(loan_skh, market_name, owner_name)
        tx_builder.mint = (
            burn_multi if tx_builder.mint is None else tx_builder.mint + burn_multi
        )

    # --- delegation hub + oracle price calc (both zero withdrawals) ---------------
    _attach_repay_withdrawals(
        tx_builder,
        snapshot,
        decrease=decrease,
        oracle_rdmr=oracle_rdmr,
        loan_skh=loan_skh,
    )

    # --- outputs: updated pool, (partial) reduced loan, then the fee output --------
    pool_out, loan_out, fee_out = _add_repay_outputs(
        tx_builder,
        snapshot=snapshot,
        pool=pool,
        loan=loan,
        new_pool_datum=new_pool_datum,
        loan_in=loan_in,
        full_repay=full_repay,
        supply_token=supply_token,
        pool_change_amount=pool_change_amount,
        fee_output_amount=fee_output_amount,
        loan_debt=loan_debt,
        loan_skh=loan_skh,
        market_name=market_name,
        collateral=collateral,
    )

    # --- resolve role indices from the FINAL builder ordering ---------------------
    _finalize_repay_redeemers(
        tx_builder,
        snapshot,
        decrease=decrease,
        oracle_rdmr=oracle_rdmr,
        pool_out=pool_out,
        loan_out=loan_out,
        fee_out=fee_out,
        used_leaves=used_leaves,
        global_config=global_config,
        path_config=path_config,
    )


def add_repay_funding(
    tx_builder: TransactionBuilder,
    *,
    snapshot: RepaySnapshot,
    actor: Address,
    actor_utxo: str,
    amount: int,
    collateral: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Add the borrower (funding) input for a repay + placeholder fee.

    The borrower supplies the supply token to repay and proves loan ownership with the
    owner NFT (qty 1); a full repay burns that owner NFT and releases the loan's
    collateral back to the borrower via balancing (no explicit collateral output). The
    input's value is resolved live by Ogmios, so a nominal output value is enough here
    (manual assembly serializes only the out-ref).

    When a partial repay also MODIFIES collateral, ``collateral`` is the TARGET
    absolute collateral amounts in the loan output (the same value passed to
    `build_repay`). The borrower must fund any ADDED collateral -- the positive
    per-unit deltas of the target over the loan input's current holdings -- so those
    are added to the funding value. REMOVED collateral (negative deltas) is returned
    to the borrower by balancing and needs no explicit funding.

    Returns the Ogmios `additionalUtxo` entry for the borrower input: every other
    Danogo input/reference is a freshly resolved live UTxO Ogmios resolves from its own
    ledger, so only this funding input -- a plain, datum-less wallet UTxO that may
    already be spent -- must be supplied explicitly.
    """
    supply_token = snapshot.market_info.supply_token
    funding: dict[str, int] = {snapshot.owner_nft: 1}
    if supply_token == "lovelace":
        funding["lovelace"] = funding.get("lovelace", 0) + amount
    else:
        funding[supply_token] = funding.get(supply_token, 0) + amount

    if collateral:
        current = _loan_collateral_holdings(
            snapshot.loan,
            loan_skh=snapshot.loan_skh,
            market_name=snapshot.market_name,
        )
        for unit, target_qty in collateral.items():
            added = target_qty - current.get(unit, 0)
            if added > 0:
                funding[unit] = funding.get(unit, 0) + added

    return add_actor_funding(
        tx_builder,
        actor=actor,
        actor_utxo=actor_utxo,
        funding=funding,
    )


def safe_collateral_for_repay(
    snapshot: RepaySnapshot,
    *,
    collateral: dict[str, int],
    loan_out_amount: int,
) -> int:
    """Preflight the post-repay health factor for a collateral-modifying repay.

    Mirrors `safe_borrow_amount`: each TARGET collateral unit is priced via the live
    oracle recipes (`_forward_prices_and_leaves`) and weighted by the market
    liquidation threshold (bps), giving the threshold-weighted collateral value
    (`total_collateral_val_with_threshold`). The on-chain DecreaseLoanAmount validator
    requires that value to strictly exceed the reduced loan amount (``loan_out_amount``
    -- the loan output datum's ``loan_amount`` after the repay), so this raises on an
    under-collateralized target rather than surface a cryptic Ogmios script error. A
    target unit with no resolvable oracle price (or not accepted by the market) is a
    hard error too -- it would misprice the loan -- so it raises rather than skip.

    Returns the threshold-weighted collateral value (the headroom the target affords).
    """
    health_value = collateral_health_value(snapshot, collateral)
    if health_value <= loan_out_amount:
        raise ValueError(
            f"post-repay collateral value {health_value} does not exceed the reduced "
            f"loan amount {loan_out_amount}: the target is under-collateralized",
        )
    return health_value
