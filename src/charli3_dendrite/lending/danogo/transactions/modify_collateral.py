"""Assemble a forward Danogo modify-collateral (ModifyCollaterals) into a builder.

`build_modify_collateral` contributes "add and/or remove collateral on an existing
loan, without repaying" to a caller-supplied `pycardano.TransactionBuilder`: the loan
script spend (under the loan reference script), the oracle withdraw-zero price calc,
the protocol-config / market / pool / oracle reference inputs, and the continuing loan
output carrying the TARGET collateral. The caller owns balancing/evaluation.

This is a slimmed repay collateral-mod path: the loan debt and `LoanDatum` are
UNCHANGED (only the locked collateral value moves), so the loan output reuses the
input loan datum verbatim -- there is no datum synthesis. The pool is a REFERENCE
input (read for the interest index), NEVER spent, so an unlike-repay modify has no
pool spend, no pool-datum synthesis, no fee output, no mint, and no withdraw hub: the
only two redeemers are the loan ``Spend`` (``ModifyCollaterals``) and the oracle
``Withdraw`` (``OraclePriceCalcRdmr``).

The oracle ``Withdraw`` re-prices the loan output's TARGET collateral set through the
same forward-pricing machinery create-loan/repay use, including the structured
deployment's multi-path pricing (each priced unit's primary derivation path composed
forward, its alternative paths referenced as deviation cross-checks). A caller may
instead supply a pre-resolved ``oracle_redeemer`` (e.g. captured on-chain) to drive the
oracle ``Withdraw`` directly; the builder then references the FULL resolved oracle
reference set so that the redeemer's reference indices line up with the canonical
ordering. This override is optional -- the forward pricer reproduces the price calc for
every Danogo source kind in use.
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
from charli3_dendrite.lending.danogo.transactions._common import collateral_health_value
from charli3_dendrite.lending.danogo.transactions._common import prepare_oracle_withdraw
from charli3_dendrite.lending.danogo.transactions.context import (
    ModifyCollateralSnapshot,
)
from charli3_dendrite.lending.danogo.transactions.context import _loan_collateral_units
from charli3_dendrite.lending.danogo.transactions.redeemers import ModifyCollaterals
from charli3_dendrite.lending.danogo.transactions.repay import _loan_collateral_holdings
from charli3_dendrite.lending.danogo.transactions.repay import _loan_output_value
from charli3_dendrite.lending.transactions.infra import OUTPUT_MIN_ADA

if TYPE_CHECKING:
    from charli3_dendrite.lending.danogo.oracles.redeemer import OracleUtxoType
    from charli3_dendrite.lending.danogo.transactions.context import Utxo

# The ModifyCollaterals validator applies the same 360-slot validity cap.
_MODIFY_VALIDITY_SLOTS = 360


def _post_modification_units(
    snapshot: ModifyCollateralSnapshot,
    target_collateral: dict[str, int] | None,
) -> list[str]:
    """Collateral units the oracle prices: the loan OUTPUT's (target) collateral.

    The validator values the continuing loan output, so it prices exactly the units the
    output locks -- the target collateral with a positive quantity. A cross-type
    modification (swap) drops the removed unit entirely, so pricing the target rather
    than the loan input's units leaves the removed type unpriced, matching the on-chain
    redeemer. With no target the loan input's units are priced (a no-op modify).
    """
    if target_collateral:
        return sorted(unit for unit, qty in target_collateral.items() if qty > 0)
    return sorted(
        _loan_collateral_units(
            snapshot.loan,
            loan_skh=snapshot.loan_skh,
            market_name=snapshot.market_name,
        ),
    )


def _modify_oracle_prep(
    snapshot: ModifyCollateralSnapshot,
    *,
    collateral_units: list[str],
) -> tuple[OraclePriceCalcRdmr, Utxo, Utxo, list[tuple[Utxo, OracleUtxoType]]]:
    """Forward-resolve the oracle Withdraw wiring for a modify-collateral.

    A modify re-prices the loan's POST-modification collateral exactly as repay
    re-prices the loan's collateral (the validator reads it to value the loan against
    the unchanged debt), reusing the same forward-pricing machinery. The pool is not
    spent and not revalued, so -- unlike repay/create-loan -- there is no alt-supply
    revaluation: only the collateral is priced.

    Returns the seeded oracle redeemer (its indices filled once the builder ordering is
    final), the chosen global + path configs, and the priced source leaves.
    """
    # A modify spends no pool, so there is no alt-supply revaluation
    # (``revalue_alt_supply`` False); only the collateral is priced and the discarded
    # alt fields stay at their no-op ``(0, None)``.
    (
        oracle_rdmr,
        global_config,
        path_config,
        used_leaves,
        _interest,
        _rates,
    ) = prepare_oracle_withdraw(
        snapshot,
        price_units=set(collateral_units),
        supply_token=snapshot.market_info.supply_token,
        revalue_alt_supply=False,
    )
    return oracle_rdmr, global_config, path_config, used_leaves


def _finalize_modify_redeemers(
    tx_builder: TransactionBuilder,
    snapshot: ModifyCollateralSnapshot,
    *,
    modify: ModifyCollaterals,
    loan_out: TransactionOutput,
) -> None:
    """Fill the ModifyCollaterals index fields from the FINAL (canonical) ordering.

    ``loan_out_idx`` is the order-preserving output position; the three reference
    indices (``protocol_cfg_ref_idx`` / ``market_ref_idx`` / ``pool_ref_idx``) come
    from the canonical out-ref sort the Plutus script context exposes, so this must run
    after every reference input is added.
    """
    ref_index = _ref_index(tx_builder)
    modify.loan_out_idx = tx_builder.outputs.index(loan_out)
    if snapshot.protocol_config.out_ref is not None:
        modify.protocol_cfg_ref_idx = ref_index[snapshot.protocol_config.out_ref]
    if snapshot.market.out_ref is not None:
        modify.market_ref_idx = ref_index[snapshot.market.out_ref]
    if snapshot.pool.out_ref is not None:
        modify.pool_ref_idx = ref_index[snapshot.pool.out_ref]


def build_modify_collateral(
    tx_builder: TransactionBuilder,
    *,
    snapshot: ModifyCollateralSnapshot,
    target_collateral: dict[str, int],
    txn_time: int | None = None,
    oracle_redeemer: OraclePriceCalcRdmr | None = None,
) -> None:
    """Contribute a forward modify-collateral (ModifyCollaterals) tx to `tx_builder`.

    Wires: the loan script spend (under the loan reference script) carrying the
    ``ModifyCollaterals`` redeemer; the protocol-config / market / pool / oracle
    reference inputs (the pool is a REFERENCE input read for the interest index, NEVER
    spent); the oracle withdraw-zero price calc (the only withdrawal -- no hub); and
    the continuing loan output (idx0) reusing the input loan datum VERBATIM with the
    TARGET collateral locked. A modify mints nothing, spends no pool, and emits no fee
    output. All protocol-derived data (oracle prices, redeemer indices) is synthesized
    from `snapshot`; the caller funds the borrower input, balances, evaluates, submits.

    ``target_collateral`` is the absolute collateral the loan output should lock
    (``unit -> quantity``): added units rise, removed units drop. The oracle
    ``Withdraw`` prices the loan output's TARGET collateral set, and the caller should
    preflight the health factor with `safe_collateral_for_modify`.

    Args:
        tx_builder: The caller's builder to mutate.
        snapshot: Resolved live building blocks for the loan being modified.
        target_collateral: Target absolute collateral amounts for the loan output.
        txn_time: POSIX milliseconds; defaults from the validity lower bound.
        oracle_redeemer: An optional pre-resolved oracle ``Withdraw`` redeemer to drive
            the price calc instead of forward-synthesizing it (e.g. to replay a captured
            on-chain redeemer); the builder then references the FULL resolved oracle
            reference set so the supplied redeemer's reference indices line up. Leave
            ``None`` to forward-synthesize from live source leaves -- the default, which
            reproduces the price calc for every Danogo source kind in use.
    """
    loan = snapshot.loan
    if loan.out_ref is None or loan.datum is None or loan.address is None:
        raise ValueError("snapshot loan UTxO is missing its out-ref/datum/address")
    if snapshot.pool.out_ref is None:
        raise ValueError("snapshot pool UTxO is missing its out-ref")

    _set_validity_window(tx_builder, slots=_MODIFY_VALIDITY_SLOTS, txn_time=txn_time)

    loan_skh = snapshot.loan_skh
    market_name = snapshot.market_name

    # One ModifyCollaterals redeemer rides the loan Spend only (the pool is referenced,
    # not spent). Indices are placeholders until the final ordering is known.
    modify = ModifyCollaterals(
        loan_out_idx=0,
        protocol_cfg_ref_idx=0,
        market_ref_idx=0,
        pool_ref_idx=0,
    )

    # --- loan script spend (reference script via add_script_input) -----------------
    tx_builder.add_script_input(
        _to_utxo(loan),
        script=_to_utxo(snapshot.loan_mint_script_ref),
        redeemer=Redeemer(modify),
    )

    # --- config / market / pool / oracle reference inputs (read-only) --------------
    tx_builder.reference_inputs.add(_to_utxo(snapshot.protocol_config))
    tx_builder.reference_inputs.add(_to_utxo(snapshot.market))
    tx_builder.reference_inputs.add(_to_utxo(snapshot.pool))

    oracle_rdmr: OraclePriceCalcRdmr
    forward_oracle: tuple[Utxo, Utxo, list[tuple[Utxo, OracleUtxoType]]] | None = None
    if oracle_redeemer is None:
        collateral_units = _post_modification_units(snapshot, target_collateral)
        oracle_rdmr, global_config, path_config, used_leaves = _modify_oracle_prep(
            snapshot,
            collateral_units=collateral_units,
        )
        forward_oracle = (global_config, path_config, used_leaves)
        tx_builder.reference_inputs.add(_to_utxo(global_config))
        tx_builder.reference_inputs.add(_to_utxo(path_config))
        for leaf, _otype in used_leaves:
            tx_builder.reference_inputs.add(_to_utxo(leaf))
    else:
        oracle_rdmr = oracle_redeemer
        # Replay drives the supplied oracle redeemer verbatim, whose reference indices
        # are computed against the full on-chain oracle reference set, so reference the
        # whole resolved set (configs + every source leaf) to reproduce that ordering.
        for ref in snapshot.oracle_data_refs:
            tx_builder.reference_inputs.add(_to_utxo(ref))
        for leaf in snapshot.oracle_source_leaves:
            tx_builder.reference_inputs.add(_to_utxo(leaf))

    # --- oracle withdraw-zero price calc (the only withdrawal -- no hub) ------------
    attach_oracle_withdraw(tx_builder, snapshot, oracle_rdmr=oracle_rdmr)

    # --- output: the continuing loan, same datum + TARGET collateral (idx0) --------
    loan_out = TransactionOutput(
        address=Address.decode(loan.address),
        amount=_loan_output_value(
            loan,
            loan_skh=loan_skh,
            market_name=market_name,
            collateral=target_collateral,
        ),
        datum=snapshot.loan_datum,
    )
    loan_out.amount.coin = max(
        loan_out.amount.coin,
        OUTPUT_MIN_ADA,
        min_lovelace(tx_builder.context, output=loan_out),
    )
    tx_builder.add_output(loan_out)

    # --- resolve role indices from the FINAL builder ordering ----------------------
    _finalize_modify_redeemers(tx_builder, snapshot, modify=modify, loan_out=loan_out)
    if forward_oracle is not None:
        global_config, path_config, used_leaves = forward_oracle
        # The structured deployment references its priced leaves in path-walk order;
        # the packed deployment sorts them by reference-input index.
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


def add_modify_collateral_funding(
    tx_builder: TransactionBuilder,
    *,
    snapshot: ModifyCollateralSnapshot,
    actor: Address,
    actor_utxo: str,
    target_collateral: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Add the borrower (funding) input for a modify-collateral.

    The borrower proves loan ownership with the owner NFT (qty 1) and funds any ADDED
    collateral -- the positive per-unit deltas of the target over the loan input's
    current holdings. REMOVED collateral (negative deltas) returns to the borrower via
    balancing and needs no explicit funding; no supply token is involved (a modify
    does not repay). The input's value is resolved live by Ogmios, so a nominal output
    value is enough here (manual assembly serializes only the out-ref).

    Returns the Ogmios `additionalUtxo` entry for the borrower input: every other
    Danogo input/reference is a freshly resolved live UTxO Ogmios resolves from its own
    ledger, so only this funding input -- a plain, datum-less wallet UTxO that may
    already be spent -- must be supplied explicitly.
    """
    funding: dict[str, int] = {snapshot.owner_nft: 1}
    if target_collateral:
        current = _loan_collateral_holdings(
            snapshot.loan,
            loan_skh=snapshot.loan_skh,
            market_name=snapshot.market_name,
        )
        for unit, target_qty in target_collateral.items():
            added = target_qty - current.get(unit, 0)
            if added > 0:
                funding[unit] = funding.get(unit, 0) + added

    return add_actor_funding(
        tx_builder,
        actor=actor,
        actor_utxo=actor_utxo,
        funding=funding,
    )


def safe_collateral_for_modify(
    snapshot: ModifyCollateralSnapshot,
    *,
    target_collateral: dict[str, int],
    txn_time: int | None = None,
) -> int:
    """Preflight the post-modification health factor for a modify-collateral.

    A modify leaves the loan debt UNCHANGED, so the on-chain ModifyCollaterals
    validator requires the threshold-weighted value of the TARGET collateral to
    strictly exceed the loan's CURRENT accrued debt -- ``current_loan_amount`` with the
    interest index advanced from the POOL REFERENCE datum to ``txn_time``. This needs
    more headroom than repay's preflight (which compares against the reduced
    ``loan_out_amount``), because the debt is not paid down here. It fails loud rather
    than surface a cryptic Ogmios script error.

    Each TARGET collateral unit is priced via the live oracle recipes
    (`_forward_prices_and_leaves`) and weighted by the market liquidation threshold
    (bps). A unit with no resolvable oracle price (or not accepted by the market) is a
    hard error -- it would misprice the loan.

    Returns the threshold-weighted collateral value (the headroom the target affords).
    """
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

    health_value = collateral_health_value(snapshot, target_collateral)
    if health_value <= loan_debt:
        raise ValueError(
            f"post-modification collateral value {health_value} does not exceed the "
            f"loan's current debt {loan_debt}: the loan would be under-collateralized",
        )
    return health_value
