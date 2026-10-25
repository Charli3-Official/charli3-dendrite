"""Composable Liqwid V2 transaction legs, on synthetic UTxOs and captured records.

Scripts are synthetic PlutusV2 scripts (placeholder bytes, never executed); every
spent script UTxO sits at its script's hash so the builder accepts the spend.
Records and feeds are the captured live ones, resolved offline.
"""

import dataclasses
from fractions import Fraction

import cbor2
import pytest
from pycardano import Address
from pycardano import AssetName
from pycardano import Network
from pycardano import PlutusV2Script
from pycardano import RawCBOR
from pycardano import ScriptHash
from pycardano import TransactionBuilder
from pycardano import TransactionId
from pycardano import TransactionInput
from pycardano import VerificationKeyHash

from charli3_dendrite.lending.liqwid.datums import ActionDatum
from charli3_dendrite.lending.liqwid.datums import LoanDatum
from charli3_dendrite.lending.liqwid.datums import datum_style
from charli3_dendrite.lending.liqwid.datums import decode_action
from charli3_dendrite.lending.liqwid.datums import encode_action_datum
from charli3_dendrite.lending.liqwid.datums import encode_loan_datum
from charli3_dendrite.lending.liqwid.deployment import StakingRole
from charli3_dendrite.lending.liqwid.deployment import default_deployment
from charli3_dendrite.lending.liqwid.transactions.errors import (
    ForeignReferenceInputError,
)
from charli3_dendrite.lending.liqwid.transactions.errors import (
    InsufficientLiquidityError,
)
from charli3_dendrite.lending.liqwid.transactions.errors import MissingFeedError
from charli3_dendrite.lending.liqwid.transactions.errors import UnhealthyLoanError
from charli3_dendrite.lending.liqwid.transactions.errors import (
    UnsupportedCollateralError,
)
from charli3_dendrite.lending.liqwid.transactions.legs import add_action_leg
from charli3_dendrite.lending.liqwid.transactions.legs import add_funding
from charli3_dendrite.lending.liqwid.transactions.legs import add_loan_input
from charli3_dendrite.lending.liqwid.transactions.legs import add_loan_token_change
from charli3_dendrite.lending.liqwid.transactions.legs import add_owner_signer
from charli3_dendrite.lending.liqwid.transactions.legs import add_qtoken_leg
from charli3_dendrite.lending.liqwid.transactions.legs import add_staking_withdrawal
from charli3_dendrite.lending.liqwid.transactions.legs import (
    check_reference_hygiene,
)
from charli3_dendrite.lending.liqwid.transactions.legs import finish_loan_flow
from charli3_dendrite.lending.liqwid.transactions.legs import flip_name
from charli3_dendrite.lending.liqwid.transactions.legs import health_reference_set
from charli3_dendrite.lending.liqwid.transactions.legs import loan_output
from charli3_dendrite.lending.liqwid.transactions.resolve import ActionUtxo
from charli3_dendrite.lending.liqwid.transactions.resolve import HealthInputs
from charli3_dendrite.lending.liqwid.transactions.resolve import LiqwidScriptRefs
from charli3_dendrite.lending.liqwid.transactions.resolve import (
    resolve_health_inputs,
)
from charli3_dendrite.lending.liqwid.transactions.resolve import (
    resolve_market_records,
)
from charli3_dendrite.lending.liqwid.transactions.terms import ActionDeltas
from charli3_dendrite.lending.transactions.infra import EvalContext
from charli3_dendrite.lending.transactions.infra import assemble_unsigned
from charli3_dendrite.lending.transactions.snapshot import PoolActionSnapshot
from charli3_dendrite.lending.transactions.utxos import Utxo
from charli3_dendrite.lending.transactions.utxos import ogmios_entry
from charli3_dendrite.lending.transactions.utxos import script_hash_of
from charli3_dendrite.lending.transactions.utxos import to_pycardano_utxo
from charli3_dendrite.lending.transactions.utxos import utxo_from_info
from charli3_dendrite.lending.units import script_payment_address
from tests.lending.liqwid.records import parsed_records
from tests.lending.liqwid.transactions.backend import FixtureBackend
from tests.lending.liqwid.transactions.backend import live_records

SCRIPT_HEX = "4e4d01000033222220051200120011"
SCRIPT_TX = "51" * 32
ACTION_TX = "a1" * 32
LOAN_TX = "b2" * 32
WALLET_TX = "c3" * 32
ACTION_TOKEN = "7a" * 28
TOKEN = "dd" * 28 + "4e4d"
OWNER = "cc" * 28
STAKE = "f61e5cf9" * 7
NONCE = cbor2.CBORTag(121, [7])
COINS_PER_BYTE = 4310


# --- synthetic UTxOs ---------------------------------------------------------


def _script(tag, *, language="plutusV2"):
    """A reference-script UTxO carrying a distinct placeholder script."""
    return Utxo(
        address=script_payment_address("5c" * 28),
        lovelace=20_000_000,
        assets=[],
        datum=None,
        ref_script=SCRIPT_HEX + f"{tag:02x}",
        ref_script_type=language,
        out_ref=(SCRIPT_TX, tag),
    )


def _script_address(script, *, stake=None):
    return Address(
        payment_part=ScriptHash(bytes.fromhex(script_hash_of(script))),
        staking_part=VerificationKeyHash(bytes.fromhex(stake)) if stake else None,
        network=Network.MAINNET,
    ).encode()


def _action(script, *, underlying="lovelace", lovelace=500_000_000, held=0, index=0):
    datum = ActionDatum(10, -3, 5, 1, 0, NONCE)
    assets = [(ACTION_TOKEN, "", 1)]
    if underlying != "lovelace":
        assets.append((underlying[:56], underlying[56:], held))
    utxo = Utxo(
        address=_script_address(script, stake=STAKE),
        lovelace=lovelace,
        assets=sorted(assets),
        datum=encode_action_datum(datum).hex(),
        out_ref=(ACTION_TX, index),
    )
    return ActionUtxo(utxo=utxo, datum=datum)


def _loan(script, *, token="00", collateral=None, index=0):
    policy = script_hash_of(_script(LOAN_POLICY))
    assets = [(policy, token, 1)]
    for unit, qty in (collateral or {"ee" * 28: 1_000}).items():
        assets.append((unit[:56], unit[56:], qty))
    return Utxo(
        address=_script_address(script),
        lovelace=3_000_000,
        assets=sorted(assets),
        datum=encode_loan_datum(LoanDatum(OWNER, 100, 0, 1, 10**6)).hex(),
        out_ref=(LOAN_TX, index),
    )


def _wallet(index=0):
    return Utxo(
        address=Address(
            payment_part=VerificationKeyHash(bytes.fromhex(OWNER)),
            network=Network.MAINNET,
        ).encode(),
        lovelace=50_000_000,
        assets=[],
        datum=None,
        out_ref=(WALLET_TX, index),
    )


ACTION_SCRIPT, QTOKEN_POLICY, LOAN_SCRIPT, LOAN_POLICY = 1, 2, 3, 4
BORROW_STAKING, LIQUIDATION_STAKING = 5, 6


def _script_refs():
    return LiqwidScriptRefs(
        action=_script(ACTION_SCRIPT),
        qtoken_policy=_script(QTOKEN_POLICY),
        loan_validator=_script(LOAN_SCRIPT),
        loan_policy=_script(LOAN_POLICY),
        borrow_staking=_script(BORROW_STAKING),
        liquidation_staking=_script(LIQUIDATION_STAKING),
    )


def _builder():
    return TransactionBuilder(EvalContext(last_block_slot=0))


def _text(out_ref):
    return f"{out_ref[0]}#{out_ref[1]}"


def _reference_refs(tx_builder):
    return {
        (bytes(m.input.transaction_id).hex(), m.input.index)
        for m in tx_builder.reference_inputs
    }


class _Snapshot(PoolActionSnapshot):
    pass


# --- captured records --------------------------------------------------------


def _records(name):
    market = default_deployment().market_by_name(name)
    backend = FixtureBackend(records=live_records())
    return resolve_market_records(backend, [market])[name]


def _health(units, borrow):
    return resolve_health_inputs(
        FixtureBackend(records=live_records()),
        units,
        borrow=borrow,
    )


def _record_utxo(kind):
    return utxo_from_info(parsed_records(kind, live=True)[0][1].info)


def _out_ref(utxo):
    return utxo.out_ref


# --- add_action_leg ----------------------------------------------------------


def test_an_ada_supply_raises_the_action_lovelace_and_keeps_address_and_nonce():
    script = _script(ACTION_SCRIPT)
    action = _action(script)
    tx_builder = _builder()
    index = add_action_leg(
        tx_builder,
        action=action,
        deltas=ActionDeltas(supply=7_000_000, qtoken=300),
        script_ref=script,
        references=[],
        underlying="lovelace",
    )
    assert index == 0
    output = tx_builder.outputs[index]
    assert output.address == Address.decode(action.utxo.address)
    assert output.address.staking_part == VerificationKeyHash(bytes.fromhex(STAKE))
    assert output.amount.coin == action.utxo.lovelace + 7_000_000
    assert output.amount.multi_asset == tx_builder.inputs[0].output.amount.multi_asset
    assert isinstance(output.datum, RawCBOR)
    assert datum_style(output.datum.cbor) == "definite"
    written = decode_action(output.datum.cbor)
    assert written == ActionDatum(7_000_010, 297, 5, 1, 0, NONCE)
    assert written.nonce == NONCE


def test_the_action_spend_uses_the_unit_redeemer_and_the_reference_script():
    script = _script(ACTION_SCRIPT)
    tx_builder = _builder()
    add_action_leg(
        tx_builder,
        action=_action(script),
        deltas=ActionDeltas(supply=1),
        script_ref=script,
        references=[],
        underlying="lovelace",
    )
    (redeemer,) = tx_builder._inputs_to_redeemers.values()
    assert redeemer.data.to_cbor_hex() == "d87980"
    assert _reference_refs(tx_builder) == {(SCRIPT_TX, ACTION_SCRIPT)}


def test_a_token_supply_adds_the_underlying_and_keeps_the_lovelace():
    script = _script(ACTION_SCRIPT)
    action = _action(script, underlying=TOKEN, lovelace=3_000_000, held=1_000)
    tx_builder = _builder()
    index = add_action_leg(
        tx_builder,
        action=action,
        deltas=ActionDeltas(supply=250, qtoken=11),
        script_ref=script,
        references=[],
        underlying=TOKEN,
    )
    output = tx_builder.outputs[index]
    assert output.amount.coin == 3_000_000
    held = output.amount.multi_asset[ScriptHash(bytes.fromhex(TOKEN[:56]))]
    assert held[AssetName(bytes.fromhex(TOKEN[56:]))] == 1_250
    token = output.amount.multi_asset[ScriptHash(bytes.fromhex(ACTION_TOKEN))]
    assert token[AssetName(b"")] == 1


def test_a_payout_of_the_whole_underlying_drops_the_token():
    script = _script(ACTION_SCRIPT)
    action = _action(script, underlying=TOKEN, lovelace=3_000_000, held=1_000)
    tx_builder = _builder()
    index = add_action_leg(
        tx_builder,
        action=action,
        deltas=ActionDeltas(principal=1_000),
        script_ref=script,
        references=[],
        underlying=TOKEN,
    )
    multi_asset = tx_builder.outputs[index].amount.multi_asset
    assert ScriptHash(bytes.fromhex(TOKEN[:56])) not in multi_asset


def test_a_payout_beyond_the_underlying_is_refused():
    script = _script(ACTION_SCRIPT)
    action = _action(script, underlying=TOKEN, lovelace=3_000_000, held=1_000)
    with pytest.raises(InsufficientLiquidityError) as info:
        add_action_leg(
            _builder(),
            action=action,
            deltas=ActionDeltas(supply=-1_001),
            script_ref=script,
            references=[],
            underlying=TOKEN,
        )
    assert (info.value.needed, info.value.available) == (1_001, 1_000)


def test_a_replay_style_writes_an_indefinite_datum():
    script = _script(ACTION_SCRIPT)
    tx_builder = _builder()
    index = add_action_leg(
        tx_builder,
        action=_action(script),
        deltas=ActionDeltas(supply=1),
        script_ref=script,
        references=[],
        underlying="lovelace",
        indefinite=True,
    )
    assert datum_style(tx_builder.outputs[index].datum.cbor) == "indefinite"


def test_action_references_merge_by_out_ref():
    script = _script(ACTION_SCRIPT)
    params = _record_utxo("market_params")
    state = _record_utxo("market_state")
    tx_builder = _builder()
    add_action_leg(
        tx_builder,
        action=_action(script, index=0),
        deltas=ActionDeltas(supply=1),
        script_ref=script,
        references=[params, state],
        underlying="lovelace",
    )
    add_action_leg(
        tx_builder,
        action=_action(script, index=1),
        deltas=ActionDeltas(supply=2),
        script_ref=dataclasses.replace(script, lovelace=99),
        references=[state, dataclasses.replace(params, lovelace=1)],
        underlying="lovelace",
    )
    assert len(tx_builder.reference_inputs) == 3
    assert _reference_refs(tx_builder) == {
        (SCRIPT_TX, ACTION_SCRIPT),
        _out_ref(params),
        _out_ref(state),
    }
    assert len(tx_builder.outputs) == 2


def test_one_action_utxo_cannot_be_spent_twice():
    script = _script(ACTION_SCRIPT)
    action = _action(script)
    tx_builder = _builder()
    leg = {
        "action": action,
        "deltas": ActionDeltas(supply=1),
        "script_ref": script,
        "references": [],
        "underlying": "lovelace",
    }
    add_action_leg(tx_builder, **leg)
    with pytest.raises(ValueError, match="already"):
        add_action_leg(tx_builder, **leg)


def test_the_action_leg_stashes_nothing_and_funding_stashes_only_the_wallet():
    script = _script(ACTION_SCRIPT)
    action = _action(script)
    snapshot = _Snapshot()
    tx_builder = _builder()
    add_action_leg(
        tx_builder,
        action=action,
        deltas=ActionDeltas(supply=1),
        script_ref=script,
        references=[_record_utxo("market_state")],
        underlying="lovelace",
    )
    assert not hasattr(snapshot, "_additional_utxos")
    add_funding(tx_builder, snapshot, [_wallet(0), action.utxo, _wallet(0)])
    assert snapshot.additional_utxo() == [ogmios_entry(_wallet(0))]
    assert len(tx_builder.inputs) == 2


def test_funding_adds_each_wallet_utxo_once():
    snapshot = _Snapshot()
    tx_builder = _builder()
    add_funding(tx_builder, snapshot, [_wallet(0), _wallet(1)])
    add_funding(tx_builder, snapshot, [_wallet(1), _wallet(2)])
    assert len(tx_builder.inputs) == 3
    assert snapshot.additional_utxo() == [ogmios_entry(_wallet(i)) for i in range(3)]


# --- qTokens and loan tokens -------------------------------------------------


def _policy(tag):
    return script_hash_of(_script(tag))


def _mint(tx_builder):
    return {
        (policy.payload.hex(), name.payload.hex(), qty)
        for policy, asset in (tx_builder.mint or {}).items()
        for name, qty in asset.items()
    }


def test_a_qtoken_mint_and_a_second_leg_merge_under_one_redeemer():
    policy = _policy(QTOKEN_POLICY)
    tx_builder = _builder()
    add_qtoken_leg(
        tx_builder,
        policy_ref=_script(QTOKEN_POLICY),
        qtoken_policy=policy,
        quantity=500,
    )
    add_qtoken_leg(
        tx_builder,
        policy_ref=_script(QTOKEN_POLICY),
        qtoken_policy=policy,
        quantity=25,
    )
    assert _mint(tx_builder) == {(policy, "", 525)}
    assert len(tx_builder._minting_script_to_redeemers) == 1
    (_, redeemer) = tx_builder._minting_script_to_redeemers[0]
    assert redeemer.data.to_cbor_hex() == "d87980"
    assert _reference_refs(tx_builder) == {(SCRIPT_TX, QTOKEN_POLICY)}


def test_a_qtoken_burn_is_a_negative_mint():
    policy = _policy(QTOKEN_POLICY)
    tx_builder = _builder()
    add_qtoken_leg(
        tx_builder,
        policy_ref=_script(QTOKEN_POLICY),
        qtoken_policy=policy,
        quantity=-40,
    )
    assert _mint(tx_builder) == {(policy, "", -40)}


def test_a_policy_reference_of_another_script_is_refused():
    tx_builder = _builder()
    with pytest.raises(ValueError, match="policy"):
        add_qtoken_leg(
            tx_builder,
            policy_ref=_script(LOAN_POLICY),
            qtoken_policy=_policy(QTOKEN_POLICY),
            quantity=1,
        )
    assert tx_builder.mint is None
    assert not tx_builder._minting_script_to_redeemers


def test_a_zero_qtoken_quantity_is_refused():
    with pytest.raises(ValueError, match="zero"):
        add_qtoken_leg(
            _builder(),
            policy_ref=_script(QTOKEN_POLICY),
            qtoken_policy=_policy(QTOKEN_POLICY),
            quantity=0,
        )


@pytest.mark.parametrize(
    ("current", "new", "expected"),
    [
        (None, "01", {("01", 1)}),
        ("00", "01", {("00", -1), ("01", 1)}),
        ("31", "01", {("31", -1), ("01", 1)}),
        ("01", None, {("01", -1)}),
    ],
)
def test_a_loan_token_change_mints_and_burns_under_one_redeemer(current, new, expected):
    policy = _policy(LOAN_POLICY)
    tx_builder = _builder()
    add_loan_token_change(
        tx_builder,
        policy_ref=_script(LOAN_POLICY),
        loan_policy=policy,
        current_name=current,
        new_name=new,
    )
    assert _mint(tx_builder) == {(policy, name, qty) for name, qty in expected}
    assert len(tx_builder._minting_script_to_redeemers) == 1


@pytest.mark.parametrize(("current", "new"), [(None, None), ("01", "01")])
def test_a_loan_token_change_needs_two_different_names(current, new):
    with pytest.raises(ValueError, match="name"):
        add_loan_token_change(
            _builder(),
            policy_ref=_script(LOAN_POLICY),
            loan_policy=_policy(LOAN_POLICY),
            current_name=current,
            new_name=new,
        )


@pytest.mark.parametrize(
    ("current", "flipped"),
    [("00", "01"), ("01", "00"), ("30", "01"), ("31", "01")],
)
def test_flip_name(current, flipped):
    assert flip_name(current) == flipped


@pytest.mark.parametrize("current", ["", "02", "0001", "ff"])
def test_an_unknown_loan_token_name_does_not_flip(current):
    with pytest.raises(ValueError, match="loan-token name"):
        flip_name(current)


# --- loan input --------------------------------------------------------------


def test_a_loan_input_is_spent_by_the_loan_validator_reference():
    script = _script(LOAN_SCRIPT)
    tx_builder = _builder()
    add_loan_input(tx_builder, loan=_loan(script), script_ref=script)
    assert len(tx_builder.inputs) == 1
    (redeemer,) = tx_builder._inputs_to_redeemers.values()
    assert redeemer.data.to_cbor_hex() == "d87980"
    assert _reference_refs(tx_builder) == {(SCRIPT_TX, LOAN_SCRIPT)}
    with pytest.raises(ValueError, match="already"):
        add_loan_input(tx_builder, loan=_loan(script), script_ref=script)


# --- staking withdrawal ------------------------------------------------------


def _withdrawal_redeemers(tx_builder):
    return [r.data.to_cbor_hex() for _, r in tx_builder._withdrawal_script_to_redeemers]


def test_two_borrow_withdrawals_leave_one_redeemer_zero():
    tx_builder = _builder()
    for _ in range(2):
        add_staking_withdrawal(
            tx_builder,
            script_refs=_script_refs(),
            role=StakingRole.BORROW,
        )
    assert _withdrawal_redeemers(tx_builder) == ["00"]
    assert list(tx_builder.withdrawals.values()) == [0]


def test_the_liquidation_withdrawal_uses_redeemer_one():
    tx_builder = _builder()
    add_staking_withdrawal(
        tx_builder,
        script_refs=_script_refs(),
        role=StakingRole.LIQUIDATION,
    )
    add_staking_withdrawal(
        tx_builder,
        script_refs=_script_refs(),
        role=StakingRole.BORROW,
    )
    assert _withdrawal_redeemers(tx_builder) == ["01", "00"]
    assert len(tx_builder.withdrawals) == 2


def test_a_missing_staking_script_is_refused():
    refs = dataclasses.replace(_script_refs(), borrow_staking=None)
    with pytest.raises(ValueError, match="borrow"):
        add_staking_withdrawal(_builder(), script_refs=refs, role=StakingRole.BORROW)


# --- loan output -------------------------------------------------------------


def _loan_output(previous=None, collateral=None):
    return loan_output(
        address=script_payment_address(script_hash_of(_script(LOAN_SCRIPT))),
        datum=encode_loan_datum(LoanDatum(OWNER, 10**9, 0, 800_000, 10**6)),
        loan_policy=_policy(LOAN_POLICY),
        loan_token="01",
        collateral={"ee" * 28: 10**12} if collateral is None else collateral,
        previous_lovelace=previous,
    )


def test_loan_lovelace_is_the_exact_minimum_at_its_fixed_point():
    output = _loan_output()
    size = len(output.to_cbor())
    assert output.amount.coin == (160 + size) * COINS_PER_BYTE
    assert output.address.staking_part is None
    assert isinstance(output.datum, RawCBOR)
    assets = {
        (p.payload.hex(), n.payload.hex(), q)
        for p, a in output.amount.multi_asset.items()
        for n, q in a.items()
    }
    assert assets == {(_policy(LOAN_POLICY), "01", 1), ("ee" * 28, "", 10**12)}


def test_loan_lovelace_never_drops_below_the_previous_amount():
    need = _loan_output().amount.coin
    assert _loan_output(previous=need + 5).amount.coin == need + 5
    assert _loan_output(previous=need - 1_000).amount.coin == need


def test_a_zero_collateral_is_left_out_and_a_negative_one_refused():
    output = _loan_output(collateral={"ee" * 28: 0})
    assert list(output.amount.multi_asset) == [
        ScriptHash(bytes.fromhex(_policy(LOAN_POLICY))),
    ]
    with pytest.raises(ValueError, match="negative"):
        _loan_output(collateral={"ee" * 28: -1})


# --- health reference set ----------------------------------------------------


def _feed_ref(inputs, underlying):
    return utxo_from_info(inputs.feeds[underlying].info).out_ref


def _state_ref(inputs, market_id):
    return utxo_from_info(inputs.states[market_id].info).out_ref


def test_a_wash_borrow_references_one_record_and_one_feed():
    borrow = _records("Ada")
    q_ada = borrow.market.qtoken_policy
    inputs = _health([q_ada], borrow)
    refs = health_reference_set(
        [q_ada, q_ada],
        borrow_market_id=borrow.market.market_id,
        borrow_underlying=borrow.market.underlying,
        inputs=inputs,
    )
    assert [u.out_ref for u in refs] == [
        utxo_from_info(inputs.cparams[q_ada].info).out_ref,
        _feed_ref(inputs, "lovelace"),
    ]
    assert _state_ref(inputs, borrow.market.market_id) not in [u.out_ref for u in refs]


def test_a_cross_market_loan_references_each_collateral_state_and_feed():
    deployment = default_deployment()
    borrow = _records("Ada")
    djed = deployment.market_by_name("DJED")
    units = [djed.qtoken_policy, borrow.market.qtoken_policy]
    inputs = _health(units, borrow)
    refs = health_reference_set(
        units,
        borrow_market_id=borrow.market.market_id,
        borrow_underlying=borrow.market.underlying,
        inputs=inputs,
    )
    assert {u.out_ref for u in refs} == {
        utxo_from_info(inputs.cparams[djed.qtoken_policy].info).out_ref,
        utxo_from_info(inputs.cparams[borrow.market.qtoken_policy].info).out_ref,
        _state_ref(inputs, djed.market_id),
        _feed_ref(inputs, "lovelace"),
        _feed_ref(inputs, djed.underlying),
    }
    assert len(refs) == 5


def test_a_unit_without_a_record_is_refused():
    borrow = _records("Ada")
    inputs = _health([borrow.market.qtoken_policy], borrow)
    with pytest.raises(UnsupportedCollateralError) as info:
        health_reference_set(
            ["ee" * 28],
            borrow_market_id=borrow.market.market_id,
            borrow_underlying=borrow.market.underlying,
            inputs=inputs,
        )
    assert info.value.unit == "ee" * 28


def test_a_missing_feed_is_refused():
    borrow = _records("Ada")
    q_ada = borrow.market.qtoken_policy
    inputs = _health([q_ada], borrow)
    no_feeds = HealthInputs(cparams=inputs.cparams, states=inputs.states, feeds={})
    with pytest.raises(MissingFeedError) as info:
        health_reference_set(
            [q_ada],
            borrow_market_id=borrow.market.market_id,
            borrow_underlying=borrow.market.underlying,
            inputs=no_feeds,
        )
    assert info.value.asset == "lovelace"


# --- reference hygiene -------------------------------------------------------


def _hygiene_builder(*utxos):
    tx_builder = _builder()
    for utxo in utxos:
        tx_builder.reference_inputs.add(to_pycardano_utxo(utxo))
    return tx_builder


def _feed_elsewhere():
    utxo = _record_utxo("oracle")
    return dataclasses.replace(utxo, address=_wallet().address)


def test_hygiene_accepts_scripts_records_and_a_feed_at_any_address():
    tx_builder = _hygiene_builder(
        _script(ACTION_SCRIPT),
        _record_utxo("market_params"),
        _record_utxo("collateral_params"),
        _record_utxo("market_state"),
        _feed_elsewhere(),
    )
    check_reference_hygiene(tx_builder, deployment=default_deployment())


@pytest.mark.parametrize("kind", ["wallet", "loan", "fake_feed", "two_records"])
def test_hygiene_refuses_anything_else(kind):
    if kind == "wallet":
        foreign = _wallet(3)
    elif kind == "loan":
        foreign = _record_utxo("loan")
    elif kind == "fake_feed":
        foreign = dataclasses.replace(_feed_elsewhere(), datum="d87980")
    else:
        params = _record_utxo("market_params")
        state = _record_utxo("market_state")
        foreign = dataclasses.replace(params, assets=params.assets + state.assets)
    tx_builder = _hygiene_builder(_script(ACTION_SCRIPT), foreign)
    with pytest.raises(ForeignReferenceInputError) as info:
        check_reference_hygiene(tx_builder, deployment=default_deployment())
    assert info.value.out_ref == _text(foreign.out_ref)


def test_hygiene_refuses_a_bare_transaction_input():
    tx_builder = _builder()
    tx_builder.reference_inputs.add(
        TransactionInput(TransactionId(bytes.fromhex(WALLET_TX)), 9),
    )
    with pytest.raises(ForeignReferenceInputError) as info:
        check_reference_hygiene(tx_builder, deployment=default_deployment())
    assert info.value.out_ref == f"{WALLET_TX}#9"


# --- owner signer ------------------------------------------------------------


def test_the_owner_signs_once():
    tx_builder = _builder()
    add_owner_signer(tx_builder, OWNER)
    add_owner_signer(tx_builder, OWNER)
    assert tx_builder.required_signers == [VerificationKeyHash(bytes.fromhex(OWNER))]


# --- finish_loan_flow --------------------------------------------------------


def _finish(tx_builder, *, collateral_in, collateral_out, debt, borrow=0, repay=0):
    records = _records("Ada")
    units = [u for u, q in (collateral_out or {}).items() if q > 0]
    finish_loan_flow(
        tx_builder,
        records=records,
        script_refs=_script_refs(),
        health=_health(units, records) if collateral_out is not None else None,
        owner=OWNER,
        collateral_in=collateral_in,
        collateral_out=collateral_out,
        debt=debt,
        borrow=borrow,
        repay=repay,
        deployment=default_deployment(),
    )


def _q_ada():
    return default_deployment().market_by_name("Ada").qtoken_policy


def test_a_close_signs_and_withdraws_without_a_health_set():
    tx_builder = _builder()
    _finish(tx_builder, collateral_in={_q_ada(): 10}, collateral_out=None, debt=0)
    assert tx_builder.required_signers == [VerificationKeyHash(bytes.fromhex(OWNER))]
    assert _withdrawal_redeemers(tx_builder) == ["00"]
    assert _reference_refs(tx_builder) == {(SCRIPT_TX, BORROW_STAKING)}


def test_a_healthy_borrow_references_its_health_set():
    tx_builder = _builder()
    q_ada = _q_ada()
    _finish(
        tx_builder,
        collateral_in={},
        collateral_out={q_ada: 10**12},
        debt=10**6,
        borrow=10**6,
    )
    records = _records("Ada")
    inputs = _health([q_ada], records)
    assert _reference_refs(tx_builder) == {
        (SCRIPT_TX, BORROW_STAKING),
        utxo_from_info(inputs.cparams[q_ada].info).out_ref,
        _feed_ref(inputs, "lovelace"),
    }
    assert _withdrawal_redeemers(tx_builder) == ["00"]


def test_an_unhealthy_borrow_is_refused_before_the_builder_changes():
    tx_builder = _builder()
    with pytest.raises(UnhealthyLoanError) as info:
        _finish(
            tx_builder,
            collateral_in={},
            collateral_out={_q_ada(): 10**6},
            debt=10**15,
            borrow=10**15,
        )
    assert info.value.required > info.value.available
    assert not tx_builder.reference_inputs
    assert not tx_builder.required_signers
    assert not tx_builder.withdrawals


def test_the_health_check_uses_the_exact_inclusive_rule():
    records = _records("Ada")
    q_ada = records.market.qtoken_policy
    inputs = _health([q_ada], records)
    feed = inputs.feeds["lovelace"]
    price = Fraction(feed.usd_num, feed.usd_den)
    cparams = inputs.cparams[q_ada].datum
    rate = Fraction(*inputs.states[records.market.market_id].datum.qtoken_rate)
    allowed = (
        Fraction(*records.params_datum.max_ltv)
        * 10**9
        * rate
        * price
        * Fraction(*cparams.weight)
    )
    largest = int(allowed / price)
    _finish(
        _builder(),
        collateral_in={},
        collateral_out={q_ada: 10**9},
        debt=largest,
        borrow=largest,
    )
    with pytest.raises(UnhealthyLoanError):
        _finish(
            _builder(),
            collateral_in={},
            collateral_out={q_ada: 10**9},
            debt=largest + 1,
            borrow=largest + 1,
        )


def test_a_repay_with_unchanged_collateral_skips_the_health_check():
    tx_builder = _builder()
    q_ada = _q_ada()
    _finish(
        tx_builder,
        collateral_in={q_ada: 10**6},
        collateral_out={q_ada: 10**6},
        debt=10**15,
        repay=1,
    )
    assert len(_reference_refs(tx_builder)) == 3


def test_a_kept_loan_needs_health_inputs():
    records = _records("Ada")
    with pytest.raises(ValueError, match="health"):
        finish_loan_flow(
            _builder(),
            records=records,
            script_refs=_script_refs(),
            health=None,
            owner=OWNER,
            collateral_in={},
            collateral_out={_q_ada(): 1},
            debt=1,
            borrow=1,
            repay=0,
            deployment=default_deployment(),
        )


def test_a_loan_flow_with_a_foreign_reference_is_refused():
    tx_builder = _hygiene_builder(_wallet(4))
    with pytest.raises(ForeignReferenceInputError):
        _finish(tx_builder, collateral_in={}, collateral_out=None, debt=0)


# --- every script the legs add is PlutusV2 (R9) ------------------------------


def _all_legs():
    refs = _script_refs()
    tx_builder = _builder()
    snapshot = _Snapshot()
    add_action_leg(
        tx_builder,
        action=_action(refs.action),
        deltas=ActionDeltas(principal=5_000_000),
        script_ref=refs.action,
        references=[_record_utxo("market_params"), _record_utxo("market_state")],
        underlying="lovelace",
    )
    add_loan_input(
        tx_builder,
        loan=_loan(refs.loan_validator),
        script_ref=refs.loan_validator,
    )
    add_loan_token_change(
        tx_builder,
        policy_ref=refs.loan_policy,
        loan_policy=_policy(LOAN_POLICY),
        current_name="00",
        new_name=flip_name("00"),
    )
    add_qtoken_leg(
        tx_builder,
        policy_ref=refs.qtoken_policy,
        qtoken_policy=_policy(QTOKEN_POLICY),
        quantity=-7,
    )
    add_staking_withdrawal(tx_builder, script_refs=refs, role=StakingRole.BORROW)
    add_funding(tx_builder, snapshot, [_wallet()])
    tx_builder.add_output(
        _loan_output(previous=3_000_000, collateral={"ee" * 28: 1_000}),
    )
    return tx_builder


def test_every_script_the_legs_add_is_plutus_v2():
    tx_builder = _all_legs()
    scripts = [
        *tx_builder._reference_scripts,
        *tx_builder._inputs_to_scripts.values(),
        *(s for s, _ in tx_builder._minting_script_to_redeemers),
        *(s for s, _ in tx_builder._withdrawal_script_to_redeemers),
    ]
    assert len(scripts) == 10
    assert all(isinstance(s, PlutusV2Script) for s in scripts)


def _data_hex(data):
    return data.cbor.hex() if isinstance(data, RawCBOR) else data.to_cbor_hex()


def test_the_legs_assemble_into_one_transaction():
    tx_builder = _all_legs()
    cbor = assemble_unsigned(tx_builder)
    assert cbor
    redeemers = sorted(
        (r.tag.name, r.index, _data_hex(r.data)) for r in tx_builder._redeemer_list
    )
    tags = [tag for tag, _, _ in redeemers]
    assert tags == ["MINT", "MINT", "SPEND", "SPEND", "WITHDRAWAL"]
    assert {data for _, _, data in redeemers} == {"d87980", "00"}
    assert len(tx_builder.reference_inputs) == 7
