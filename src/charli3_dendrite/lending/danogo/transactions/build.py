"""Assemble a forward Danogo create-loan transaction into a `TransactionBuilder`.

`build_create_loan` contributes the create-loan components to a caller-supplied
`pycardano.TransactionBuilder`, mirroring the DEX seam (`add_script_input`,
`reference_inputs.add`, `add_minting_script`, `add_withdrawal_script`, `add_output`,
validity). The caller owns the chain context, balancing, and evaluation.

Unlike the replay harness in `create_loan.py` (which copies the captured loan datum
and embeds the captured oracle redeemer), this synthesizes every protocol-derived
piece from live state: the post-borrow pool datum and new loan datum
(`datum_synth`), the loan-token + owner-NFT mint, and the oracle price-calc redeemer
(`oracle_synth`, with collateral prices recomputed from the live source leaves via
the packaged recipes). The `CreateLoan` redeemer's role indices and the oracle
redeemer's reference-input indices are derived from the FINAL builder ordering, not
hard-coded.

The deposit/withdraw, repay, and increase-loan builders live in `topup_withdraw.py`,
`repay.py`, and `increase_loan.py`; this module re-exports `build_topup_withdraw`,
`build_repay`/`add_repay_funding`, and `build_increase_loan`/`add_increase_funding`/
`safe_increase_amount` so the historical `transactions.build` import surface keeps
resolving.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Any

from pycardano import Address
from pycardano import Network
from pycardano import Redeemer
from pycardano import ScriptHash
from pycardano import TransactionBuilder
from pycardano import TransactionOutput
from pycardano import Value
from pycardano import Withdrawals
from pycardano import min_lovelace

from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.lending.danogo.datums import PoolDatum
from charli3_dendrite.lending.danogo.datums import PRational
from charli3_dendrite.lending.danogo.math import total_collateral_val_with_threshold
from charli3_dendrite.lending.danogo.oracles.redeemer import OraclePriceCalcRdmr
from charli3_dendrite.lending.danogo.oracles.redeemer import OracleUtxoType
from charli3_dendrite.lending.danogo.transactions._common import _alt_supply_update
from charli3_dendrite.lending.danogo.transactions._common import (
    _forward_prices_and_leaves,
)
from charli3_dendrite.lending.danogo.transactions._common import _market_fee_address
from charli3_dendrite.lending.danogo.transactions._common import _pool_output_value
from charli3_dendrite.lending.danogo.transactions._common import _ref_index
from charli3_dendrite.lending.danogo.transactions._common import _select_global_config
from charli3_dendrite.lending.danogo.transactions._common import _select_path_config
from charli3_dendrite.lending.danogo.transactions._common import _set_validity_window
from charli3_dendrite.lending.danogo.transactions._common import _synth_oracle_redeemer
from charli3_dendrite.lending.danogo.transactions._common import _to_utxo
from charli3_dendrite.lending.danogo.transactions._common import add_actor_funding
from charli3_dendrite.lending.danogo.transactions.context import CreateLoanSnapshot
from charli3_dendrite.lending.danogo.transactions.datum_synth import synth_loan_datum
from charli3_dendrite.lending.danogo.transactions.datum_synth import (
    synth_pool_datum_create_loan,
)
from charli3_dendrite.lending.danogo.transactions.increase_loan import (
    add_increase_funding,
)
from charli3_dendrite.lending.danogo.transactions.increase_loan import (
    build_increase_loan,
)
from charli3_dendrite.lending.danogo.transactions.increase_loan import (
    safe_increase_amount,
)
from charli3_dendrite.lending.danogo.transactions.loan_ids import owner_nft_name
from charli3_dendrite.lending.danogo.transactions.modify_collateral import (
    add_modify_collateral_funding,
)
from charli3_dendrite.lending.danogo.transactions.modify_collateral import (
    build_modify_collateral,
)
from charli3_dendrite.lending.danogo.transactions.modify_collateral import (
    safe_collateral_for_modify,
)
from charli3_dendrite.lending.danogo.transactions.redeemers import CreateLoan
from charli3_dendrite.lending.danogo.transactions.redeemers import NoneVal
from charli3_dendrite.lending.danogo.transactions.redeemers import OptionInt
from charli3_dendrite.lending.danogo.transactions.redeemers import OutputReference
from charli3_dendrite.lending.danogo.transactions.redeemers import SomeInt
from charli3_dendrite.lending.danogo.transactions.repay import add_repay_funding
from charli3_dendrite.lending.danogo.transactions.repay import build_repay
from charli3_dendrite.lending.danogo.transactions.topup_withdraw import (
    build_topup_withdraw,
)
from charli3_dendrite.lending.math import bps_mul_ceil
from charli3_dendrite.lending.math import floor_div
from charli3_dendrite.lending.transactions.infra import OUTPUT_MIN_ADA
from charli3_dendrite.utility import asset_to_value

if TYPE_CHECKING:
    from charli3_dendrite.backend.backend_base import AbstractBackend
    from charli3_dendrite.lending.danogo.datums import LoanDatum
    from charli3_dendrite.lending.danogo.transactions.context import Utxo

__all__ = [
    "add_actor_funding",
    "add_borrower_funding",
    "add_increase_funding",
    "add_modify_collateral_funding",
    "add_repay_funding",
    "build_and_evaluate",
    "build_create_loan",
    "build_increase_loan",
    "build_modify_collateral",
    "build_repay",
    "build_topup_withdraw",
    "safe_borrow_amount",
    "safe_collateral_for_modify",
    "safe_increase_amount",
]

# The create-loan validator caps the validity window at 360_000 ms (360 slots).
_CREATE_LOAN_VALIDITY_SLOTS = 360


def _split_unit(unit: str) -> tuple[bytes, bytes]:
    """Dendrite unit -> (policy, name) bytes; lovelace is (b"", b"")."""
    if unit == "lovelace":
        return b"", b""
    return bytes.fromhex(unit[:56]), bytes.fromhex(unit[56:])


def _origination_fee(snapshot: CreateLoanSnapshot, *, loan_amount: int) -> int:
    """Origination fee (lovelace) paid to the treasury for this loan.

    Mirrors the pool datum's origination-fee accrual,
    ``ceil(loan_amount * loan_origination_fee_rate / BASIS)`` -- 0 for markets
    whose origination rate is 0. The fee output is floored to min-ADA by the
    caller.
    """
    rate = snapshot.market_info.loan_origination_fee_rate
    return bps_mul_ceil(loan_amount, rate)


def _build_fee_output(
    tx_builder: TransactionBuilder,
    snapshot: CreateLoanSnapshot,
    *,
    loan_amount: int,
) -> TransactionOutput:
    """The origination-fee output paid to the market's treasury address.

    The create-loan validators require a fee payment to the market's
    fee-recipient address (the redeemer's ``fee_out_idx`` points at it); its
    lovelace is the origination fee floored to the output's min-ADA.
    """
    if snapshot.market.datum is None:
        raise ValueError("snapshot market UTxO is missing its datum")
    fee_addr = _market_fee_address(snapshot.market.datum)
    fee_amount = _origination_fee(snapshot, loan_amount=loan_amount)
    fee_out = TransactionOutput(fee_addr, Value(0))
    fee_out.amount.coin = max(
        fee_amount,
        min_lovelace(tx_builder.context, output=fee_out),
    )
    return fee_out


def _synth_datums(
    snapshot: CreateLoanSnapshot,
    *,
    loan_amount: int,
    txn_time: int,
    alt_tokens_interest: int,
    new_alt_rates: list[PRational] | None,
) -> tuple[PoolDatum, LoanDatum, str]:
    """Synthesize the post-borrow pool datum + new loan datum from live state.

    Returns the advanced pool datum, the new loan datum, and the owner-NFT asset
    name. The origination fee is 0 for current markets (rate + minimum both 0), so
    the loan amount equals the borrow amount. ``alt_tokens_interest`` /
    ``new_alt_rates`` carry the alt-supply revaluation (see `_alt_supply_update`).
    """
    market = snapshot.market_info
    if snapshot.pool.datum is None or snapshot.pool.out_ref is None:
        raise ValueError("snapshot pool UTxO is missing its datum/out-ref")
    new_pool_datum = synth_pool_datum_create_loan(
        PoolDatum.from_cbor(snapshot.pool.datum),
        loan_amount=loan_amount,
        txn_time=txn_time,
        power_base=market.power_base,
        base_rate=market.base_rate,
        loan_fee_rate=market.loan_fee_rate,
        loan_origination_fee_rate=market.loan_origination_fee_rate,
        alt_tokens_interest=alt_tokens_interest,
        new_alt_supply_tokens_rate=new_alt_rates,
    )
    owner_name = owner_nft_name(snapshot.pool.out_ref)
    supply_policy, supply_name = _split_unit(market.supply_token)
    loan_datum = synth_loan_datum(
        owner_policy=bytes.fromhex(snapshot.loan_skh),
        owner_name=bytes.fromhex(owner_name),
        token_policy=supply_policy,
        token_name=supply_name,
        loan_amount=loan_amount,
        initial_interest_index=new_pool_datum.interest_index,
    )
    return new_pool_datum, loan_datum, owner_name


def _finalize_redeemers(
    tx_builder: TransactionBuilder,
    snapshot: CreateLoanSnapshot,
    *,
    create_loan: CreateLoan,
    oracle_rdmr: OraclePriceCalcRdmr,
    pool_out: TransactionOutput,
    loan_out: TransactionOutput,
    fee_out: TransactionOutput,
    used_leaves: list[tuple[Utxo, OracleUtxoType]],
    prices: dict[str, dict[str, tuple[int, int]]],
    global_config: Utxo,
    path_config: Utxo,
) -> None:
    """Fill the redeemer role indices from the FINAL (canonical) builder ordering.

    Output indices come from the (order-preserving) output list; reference-input
    indices from the canonical out-ref sort the Plutus script context exposes. The
    `create_loan` data is shared by the spend + mint Redeemer wrappers, so mutating it
    here updates both.
    """
    ref_index = _ref_index(tx_builder)
    create_loan.pool_out_idx = tx_builder.outputs.index(pool_out)
    create_loan.loan_out_idx = tx_builder.outputs.index(loan_out)
    create_loan.fee_out_idx = SomeInt(tx_builder.outputs.index(fee_out))
    if snapshot.protocol_config.out_ref is not None:
        create_loan.protocol_cfg_ref_idx = ref_index[snapshot.protocol_config.out_ref]
    if snapshot.market.out_ref is not None:
        create_loan.market_ref_idx = ref_index[snapshot.market.out_ref]

    _synth_oracle_redeemer(
        tx_builder,
        oracle_rdmr=oracle_rdmr,
        used_leaves=used_leaves,
        prices=prices,
        global_config=global_config,
        path_config=path_config,
    )


def build_create_loan(
    tx_builder: TransactionBuilder,
    *,
    snapshot: CreateLoanSnapshot,
    actor_address: Address | str,
    collateral: dict[str, int],
    borrow_amount: int,
    txn_time: int | None = None,
) -> None:
    """Contribute a forward create-loan transaction to `tx_builder`.

    Wires (mirroring the DEX builders): the pool script spend (reference-script via
    `add_script_input`), the config/market/oracle reference inputs, the loan-token +
    owner-NFT mint, the oracle withdraw-zero redeemer, and the updated-pool + loan
    outputs, plus the validity range. All protocol-derived data (pool/loan datums,
    oracle prices, redeemer indices) is synthesized from `snapshot`; the caller funds
    the collateral inputs, balances against `actor_address`, evaluates, and submits.

    Args:
        tx_builder: The caller's builder to mutate.
        snapshot: Resolved live building blocks for the market.
        actor_address: The borrower; its stake part is bound to the loan UTxO and it
            receives the owner NFT + change during balancing.
        collateral: `unit -> quantity` locked into the loan UTxO.
        borrow_amount: Amount of the supply token to borrow.
        txn_time: POSIX milliseconds for datum synthesis; defaults from the validity
            lower bound (the chain context's current slot).
    """
    if isinstance(actor_address, str):
        actor_address = Address.decode(actor_address)
    pool = snapshot.pool
    if pool.out_ref is None or pool.datum is None or pool.address is None:
        raise ValueError("snapshot pool UTxO is missing its out-ref/datum/address")

    txn_time = _set_validity_window(
        tx_builder,
        slots=_CREATE_LOAN_VALIDITY_SLOTS,
        txn_time=txn_time,
    )

    supply_token = snapshot.market_info.supply_token
    loan_skh = snapshot.loan_skh
    market_name = snapshot.market_name

    # Price (and reference leaves for) the loan's borrowed collateral AND the market's
    # alt-supply tokens. The create-loan validators value the pool with the alt-supply
    # prices, and the oracle Withdraw verifies every priced asset against its leaves --
    # so both must be priced via their recipes (the captured tx prices exactly this set:
    # the collateral + the one alt-supply token). Anything beyond this set is the
    # over-wiring the oracle Withdraw validator rejects.
    price_units = set(collateral) | set(snapshot.market_info.alt_supply_tokens)
    prices, used_leaves = _forward_prices_and_leaves(snapshot, price_units)
    global_config = _select_global_config(snapshot)
    path_config = _select_path_config(
        snapshot,
        supply_token=supply_token,
        used_leaves=used_leaves,
    )

    # Forward datums: advance the pool and synthesize the new loan datum. The
    # alt-supply revaluation (rate + booked interest) derives from the freshly
    # priced alt tokens.
    # TODO(market): `loan_amount` equals `borrow_amount` while the origination fee is
    # 0 for current markets (rate == 0) and the market parser does not surface the
    # origination-fee minimum (see the matching TODO(market) in `datum_synth.py`). The
    # fee is recomputed inside `synth_pool_datum_create_loan`; generalize here once that
    # minimum is wired through `market.py`.
    loan_amount = borrow_amount
    alt_tokens_interest, new_alt_rates = _alt_supply_update(snapshot, prices)
    new_pool_datum, loan_datum, owner_name = _synth_datums(
        snapshot,
        loan_amount=loan_amount,
        txn_time=txn_time,
        alt_tokens_interest=alt_tokens_interest,
        new_alt_rates=new_alt_rates,
    )

    # The same CreateLoan redeemer drives both the pool spend and the loan mint (the
    # spend is delegated to the mint). Indices are placeholders until the final
    # ordering is known; the data object is shared by both Redeemer wrappers so the
    # in-place fixups below propagate to both.
    fee_out_idx: OptionInt = NoneVal()
    create_loan = CreateLoan(
        pool_out_idx=0,
        loan_out_idx=0,
        fee_out_idx=fee_out_idx,
        protocol_cfg_ref_idx=0,
        market_ref_idx=0,
        pool_in_out_ref=OutputReference(
            transaction_id=bytes.fromhex(pool.out_ref[0]),
            output_index=pool.out_ref[1],
        ),
    )
    oracle_rdmr = OraclePriceCalcRdmr(
        oracle_source_idx=0,
        oracle_path_idxs=[],
        oracle_idxs=[],
        prices=prices,
        borrow_rates={},
    )

    # --- pool script spend (reference script attached via add_script_input) --------
    tx_builder.add_script_input(
        _to_utxo(pool),
        script=_to_utxo(snapshot.pool_script_ref),
        redeemer=Redeemer(create_loan),
    )

    # --- config / market / oracle reference inputs (read-only) --------------------
    # Only the pruned oracle set: the chosen routing-path + global source configs and
    # the borrowed collateral's source leaves (plus protocol config + market).
    tx_builder.reference_inputs.add(_to_utxo(snapshot.protocol_config))
    tx_builder.reference_inputs.add(_to_utxo(snapshot.market))
    tx_builder.reference_inputs.add(_to_utxo(global_config))
    tx_builder.reference_inputs.add(_to_utxo(path_config))
    for leaf, _otype in used_leaves:
        tx_builder.reference_inputs.add(_to_utxo(leaf))

    # --- mint: 1 loan token (market NFT name) + 1 owner NFT (blake2b of pool ref) --
    tx_builder.add_minting_script(
        _to_utxo(snapshot.loan_mint_script_ref),
        redeemer=Redeemer(create_loan),
    )
    mint_assets = Assets(
        **{loan_skh + market_name: 1, loan_skh + owner_name: 1},
    )
    mint_multi = asset_to_value(mint_assets).multi_asset
    tx_builder.mint = (
        mint_multi if tx_builder.mint is None else tx_builder.mint + mint_multi
    )

    # --- oracle withdraw-zero trick -----------------------------------------------
    tx_builder.add_withdrawal_script(
        _to_utxo(snapshot.oracle_script_ref),
        Redeemer(oracle_rdmr),
    )
    reward_addr = Address(
        staking_part=ScriptHash(bytes.fromhex(snapshot.oracle_skh)),
        network=Network.MAINNET,
    )
    tx_builder.withdrawals = Withdrawals({bytes(reward_addr): 0})

    # --- outputs: updated pool, then the loan UTxO --------------------------------
    pool_out = TransactionOutput(
        address=Address.decode(pool.address),
        amount=_pool_output_value(pool, supply_token, -borrow_amount),
        datum=new_pool_datum,
    )
    tx_builder.add_output(pool_out)

    loan_addr = Address(
        payment_part=ScriptHash(bytes.fromhex(loan_skh)),
        staking_part=actor_address.staking_part,
        network=Network.MAINNET,
    )
    loan_assets: dict[str, int] = {loan_skh + market_name: 1}
    for unit, qty in collateral.items():
        loan_assets[unit] = loan_assets.get(unit, 0) + qty
    loan_out = TransactionOutput(
        address=loan_addr,
        amount=asset_to_value(Assets(**loan_assets)),
        datum=loan_datum,
    )
    # Top up to the min-ADA floor without discarding any deposited lovelace collateral.
    loan_out.amount.coin = max(
        loan_out.amount.coin,
        OUTPUT_MIN_ADA,
        min_lovelace(tx_builder.context, output=loan_out),
    )
    tx_builder.add_output(loan_out)

    # --- protocol fee output (treasury) -------------------------------------------
    fee_out = _build_fee_output(tx_builder, snapshot, loan_amount=loan_amount)
    tx_builder.add_output(fee_out)

    # --- resolve role indices from the FINAL builder ordering ---------------------
    _finalize_redeemers(
        tx_builder,
        snapshot,
        create_loan=create_loan,
        oracle_rdmr=oracle_rdmr,
        pool_out=pool_out,
        loan_out=loan_out,
        fee_out=fee_out,
        used_leaves=used_leaves,
        prices=prices,
        global_config=global_config,
        path_config=path_config,
    )


def safe_borrow_amount(
    snapshot: CreateLoanSnapshot,
    collateral: dict[str, int],
    borrow_amount: int | None,
) -> int:
    """Derive a borrow amount the loan health check accepts, from live prices.

    The loan validator requires the threshold-weighted collateral value to strictly
    exceed the loan amount. Each locked collateral is priced via the live oracle
    recipes (`_forward_prices_and_leaves`) and weighted by the market liquidation
    threshold (bps), giving ``max_borrow`` (== ``total_collateral_val_with_threshold``).
    A target of ``floor(max_borrow / 2)`` leaves ample headroom for either flooring
    convention; an explicit hint is clamped to that target. The result is pinned to
    ``[min_tx_amount, max_borrow - 1]`` so it stays borrowable and strictly healthy.
    """
    market = snapshot.market_info
    quote = market.supply_token
    prices, _ = _forward_prices_and_leaves(snapshot, set(collateral))
    quote_prices = prices.get(quote, {})

    terms: list[tuple[int, int, int, int]] = []
    for unit, qty in collateral.items():
        price = quote_prices.get(unit)
        if price is None:
            raise ValueError(
                f"no live oracle price for collateral {unit!r} (quote {quote!r})",
            )
        threshold = market.threshold_for(unit)
        if threshold <= 0:
            raise ValueError(f"collateral {unit!r} is not accepted by the market")
        num, denom = price
        terms.append((qty, num, denom, threshold))

    max_borrow = total_collateral_val_with_threshold(terms)
    if max_borrow <= market.min_tx_amount:
        raise ValueError(
            f"collateral supports at most {max_borrow}, which cannot cover the market "
            f"minimum tx amount {market.min_tx_amount}",
        )

    target = floor_div(max_borrow, 2)
    candidate = target if borrow_amount is None else min(borrow_amount, target)
    candidate = max(candidate, market.min_tx_amount)
    return min(candidate, max_borrow - 1)


def add_borrower_funding(
    tx_builder: TransactionBuilder,
    *,
    snapshot: CreateLoanSnapshot,
    actor: Address,
    actor_utxo: str,
    collateral: dict[str, int],
) -> dict[str, Any]:
    """Add the borrower funding input + owner-NFT change output + placeholder fee.

    The borrower funds the loan and receives the minted owner NFT (its
    proof-of-ownership token). Its value is resolved live by Ogmios, so a nominal
    output value is enough here (manual assembly serializes only the out-ref).

    Returns the Ogmios `additionalUtxo` entry for the borrower input: every other
    Danogo input/reference is a freshly resolved live UTxO Ogmios resolves from its
    own ledger, so only this funding input -- a plain wallet UTxO that may already be
    spent -- must be supplied explicitly.

    Borrowing is `add_actor_funding` (the funding token here is the collateral) plus
    the minted owner NFT, which the borrower receives in a dedicated change output.
    """
    entry = add_actor_funding(
        tx_builder,
        actor=actor,
        actor_utxo=actor_utxo,
        funding=collateral,
    )

    if snapshot.pool.out_ref is None:
        raise ValueError("snapshot pool UTxO is missing its out-ref")
    owner_name = owner_nft_name(snapshot.pool.out_ref)
    change = TransactionOutput(
        actor,
        asset_to_value(
            Assets(**{"lovelace": OUTPUT_MIN_ADA, snapshot.loan_skh + owner_name: 1}),
        ),
    )
    tx_builder.add_output(change)

    return entry


def build_and_evaluate(
    backend: AbstractBackend,
    *,
    market_name: str,
    actor_address: str,
    actor_utxo: str,
    collateral: dict[str, int],
    borrow_amount: int | None = None,
) -> list[dict[str, Any]]:
    """Build a forward create-loan tx from live state and evaluate it on Ogmios.

    Thin compatibility wrapper over the cross-protocol lending seam: it packs its
    arguments into `ActionParams` and drives `DanogoTxBuilder.build_and_evaluate` for
    the `BORROW` action (resolve snapshot -> contribute create-loan + borrower funding
    -> assemble unsigned -> Ogmios `evaluateTransaction`). Returns Ogmios's
    per-redeemer execution budgets. Nothing is signed or submitted.

    `DanogoTxBuilder` is imported lazily to avoid the `build` <-> `builder` import
    cycle (`builder` imports the create-loan build helpers from this module).
    """
    from charli3_dendrite.lending.danogo.transactions.builder import DanogoTxBuilder
    from charli3_dendrite.lending.transactions.base import ActionParams
    from charli3_dendrite.lending.transactions.base import LendingAction

    return DanogoTxBuilder().build_and_evaluate(
        backend=backend,
        market_name=market_name,
        action=LendingAction.BORROW,
        params=ActionParams(
            actor_address=actor_address,
            actor_utxo=actor_utxo,
            collateral=collateral,
            borrow_amount=borrow_amount,
        ),
    )
