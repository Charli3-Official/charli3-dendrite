"""Composable pieces of a Liqwid V2 transaction.

Each leg adds to a caller's pycardano ``TransactionBuilder``: spends, mints and
burns, withdrawals, signers and reference inputs merge with those already there
(an out-ref, a policy or a reward account counts once) and no leg writes a
validity window. Every Liqwid script runs from its reference UTxO; every spend and
mint uses the unit redeemer ``Constr0 []`` (``d87980``), the staking withdrawals
the integer ``0`` (borrow) or ``1`` (liquidation).

Only :func:`add_funding` touches the Ogmios ``additionalUtxo`` stash: action
UTxOs, loans and reference inputs are left for Ogmios to resolve from its own
ledger, so one spent since it was resolved fails evaluation instead of being
evaluated as a stale copy. The legs add no payout or change output to the user;
that is the caller's balancing.

:func:`finish_loan_flow` is the shared end of every owner loan flow: the health
reference set and check, the owner's signature, the borrow staking withdrawal and
the reference-input hygiene check.
"""

from __future__ import annotations

from collections.abc import Iterable
from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import TYPE_CHECKING

from pycardano import Address
from pycardano import Asset
from pycardano import AssetName
from pycardano import MultiAsset
from pycardano import PlutusData
from pycardano import RawCBOR
from pycardano import RawPlutusData
from pycardano import Redeemer
from pycardano import ScriptHash
from pycardano import TransactionBuilder
from pycardano import TransactionInput
from pycardano import TransactionOutput
from pycardano import UTxO
from pycardano import Value
from pycardano import VerificationKeyHash
from pycardano import script_hash

from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.lending.liqwid.datums import CollateralParamsDatum
from charli3_dendrite.lending.liqwid.datums import MarketStateDatum
from charli3_dendrite.lending.liqwid.datums import Rational
from charli3_dendrite.lending.liqwid.datums import decode_oracle_feed
from charli3_dendrite.lending.liqwid.datums import encode_action_datum
from charli3_dendrite.lending.liqwid.deployment import LiqwidDeployment
from charli3_dendrite.lending.liqwid.deployment import MarketComponents
from charli3_dendrite.lending.liqwid.deployment import StakingRole
from charli3_dendrite.lending.liqwid.deployment import resolve_deployment
from charli3_dendrite.lending.liqwid.transactions.errors import (
    ForeignReferenceInputError,
)
from charli3_dendrite.lending.liqwid.transactions.errors import (
    InsufficientLiquidityError,
)
from charli3_dendrite.lending.liqwid.transactions.errors import MissingFeedError
from charli3_dendrite.lending.liqwid.transactions.errors import UnsupportedMarketError
from charli3_dendrite.lending.liqwid.transactions.resolve import collateral_link
from charli3_dendrite.lending.liqwid.transactions.terms import ActionDeltas
from charli3_dendrite.lending.liqwid.transactions.terms import check_health
from charli3_dendrite.lending.liqwid.transactions.terms import collateral_holdings
from charli3_dendrite.lending.liqwid.transactions.terms import health_required
from charli3_dendrite.lending.transactions.common import add_zero_withdrawal
from charli3_dendrite.lending.transactions.common import merge_reference_inputs
from charli3_dendrite.lending.transactions.common import plutus_int
from charli3_dendrite.lending.transactions.common import settled_min_ada
from charli3_dendrite.lending.transactions.common import with_reference_script
from charli3_dendrite.lending.transactions.utxos import Utxo
from charli3_dendrite.lending.transactions.utxos import ogmios_entry
from charli3_dendrite.lending.transactions.utxos import script_hash_of
from charli3_dendrite.lending.transactions.utxos import to_pycardano_utxo
from charli3_dendrite.lending.transactions.utxos import utxo_from_info
from charli3_dendrite.lending.transactions.utxos import utxo_value
from charli3_dendrite.lending.units import POLICY_HEX_LEN
from charli3_dendrite.lending.units import out_ref_text
from charli3_dendrite.lending.units import policy_of
from charli3_dendrite.utility import asset_to_value

if TYPE_CHECKING:
    from charli3_dendrite.lending.liqwid.indexing import LiqwidRecord
    from charli3_dendrite.lending.liqwid.oracles import LiqwidFeed
    from charli3_dendrite.lending.liqwid.transactions.resolve import ActionUtxo
    from charli3_dendrite.lending.liqwid.transactions.resolve import HealthInputs
    from charli3_dendrite.lending.liqwid.transactions.resolve import LiqwidScriptRefs
    from charli3_dendrite.lending.liqwid.transactions.resolve import MarketRecords
    from charli3_dendrite.lending.transactions.snapshot import PoolActionSnapshot

__all__ = [
    "add_action_leg",
    "add_funding",
    "add_loan_input",
    "add_loan_token_change",
    "add_owner_signer",
    "add_qtoken_leg",
    "add_staking_withdrawal",
    "check_loan_health",
    "check_reference_hygiene",
    "finish_loan_flow",
    "flip_name",
    "health_reference_set",
    "loan_output",
]

_LOVELACE = "lovelace"

# The loan-token name a modification writes for each current name.
_FLIPPED_NAME: dict[str, str] = {"00": "01", "01": "00", "30": "01", "31": "01"}

# The integer redeemer of the zero withdrawal from each staking script.
_STAKING_REDEEMER: dict[StakingRole, int] = {
    StakingRole.BORROW: 0,
    StakingRole.LIQUIDATION: 1,
}


@dataclass
class _Unit(PlutusData):
    """``Constr0 []`` (CBOR ``d87980``): the redeemer of every Liqwid spend and mint."""

    CONSTR_ID = 0


def _unit_redeemer() -> Redeemer:
    return Redeemer(_Unit())


# --- builder plumbing --------------------------------------------------------


def _input_ref(tx_input: TransactionInput) -> tuple[str, int]:
    return bytes(tx_input.transaction_id).hex(), tx_input.index


def _check_unspent(tx_builder: TransactionBuilder, utxo: UTxO) -> None:
    if any(held.input == utxo.input for held in tx_builder.inputs):
        spent = out_ref_text(_input_ref(utxo.input))
        msg = f"{spent} is already spent by this transaction"
        raise ValueError(msg)


def _mints_policy(tx_builder: TransactionBuilder, policy: str) -> bool:
    return any(
        script_hash(script).payload.hex() == policy
        for script, _ in tx_builder._minting_script_to_redeemers
    )


def _add_mint(
    tx_builder: TransactionBuilder,
    *,
    policy_ref: Utxo,
    policy: str,
    tokens: Mapping[str, int],
) -> None:
    """Merge ``tokens`` (name hex -> signed quantity) of ``policy`` into the mint.

    The policy's script is added once, from ``policy_ref``, with the unit
    redeemer; a later leg on the same policy only merges its quantities.

    Raises:
        ValueError: ``policy_ref`` does not carry the policy's script, or the
            merged mint of the policy cancels out.
    """
    found = script_hash_of(policy_ref)
    if found != policy:
        msg = f"reference script {found} is not the minting policy {policy}"
        raise ValueError(msg)
    key = ScriptHash(bytes.fromhex(policy))
    minted = MultiAsset(
        {
            key: Asset(
                {AssetName(bytes.fromhex(name)): qty for name, qty in tokens.items()},
            ),
        },
    )
    merged = minted if tx_builder.mint is None else tx_builder.mint + minted
    if key not in merged:
        msg = f"the mint and burn of policy {policy} cancel out"
        raise ValueError(msg)
    tx_builder.mint = merged
    if not _mints_policy(tx_builder, policy):
        with_reference_script(
            tx_builder,
            policy_ref,
            lambda ref: tx_builder.add_minting_script(ref, _unit_redeemer()),
        )


# --- legs --------------------------------------------------------------------


def add_funding(
    tx_builder: TransactionBuilder,
    stash: PoolActionSnapshot,
    funding: Sequence[Utxo],
) -> None:
    """Spend the wallet UTxOs ``funding`` and stash them for Ogmios evaluation.

    Each UTxO not already an input of the builder is added as an input and its
    ``additionalUtxo`` entry appended to ``stash``; one already spent is skipped.
    """
    held = {_input_ref(utxo.input) for utxo in tx_builder.inputs}
    for u in funding:
        utxo = to_pycardano_utxo(u)
        ref = _input_ref(utxo.input)
        if ref in held:
            continue
        tx_builder.add_input(utxo)
        stash.add_actor_additional_utxo(ogmios_entry(u))
        held.add(ref)


def _shifted_value(action: ActionUtxo, underlying: str, delta: int) -> Value:
    """The action UTxO's value with ``delta`` of ``underlying`` added."""
    held = action.underlying_amount(underlying)
    if held + delta < 0:
        raise InsufficientLiquidityError(-delta, held)
    utxo = action.utxo
    if underlying == _LOVELACE:
        return utxo_value(utxo.lovelace + delta, utxo.assets)
    policy, name = policy_of(underlying), underlying[POLICY_HEX_LEN:]
    assets = [a for a in utxo.assets if (a[0], a[1]) != (policy, name)]
    if held + delta:
        assets.append((policy, name, held + delta))
    return utxo_value(utxo.lovelace, assets)


def add_action_leg(
    tx_builder: TransactionBuilder,
    *,
    action: ActionUtxo,
    deltas: ActionDeltas,
    script_ref: Utxo,
    references: Sequence[Utxo],
    underlying: str,
    indefinite: bool | None = None,
) -> int:
    """Spend ``action`` and re-create it with this transaction's ``deltas``.

    The action UTxO is spent by the action validator in ``script_ref`` with the
    unit redeemer and re-created at the same full address (stake part kept) with
    the record ``old + deltas`` (nonce kept) and its value changed by
    ``deltas.underlying_delta()`` of ``underlying`` (``"lovelace"`` or a unit).
    The datum is written definite unless ``indefinite`` (a replay of a captured
    style). ``references`` (the market's MarketParams and MarketState) merge into
    the reference inputs.

    Returns:
        The index of the re-created action output.

    Raises:
        InsufficientLiquidityError: The action UTxO holds less ``underlying``
            than the deltas pay out.
        ValueError: The action UTxO is already spent by this transaction.
    """
    spent = to_pycardano_utxo(action.utxo)
    _check_unspent(tx_builder, spent)
    datum = encode_action_datum(
        deltas.apply_to(action.datum),
        indefinite=bool(indefinite),
    )
    output = TransactionOutput(
        spent.output.address,
        _shifted_value(action, underlying, deltas.underlying_delta()),
        datum=RawCBOR(datum),
    )
    with_reference_script(
        tx_builder,
        script_ref,
        lambda ref: tx_builder.add_script_input(
            spent,
            script=ref,
            redeemer=_unit_redeemer(),
        ),
    )
    merge_reference_inputs(tx_builder, references)
    tx_builder.add_output(output)
    return len(tx_builder.outputs) - 1


def add_qtoken_leg(
    tx_builder: TransactionBuilder,
    *,
    policy_ref: Utxo,
    qtoken_policy: str,
    quantity: int,
) -> None:
    """Mint (``quantity > 0``) or burn (``< 0``) qTokens (empty asset name).

    Raises:
        ValueError: ``quantity`` is zero, or ``policy_ref`` does not carry the
            qToken policy.
    """
    if quantity == 0:
        msg = "a qToken leg mints or burns a non-zero quantity"
        raise ValueError(msg)
    _add_mint(
        tx_builder,
        policy_ref=policy_ref,
        policy=qtoken_policy,
        tokens={"": quantity},
    )


def add_loan_input(
    tx_builder: TransactionBuilder,
    *,
    loan: Utxo,
    script_ref: Utxo,
) -> None:
    """Spend ``loan`` by the loan validator in ``script_ref``, unit redeemer.

    Raises:
        ValueError: The loan is already spent by this transaction.
    """
    spent = to_pycardano_utxo(loan)
    _check_unspent(tx_builder, spent)
    with_reference_script(
        tx_builder,
        script_ref,
        lambda ref: tx_builder.add_script_input(
            spent,
            script=ref,
            redeemer=_unit_redeemer(),
        ),
    )


def flip_name(current: str) -> str:
    """The loan-token name a modification writes: ``00``/``01`` swap, ``3x`` -> ``01``.

    Raises:
        ValueError: ``current`` is not a loan-token name.
    """
    try:
        return _FLIPPED_NAME[current]
    except KeyError:
        msg = f"unknown loan-token name {current!r}"
        raise ValueError(msg) from None


def add_loan_token_change(
    tx_builder: TransactionBuilder,
    *,
    policy_ref: Utxo,
    loan_policy: str,
    current_name: str | None,
    new_name: str | None,
) -> None:
    """Burn the loan token ``current_name`` and mint ``new_name``, one redeemer.

    An open passes ``current_name=None`` (mint only) and a close
    ``new_name=None`` (burn only); a modification passes both.

    Raises:
        ValueError: The two names are equal (both None included), or
            ``policy_ref`` does not carry the loan-token policy.
    """
    if current_name == new_name:
        msg = f"a loan-token change needs two different names, got {current_name!r}"
        raise ValueError(msg)
    tokens: dict[str, int] = {}
    if current_name is not None:
        tokens[current_name] = -1
    if new_name is not None:
        tokens[new_name] = 1
    _add_mint(tx_builder, policy_ref=policy_ref, policy=loan_policy, tokens=tokens)


def add_staking_withdrawal(
    tx_builder: TransactionBuilder,
    *,
    script_refs: LiqwidScriptRefs,
    role: StakingRole,
) -> None:
    """Withdraw zero from the market's ``role`` staking script.

    The redeemer is ``0`` for the borrow script and ``1`` for the liquidation
    script; a withdrawal already on the builder is kept as it is.

    Raises:
        ValueError: ``script_refs`` carries no script for ``role``.
    """
    script = (
        script_refs.borrow_staking
        if role is StakingRole.BORROW
        else script_refs.liquidation_staking
    )
    if script is None:
        msg = f"no {role.value} staking reference script was resolved"
        raise ValueError(msg)
    add_zero_withdrawal(
        tx_builder,
        script_ref=script,
        redeemer=plutus_int(_STAKING_REDEEMER[role]),
    )


def loan_output(
    *,
    address: str,
    datum: bytes,
    loan_policy: str,
    loan_token: str,
    collateral: Mapping[str, int],
    previous_lovelace: int | None,
) -> TransactionOutput:
    """A loan output: the loan token, the collateral and the least lovelace.

    The output holds one ``loan_token`` of ``loan_policy``, every non-zero
    collateral quantity and the inline ``datum``. Its lovelace is the exact
    ledger minimum ``(160 + S) * coinsPerUTxOByte`` at its fixed point (``S`` the
    encoded output size), or ``previous_lovelace`` when that is more: a flow
    never lowers a loan's lovelace.

    Raises:
        ValueError: A collateral quantity is negative, or a collateral unit is
            lovelace or the loan token.
    """
    token = loan_policy + loan_token
    assets = {token: 1}
    for unit, qty in collateral.items():
        if qty < 0:
            msg = f"negative collateral quantity {qty} of {unit}"
            raise ValueError(msg)
        if unit in (_LOVELACE, token):
            msg = f"{unit} cannot be loan collateral"
            raise ValueError(msg)
        if qty:
            assets[unit] = qty
    target = Address.decode(address)
    inline = RawCBOR(datum)
    need = settled_min_ada(target, assets, inline)
    lovelace = need if previous_lovelace is None else max(previous_lovelace, need)
    return TransactionOutput(
        target,
        asset_to_value(Assets(**{_LOVELACE: lovelace, **assets})),
        datum=inline,
    )


# --- health ------------------------------------------------------------------


def _feed(feeds: Mapping[str, LiqwidFeed], underlying: str) -> LiqwidFeed:
    feed = feeds.get(underlying)
    if feed is None or feed.info is None:
        raise MissingFeedError(underlying)
    return feed


def _state(inputs: HealthInputs, market: MarketComponents) -> LiqwidRecord:
    state = inputs.states.get(market.market_id)
    if state is None:
        raise UnsupportedMarketError(market.name, "no MarketState among the inputs")
    return state


def _unique(utxos: Iterable[Utxo]) -> list[Utxo]:
    """``utxos`` with each out-ref kept once, the first."""
    seen: set[tuple[str, int] | None] = set()
    unique = []
    for u in utxos:
        if u.out_ref not in seen:
            seen.add(u.out_ref)
            unique.append(u)
    return unique


def health_reference_set(
    units: Iterable[str],
    *,
    borrow_market_id: str,
    borrow_underlying: str,
    inputs: HealthInputs,
    deployment: LiqwidDeployment | None = None,
) -> list[Utxo]:
    """The reference inputs a loan's health check reads, each out-ref once.

    One CollateralParams record per distinct unit of ``units``, the MarketState
    of each collateral market other than the borrow market, and one oracle feed
    per distinct underlying of the collateral and of the borrow. A collateral of
    the borrow market (a wash borrow) adds no MarketState and shares its feed.
    Each collateral market is the one its record links in ``deployment`` (the
    bundled one by default), as
    :func:`~charli3_dendrite.lending.liqwid.transactions.resolve.collateral_link`
    reads it.

    Raises:
        UnsupportedCollateralError: A unit has no exact CollateralParams record
            naming its market and underlying, or the market it names is not in
            ``deployment``.
        UnsupportedMarketError: A collateral market has no MarketState in
            ``inputs``.
        MissingFeedError: No feed in ``inputs`` prices an underlying.
    """
    active = resolve_deployment(deployment)
    records: list[Utxo] = []
    markets: dict[str, MarketComponents] = {}
    underlyings: list[str] = []
    for unit in dict.fromkeys(units):
        market, datum = collateral_link(unit, inputs.cparams.get(unit), active)
        records.append(utxo_from_info(inputs.cparams[unit].info))
        if market.market_id != borrow_market_id:
            markets.setdefault(market.market_id, market)
        underlyings.append(datum.underlying)
    states = [_state(inputs, market).info for market in markets.values()]
    feeds = [
        _feed(inputs.feeds, underlying).info
        for underlying in dict.fromkeys([*underlyings, borrow_underlying])
    ]
    records.extend(utxo_from_info(info) for info in states)
    records.extend(utxo_from_info(info) for info in feeds if info is not None)
    return _unique(records)


def _price(feeds: Mapping[str, LiqwidFeed], underlying: str) -> Rational:
    feed = _feed(feeds, underlying)
    return (feed.usd_num, feed.usd_den)


def check_loan_health(
    records: MarketRecords,
    inputs: HealthInputs,
    collateral: Mapping[str, int],
    debt: int,
    deployment: LiqwidDeployment,
) -> None:
    """Check a loan holding ``collateral`` against ``debt`` (`check_health`).

    The collateral is valued through the borrow market's (``records``)
    CollateralParams in ``inputs`` and the debt priced by the borrowed asset's
    feed, at the market's maximum LTV.

    Raises:
        UnsupportedCollateralError: A unit has no exact CollateralParams record
            naming its market and underlying.
        UnsupportedMarketError: A collateral market has no MarketState in
            ``inputs``.
        MissingFeedError: No feed prices an underlying or the borrowed asset.
        UnhealthyLoanError: The loan fails the health rule.
    """
    cparams: dict[str, CollateralParamsDatum] = {}
    rates: dict[str, Rational] = {}
    prices: dict[str, Rational] = {}
    for unit, qty in collateral.items():
        if qty <= 0:
            continue
        market, datum = collateral_link(unit, inputs.cparams.get(unit), deployment)
        state = _state(inputs, market).datum
        if not isinstance(state, MarketStateDatum):
            msg = f"liqwid record of market {market.name} is not a MarketState"
            raise TypeError(msg)
        cparams[unit] = datum
        rates[unit] = state.qtoken_rate
        prices[datum.underlying] = _price(inputs.feeds, datum.underlying)
    holdings = collateral_holdings(
        collateral,
        cparams=cparams,
        rates=rates,
        prices=prices,
    )
    check_health(
        holdings,
        debt,
        Fraction(*_price(inputs.feeds, records.market.underlying)),
        Fraction(*records.params_datum.max_ltv),
    )


def add_owner_signer(tx_builder: TransactionBuilder, owner_pkh: str) -> None:
    """Require the signature of ``owner_pkh`` (hex), once."""
    key = VerificationKeyHash(bytes.fromhex(owner_pkh))
    signers = list(tx_builder.required_signers or [])
    if key not in signers:
        signers.append(key)
    tx_builder.required_signers = signers


def finish_loan_flow(
    tx_builder: TransactionBuilder,
    *,
    records: MarketRecords,
    script_refs: LiqwidScriptRefs,
    health: HealthInputs | None,
    owner: str,
    collateral_in: Mapping[str, int],
    collateral_out: Mapping[str, int] | None,
    debt: int,
    borrow: int,
    repay: int,
    deployment: LiqwidDeployment,
) -> None:
    """The shared end of every owner loan flow on the borrow market ``records``.

    When a loan output remains (``collateral_out`` is not None), the health
    reference set of its collateral joins the reference inputs and, when the
    loan validator checks health for this change (`health_required`), the
    output loan must carry ``debt`` (`health_debt`) at the borrow market's
    maximum LTV. A close (``collateral_out=None``) references no health set.
    Every flow then requires the ``owner``'s signature, withdraws zero from the
    borrow staking script and passes `check_reference_hygiene`. Nothing is added
    when the health check fails.

    Args:
        tx_builder: The builder the flow's legs were added to.
        records: The borrow market's records.
        script_refs: The borrow market's reference scripts.
        health: The health inputs of the output collateral; required when a loan
            output remains.
        owner: The loan owner's key hash (hex).
        collateral_in: Collateral unit -> quantity of the input loan (empty on
            an open).
        collateral_out: Collateral unit -> quantity of the output loan, or None
            when no loan output remains.
        debt: The debt the health check measures, in borrowed base units.
        borrow: Principal lent by this transaction.
        repay: Amount repaid by this transaction.
        deployment: The Liqwid deployment the health set links collateral markets
            in and the hygiene check reads.

    Raises:
        ValueError: A loan output remains and ``health`` is None.
        UnhealthyLoanError: The output loan fails the health rule.
        ForeignReferenceInputError: The transaction references an input that is
            neither a reference script nor a Liqwid record.
    """
    if collateral_out is not None:
        if health is None:
            msg = "a loan flow that keeps its loan needs health inputs"
            raise ValueError(msg)
        held = [unit for unit, qty in collateral_out.items() if qty > 0]
        references = health_reference_set(
            held,
            borrow_market_id=records.market.market_id,
            borrow_underlying=records.market.underlying,
            inputs=health,
            deployment=deployment,
        )
        if health_required(borrow, repay, collateral_in, collateral_out):
            check_loan_health(records, health, collateral_out, debt, deployment)
        merge_reference_inputs(tx_builder, references)
    add_owner_signer(tx_builder, owner)
    add_staking_withdrawal(
        tx_builder,
        script_refs=script_refs,
        role=StakingRole.BORROW,
    )
    check_reference_hygiene(tx_builder, deployment=deployment)


# --- reference hygiene -------------------------------------------------------


def _tokens(output: TransactionOutput) -> list[tuple[str, str, int]]:
    return [
        (policy.payload.hex(), name.payload.hex(), qty)
        for policy, asset in output.amount.multi_asset.items()
        for name, qty in asset.items()
    ]


def _is_record(output: TransactionOutput, deployment: LiqwidDeployment) -> bool:
    """Whether ``output`` holds exactly one Liqwid market record NFT."""
    nfts = [
        (name, qty)
        for policy, name, qty in _tokens(output)
        if deployment.market_of_params_policy(policy) is not None
        or deployment.market_of_collateral_params_policy(policy) is not None
        or deployment.market_of_state_policy(policy) is not None
    ]
    return nfts == [("", 1)]


def _inline_datum(output: TransactionOutput) -> bytes | None:
    datum = output.datum
    if isinstance(datum, RawCBOR):
        return datum.cbor
    if isinstance(datum, (PlutusData, RawPlutusData)):
        return datum.to_cbor()
    return None


def _is_feed(output: TransactionOutput, deployment: LiqwidDeployment) -> bool:
    """Whether ``output`` is a genuine oracle feed, at whatever address.

    It holds the oracle token alone under its policy (empty name, quantity 1)
    and an inline datum that decodes as a feed record.
    """
    tokens = [
        (name, qty)
        for policy, name, qty in _tokens(output)
        if policy == deployment.oracle_token_policy
    ]
    datum = _inline_datum(output)
    if tokens != [("", 1)] or datum is None:
        return False
    try:
        decode_oracle_feed(datum)
    except ValueError:
        return False
    return True


def check_reference_hygiene(
    tx_builder: TransactionBuilder,
    *,
    deployment: LiqwidDeployment,
) -> None:
    """Refuse a reference input a Liqwid loan validator would reject.

    Each reference input must be a UTxO that carries a reference script, a
    Liqwid record (exactly one MarketParams, CollateralParams or MarketState
    NFT) or a genuine oracle feed. A loan, a wallet UTxO and a bare out-ref
    (whose output is unknown) are refused.

    Raises:
        ForeignReferenceInputError: The first such input in out-ref order.
    """
    members = sorted(
        tx_builder.reference_inputs,
        key=lambda m: _input_ref(m.input if isinstance(m, UTxO) else m),
    )
    for member in members:
        if isinstance(member, UTxO) and (
            member.output.script is not None
            or _is_record(member.output, deployment)
            or _is_feed(member.output, deployment)
        ):
            continue
        tx_input = member.input if isinstance(member, UTxO) else member
        raise ForeignReferenceInputError(out_ref_text(_input_ref(tx_input)))
