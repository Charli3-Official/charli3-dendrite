"""Assemble a forward Danogo deposit (top-up) / withdrawal into a `TransactionBuilder`.

`build_topup_withdraw` contributes the supply-side pool action to a caller-supplied
`pycardano.TransactionBuilder`, mirroring `build_create_loan`: the pool script spend,
the protocol-config + market reference inputs, the dToken mint/burn, the updated-pool
+ actor outputs, and (for alt-supply markets) the oracle withdraw-zero redeemer. The
direction is carried by a signed supply delta; the caller owns balancing/evaluation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pycardano import Address
from pycardano import Asset
from pycardano import AssetName
from pycardano import IndefiniteList
from pycardano import MultiAsset
from pycardano import Redeemer
from pycardano import ScriptHash
from pycardano import TransactionBuilder
from pycardano import TransactionOutput
from pycardano import min_lovelace

from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.lending.danogo.datums import PoolDatum
from charli3_dendrite.lending.danogo.oracles.redeemer import OraclePriceCalcRdmr
from charli3_dendrite.lending.danogo.oracles.redeemer import OracleUtxoType
from charli3_dendrite.lending.danogo.transactions._common import _ref_index
from charli3_dendrite.lending.danogo.transactions._common import _set_validity_window
from charli3_dendrite.lending.danogo.transactions._common import _synth_oracle_redeemer
from charli3_dendrite.lending.danogo.transactions._common import _to_utxo
from charli3_dendrite.lending.danogo.transactions._common import attach_oracle_withdraw
from charli3_dendrite.lending.danogo.transactions._common import (
    build_pool_script_output,
)
from charli3_dendrite.lending.danogo.transactions._common import prepare_oracle_withdraw
from charli3_dendrite.lending.danogo.transactions.context import TopupWithdrawSnapshot
from charli3_dendrite.lending.danogo.transactions.datum_synth import (
    synth_pool_datum_topup_withdraw,
)
from charli3_dendrite.lending.danogo.transactions.redeemers import NoneVal
from charli3_dendrite.lending.danogo.transactions.redeemers import PoolMarketIndexer
from charli3_dendrite.lending.danogo.transactions.redeemers import TopupWithdraw
from charli3_dendrite.lending.transactions.infra import OUTPUT_MIN_ADA
from charli3_dendrite.utility import asset_to_value

if TYPE_CHECKING:
    from charli3_dendrite.lending.danogo.datums import PRational
    from charli3_dendrite.lending.danogo.market import DanogoMarket
    from charli3_dendrite.lending.danogo.transactions.context import Utxo

# The TopupWithdraw validator applies the same 360_000 ms (360 slots) validity cap.
_TOPUP_WITHDRAW_VALIDITY_SLOTS = 360


def _dtoken_mint(pool_skh: str, market_name: str, amount: int) -> MultiAsset:
    """Mint/burn `amount` dTokens: policy = `pool_skh`, name = `market_name`.

    A positive `amount` mints (deposit), a negative one burns (withdraw); the
    `MultiAsset` is built directly so the signed quantity is preserved.
    """
    return MultiAsset(
        {
            ScriptHash(bytes.fromhex(pool_skh)): Asset(
                {AssetName(bytes.fromhex(market_name)): amount},
            ),
        },
    )


def _topup_dtoken_and_datum(
    pool: Utxo,
    market: DanogoMarket,
    *,
    supply_change: int,
    withdraw_fee: int,
    txn_time: int,
    alt_tokens_interest: int = 0,
    new_alt_rates: list[PRational] | None = None,
) -> tuple[int, PoolDatum]:
    """The dToken mint/burn amount + advanced pool datum for a deposit/withdraw.

    Both come from a single `synth_pool_datum_topup_withdraw` call, so the amount
    minted/burned is exactly what the datum booked into ``circulating_dtoken`` -- the
    mint amount and the datum cannot drift apart. ``alt_tokens_interest`` /
    ``new_alt_rates`` carry the alt-supply revaluation (see `_alt_supply_update`); for
    a single-supply-token market they are 0 / None and the rate list is unchanged.
    """
    if pool.datum is None:
        raise ValueError("snapshot pool UTxO is missing its datum")
    new_pool_datum, minted_dtoken = synth_pool_datum_topup_withdraw(
        PoolDatum.from_cbor(pool.datum),
        pool_changed_amount=supply_change,
        withdraw_fee=withdraw_fee,
        txn_time=txn_time,
        power_base=market.power_base,
        base_rate=market.base_rate,
        loan_fee_rate=market.loan_fee_rate,
        alt_tokens_interest=alt_tokens_interest,
        new_alt_supply_tokens_rate=new_alt_rates,
    )
    return minted_dtoken, new_pool_datum


def _topup_actor_output(
    tx_builder: TransactionBuilder,
    actor_address: Address,
    *,
    pool_skh: str,
    market_name: str,
    supply_token: str,
    supply_change: int,
    withdraw_fee: int,
    minted_dtoken: int,
) -> TransactionOutput:
    """The actor's output: minted dTokens (deposit) or redeemed supply token (withdraw).

    A deposit hands the freshly minted dTokens to the actor; a withdraw returns the
    supply token drawn out of the pool, net of the withdrawal fee. Floored to the
    output's min-ADA.
    """
    if supply_change >= 0:
        actor_assets: dict[str, int] = {pool_skh + market_name: minted_dtoken}
    else:
        received = -supply_change - withdraw_fee
        key = "lovelace" if supply_token == "lovelace" else supply_token
        actor_assets = {key: received}
    actor_out = TransactionOutput(actor_address, asset_to_value(Assets(**actor_assets)))
    actor_out.amount.coin = max(
        actor_out.amount.coin,
        OUTPUT_MIN_ADA,
        min_lovelace(tx_builder.context, output=actor_out),
    )
    return actor_out


def _attach_topup_oracle(
    tx_builder: TransactionBuilder,
    snapshot: TopupWithdrawSnapshot,
    *,
    oracle_rdmr: OraclePriceCalcRdmr,
    global_config: Utxo,
    path_config: Utxo,
    used_leaves: list[tuple[Utxo, OracleUtxoType]],
) -> None:
    """Add the pruned oracle reference inputs and the oracle withdraw-zero trick.

    The reference set is the chosen global source + routing-path configs and the
    alt-supply tokens' source leaves; the oracle price-calc runs as a zero withdrawal
    against its reward address (same mechanism as create-loan).
    """
    if snapshot.oracle_script_ref is None:
        raise ValueError("alt-supply market is missing its oracle reference script")
    tx_builder.reference_inputs.add(_to_utxo(global_config))
    tx_builder.reference_inputs.add(_to_utxo(path_config))
    for leaf, _otype in used_leaves:
        tx_builder.reference_inputs.add(_to_utxo(leaf))
    attach_oracle_withdraw(tx_builder, snapshot, oracle_rdmr=oracle_rdmr)


def _topup_oracle_prep(
    snapshot: TopupWithdrawSnapshot,
    *,
    supply_token: str,
) -> tuple[
    OraclePriceCalcRdmr | None,
    Utxo | None,
    Utxo | None,
    list[tuple[Utxo, OracleUtxoType]],
    int,
    list[PRational] | None,
]:
    """Resolve the oracle Withdraw wiring for a deposit/withdraw.

    A single-supply-token market re-prices nothing, so this returns the no-op shape
    ``(None, None, None, [], 0, None)`` and the caller wires no oracle.

    An alt-supply market re-prices its alternative supply tokens from the oracle on
    every pool action and books the change as interest, exactly as create-loan does.
    The deposit/withdraw prices ONLY the alt-supply tokens (no collateral is locked),
    so the oracle Withdraw is pruned to that set (single global source + the routing
    path config + the alt tokens' leaves); anything more is the over-wiring the oracle
    validator rejects. Returns the seeded oracle redeemer (indices filled once the
    builder ordering is final), the chosen global + path configs, the priced leaves,
    and the alt revaluation (`alt_tokens_interest` + new per-token rates).
    """
    market = snapshot.market_info
    if not market.alt_supply_tokens:
        return None, None, None, [], 0, None
    if snapshot.oracle_script_ref is None:
        raise ValueError(
            "alt-supply market is missing its resolved oracle reference script",
        )
    # A deposit/withdraw revalues the pool's alt-supply holdings from the oracle on
    # every action and books the change as interest, exactly as create-loan does.
    return prepare_oracle_withdraw(
        snapshot,
        price_units=set(market.alt_supply_tokens),
        supply_token=supply_token,
        revalue_alt_supply=True,
    )


def build_topup_withdraw(
    tx_builder: TransactionBuilder,
    *,
    snapshot: TopupWithdrawSnapshot,
    actor_address: Address | str,
    supply_change: int,
    txn_time: int | None = None,
) -> None:
    """Contribute a deposit (top-up) or withdrawal to `tx_builder`.

    Wires (mirroring `build_create_loan`): the pool script spend (reference-script via
    `add_script_input`), the protocol-config + market reference inputs, the dToken
    mint/burn, and the updated-pool + actor outputs, plus the validity range. The pool
    datum and the dToken delta both come from one `synth_pool_datum_topup_withdraw`
    call, so the minted/burned amount agrees with the datum the validator recomputes.

    Args:
        tx_builder: The caller's builder to mutate.
        snapshot: Resolved live building blocks for the market.
        actor_address: The depositor/withdrawer; receives the minted dTokens (deposit)
            or the redeemed supply token (withdraw).
        supply_change: Signed supply-token change -- positive deposits into the pool,
            negative withdraws from it.
        txn_time: POSIX milliseconds for datum synthesis; defaults from the validity
            lower bound (the chain context's current slot).
    """
    market = snapshot.market_info

    # The pool spend + dToken mint validators reject a supply change below the
    # market's minimum transaction amount (abs(pool_changed_amount) >= min_tx_amount);
    # catch it here with a clear error rather than a cryptic Ogmios script failure.
    if abs(supply_change) < market.min_tx_amount:
        raise ValueError(
            f"supply change of {abs(supply_change)} (market {snapshot.market_name}) is "
            f"below the market minimum transaction amount {market.min_tx_amount}",
        )

    if isinstance(actor_address, str):
        actor_address = Address.decode(actor_address)
    pool = snapshot.pool
    if pool.out_ref is None or pool.datum is None or pool.address is None:
        raise ValueError("snapshot pool UTxO is missing its out-ref/datum/address")

    txn_time = _set_validity_window(
        tx_builder,
        slots=_TOPUP_WITHDRAW_VALIDITY_SLOTS,
        txn_time=txn_time,
    )

    supply_token = market.supply_token
    pool_skh = snapshot.pool_skh
    market_name = snapshot.market_name

    # No fee output is emitted (every captured TopupWithdraw carries fee_out_idx=None
    # and a zero withdrawal fee), so the fee never leaves the supply flow.
    withdraw_fee = 0

    (
        oracle_rdmr,
        global_config,
        path_config,
        used_leaves,
        alt_tokens_interest,
        new_alt_rates,
    ) = _topup_oracle_prep(snapshot, supply_token=supply_token)

    minted_dtoken, new_pool_datum = _topup_dtoken_and_datum(
        pool,
        market,
        supply_change=supply_change,
        withdraw_fee=withdraw_fee,
        txn_time=txn_time,
        alt_tokens_interest=alt_tokens_interest,
        new_alt_rates=new_alt_rates,
    )

    # The same TopupWithdraw redeemer drives both the pool spend and the dToken
    # mint/burn (they are byte-identical). Indices are placeholders until the final
    # ordering is known; the indexer is shared by both Redeemer wrappers so the
    # in-place fixups below propagate to both.
    indexer = PoolMarketIndexer(
        pool_out_idx=0,
        fee_out_idx=NoneVal(),
        market_ref_idx=0,
    )
    topup = TopupWithdraw(
        protocol_cfg_ref_idx=0,
        pools=IndefiniteList([indexer]),
    )

    # --- pool script spend (reference script attached via add_script_input) --------
    tx_builder.add_script_input(
        _to_utxo(pool),
        script=_to_utxo(snapshot.pool_script_ref),
        redeemer=Redeemer(topup),
    )

    # --- config / market reference inputs (read-only) -----------------------------
    tx_builder.reference_inputs.add(_to_utxo(snapshot.protocol_config))
    tx_builder.reference_inputs.add(_to_utxo(snapshot.market))

    # --- oracle reference inputs + withdraw-zero trick (alt-supply markets only) ---
    if oracle_rdmr is not None:
        assert global_config is not None  # noqa: S101 - set with oracle_rdmr
        assert path_config is not None  # noqa: S101 - set with oracle_rdmr
        _attach_topup_oracle(
            tx_builder,
            snapshot,
            oracle_rdmr=oracle_rdmr,
            global_config=global_config,
            path_config=path_config,
            used_leaves=used_leaves,
        )

    # --- mint/burn: +dTokens (deposit) or -dTokens (withdraw) ---------------------
    tx_builder.add_minting_script(
        _to_utxo(snapshot.pool_script_ref),
        redeemer=Redeemer(topup),
    )
    mint_multi = _dtoken_mint(pool_skh, market_name, minted_dtoken)
    tx_builder.mint = (
        mint_multi if tx_builder.mint is None else tx_builder.mint + mint_multi
    )

    # --- outputs: updated pool, then the actor output -----------------------------
    pool_out = build_pool_script_output(
        pool,
        supply_token=supply_token,
        supply_delta=supply_change,
        datum=new_pool_datum,
    )
    tx_builder.add_output(pool_out)

    actor_out = _topup_actor_output(
        tx_builder,
        actor_address,
        pool_skh=pool_skh,
        market_name=market_name,
        supply_token=supply_token,
        supply_change=supply_change,
        withdraw_fee=withdraw_fee,
        minted_dtoken=minted_dtoken,
    )
    tx_builder.add_output(actor_out)

    # --- resolve role indices from the FINAL builder ordering ---------------------
    ref_index = _ref_index(tx_builder)
    indexer.pool_out_idx = tx_builder.outputs.index(pool_out)
    indexer.fee_out_idx = NoneVal()
    if snapshot.market.out_ref is not None:
        indexer.market_ref_idx = ref_index[snapshot.market.out_ref]
    if snapshot.protocol_config.out_ref is not None:
        topup.protocol_cfg_ref_idx = ref_index[snapshot.protocol_config.out_ref]

    if oracle_rdmr is not None:
        assert global_config is not None  # noqa: S101 - set with oracle_rdmr
        assert path_config is not None  # noqa: S101 - set with oracle_rdmr
        _synth_oracle_redeemer(
            tx_builder,
            oracle_rdmr=oracle_rdmr,
            used_leaves=used_leaves,
            prices=oracle_rdmr.prices,
            global_config=global_config,
            path_config=path_config,
        )
