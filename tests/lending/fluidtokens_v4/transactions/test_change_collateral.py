"""Offline: forward-built V4 collateral changes reproduce the captured ones."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

import pytest
from pycardano import TransactionOutput

from charli3_dendrite.lending.fluidtokens.datums import Asset
from charli3_dendrite.lending.fluidtokens.oracles.fluid_api import (
    FluidTokensProviderClient,
)
from charli3_dendrite.lending.fluidtokens.transactions.utxos import Utxo
from charli3_dendrite.lending.fluidtokens_v4.datums import LoanDatum
from charli3_dendrite.lending.fluidtokens_v4.transactions import resolve
from charli3_dendrite.lending.fluidtokens_v4.transactions.change_collateral import (
    ChangeCollateralPosition,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.change_collateral import (
    ChangeCollateralSnapshot,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.change_collateral import (
    build_change_collateral,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.common import min_ada
from charli3_dendrite.lending.fluidtokens_v4.transactions.loan_action import (
    LoanPosition,
)
import cbor2
from pycardano import RawPlutusData
from tests.lending.fluidtokens_v4.records import NO_ORACLE_TOKENS
from tests.lending.fluidtokens_v4.transactions.replay import ORACLE_V2_POLICY
from tests.lending.fluidtokens_v4.transactions.replay import RegistrySession
from tests.lending.fluidtokens_v4.transactions.replay import at_minimum_ada
from tests.lending.fluidtokens_v4.transactions.replay import build
from tests.lending.fluidtokens_v4.transactions.replay import captured_output_view
from tests.lending.fluidtokens_v4.transactions.replay import captured_redeemers
from tests.lending.fluidtokens_v4.transactions.replay import fixture
from tests.lending.fluidtokens_v4.transactions.replay import other_oracle
from tests.lending.fluidtokens_v4.transactions.replay import output_view
from tests.lending.fluidtokens_v4.transactions.replay import redeemers
from tests.lending.fluidtokens_v4.transactions.replay import registry_backend
from tests.lending.fluidtokens_v4.transactions.replay import registry_entry


@pytest.mark.parametrize(
    ("name", "loans"),
    [("change_collateral_single", 1), ("change_collateral_multi", 3)],
)
def test_change_collateral_is_byte_exact(name: str, loans: int) -> None:
    fix = fixture(name)
    built = build(
        build_change_collateral,
        ChangeCollateralSnapshot.from_capture(fix),
        slot=fix["invalid_before"],
    )
    assert redeemers(built.tx) == captured_redeemers(fix)
    # Continuing loans, then the returned bonds; the wallet change follows.
    outputs = built.tx.transaction_body.outputs
    assert [output_view(o) for o in outputs[: 2 * loans]] == [
        captured_output_view(o) for o in fix["outputs"][: 2 * loans]
    ]
    assert built.tx.transaction_body.mint is None


def test_a_loan_must_keep_some_collateral() -> None:
    fix = fixture("change_collateral_single")
    snapshot = ChangeCollateralSnapshot.from_capture(fix)
    snapshot.positions[0].new_collateral_amount = 0
    with pytest.raises(ValueError, match="positive collateral"):
        build(build_change_collateral, snapshot, slot=fix["invalid_before"])


def test_policy_wide_collateral_is_refused() -> None:
    fix = fixture("change_collateral_single")
    snapshot = ChangeCollateralSnapshot.from_capture(fix)
    (position,) = snapshot.positions
    datum = position.loan_datum
    none = RawPlutusData(cbor2.CBORTag(122, []))  # Option<AssetName> None
    policy_wide = replace(
        datum,
        collateral=replace(datum.collateral, maybe_asset_name=none),
    )
    position.loan = replace(position.loan, datum=policy_wide.to_cbor_hex())
    with pytest.raises(NotImplementedError, match="policy-wide"):
        build(build_change_collateral, snapshot, slot=fix["invalid_before"])


def _with_collateral(position: ChangeCollateralPosition, amount: int) -> Utxo:
    """The position's loan holding ``amount`` of its collateral."""
    unit = position.collateral_unit
    return replace(
        position.loan,
        assets=[
            (p, n, amount if p + n == unit else q) for p, n, q in position.loan.assets
        ],
    )


def _continuing(snapshot: ChangeCollateralSnapshot, fix: dict) -> TransactionOutput:
    return build(
        build_change_collateral,
        snapshot,
        slot=fix["invalid_before"],
    ).tx.transaction_body.outputs[0]


def test_a_wider_token_collateral_tops_the_ada_up_to_the_minimum() -> None:
    fix = fixture("change_collateral_single")
    snapshot = ChangeCollateralSnapshot.from_capture(fix)
    (position,) = snapshot.positions
    (held,) = [
        q for p, n, q in position.loan.assets if p + n == position.collateral_unit
    ]
    assert held < 2**32
    position.loan = at_minimum_ada(position.loan)
    position.new_collateral_amount = 2**32
    loan = _continuing(snapshot, fix)
    # A 9-byte quantity where a 5-byte one was: four more bytes at 4,310 lovelace.
    assert loan.amount.coin == min_ada(loan) == position.loan.lovelace + 4 * 4_310


def test_a_token_collateral_change_never_lowers_the_ada() -> None:
    fix = fixture("change_collateral_single")
    snapshot = ChangeCollateralSnapshot.from_capture(fix)
    (position,) = snapshot.positions
    position.new_collateral_amount = 2**32
    assert _continuing(snapshot, fix).amount.coin == position.loan.lovelace
    position.loan = at_minimum_ada(_with_collateral(position, 2**32))
    position.new_collateral_amount = 2**31
    loan = _continuing(snapshot, fix)
    assert loan.amount.coin == position.loan.lovelace > min_ada(loan)


def test_an_ada_collateral_must_cover_the_minimum_ada() -> None:
    fix = fixture("change_collateral_single")
    snapshot = ChangeCollateralSnapshot.from_capture(fix)
    (position,) = snapshot.positions
    datum = position.loan_datum
    ada = replace(
        datum.collateral,
        policy_id=b"",
        maybe_asset_name=RawPlutusData(cbor2.CBORTag(121, [b""])),  # Some("")
    )
    unit = position.collateral_unit
    position.loan = replace(
        position.loan,
        datum=replace(datum, collateral=ada).to_cbor_hex(),
        assets=[(p, n, q) for p, n, q in position.loan.assets if p + n != unit],
    )
    floor = at_minimum_ada(position.loan).lovelace
    position.new_collateral_amount = floor - 1
    with pytest.raises(ValueError, match=f"at least {floor} lovelace"):
        build(build_change_collateral, snapshot, slot=fix["invalid_before"])
    position.new_collateral_amount = floor
    assert _continuing(snapshot, fix).amount.coin == floor


# A token principal and the units of the NFTs held by the feeds of its two oracles,
# one per oracle version.
_USDM = "c48cbb3d5e57ed56e276bc45f99ab39abe94e6cd7ac39fb402da47ad0014df105553444d"
_OTHER_ORACLE_POLICY = "ab" * 28


def _from_backend(
    monkeypatch: pytest.MonkeyPatch,
    capture: ChangeCollateralSnapshot,
    *,
    loan: Utxo | None = None,
    **overrides: object,
) -> ChangeCollateralSnapshot:
    """``from_backend`` of ``capture``'s one loan, or of ``loan`` in its place."""
    (position,) = capture.positions
    loan = loan or position.loan
    context = resolve.LoanActionContext(
        positions=[LoanPosition(loan, position.borrower_bond)],
        funding=capture.funding,
        config=capture.config,
        scripts=resolve.config_datum(capture.config),
        loan_spend_script_ref=capture.loan_spend_script_ref,
        loan_policy_script_ref=capture.loan_policy_script_ref,
        action_script_ref=capture.action_script_ref,
    )
    monkeypatch.setattr(resolve, "resolve_loan_action", lambda _backend, **_: context)
    kwargs: dict = {
        "changes": [(loan.out_ref, position.new_collateral_amount)],
        "borrower_address": position.borrower_bond.address,
        "funding": capture.funding,
        "valid_from": capture.valid_from,
        "valid_to": capture.valid_to,
    }
    return ChangeCollateralSnapshot.from_backend(object(), **(kwargs | overrides))  # type: ignore[arg-type]


def _unit(token: Asset) -> str:
    return token.policy_id.hex() + token.asset_name.hex()


def _with_datum(loan: Utxo, edit: Callable[[LoanDatum], LoanDatum]) -> Utxo:
    return replace(loan, datum=edit(LoanDatum.from_cbor(loan.datum)).to_cbor_hex())


@pytest.mark.parametrize("first", ["other-oracle", "named-oracle"])
def test_from_backend_prices_through_the_oracle_the_loan_names(
    monkeypatch: pytest.MonkeyPatch,
    first: str,
) -> None:
    # The registry lists a token once per oracle version; only the oracle the loan
    # names prices its collateral.
    fix = fixture("change_collateral_single")
    capture = ChangeCollateralSnapshot.from_capture(fix)
    (position,) = capture.positions
    (named,) = capture.oracles
    token = position.loan_datum.collateral.oracle_token_asset
    other, other_token = other_oracle(named, ORACLE_V2_POLICY)
    entries = [registry_entry(other, other_token), registry_entry(named, _unit(token))]
    if first == "named-oracle":
        entries.reverse()
    registry_backend(
        monkeypatch,
        [named.feed, named.script_ref, other.feed, other.script_ref],
    )
    snapshot = _from_backend(
        monkeypatch,
        capture,
        provider=FluidTokensProviderClient(session=RegistrySession(entries)),
    )
    witness = snapshot.collateral_oracle(snapshot.positions[0])
    assert witness is not None
    assert (witness.feed, witness.script_ref) == (named.feed, named.script_ref)
    body = build(
        build_change_collateral,
        snapshot,
        slot=fix["invalid_before"],
    ).tx.transaction_body
    refs = {(bytes(i.transaction_id).hex(), i.index) for i in body.reference_inputs}
    assert named.feed.out_ref in refs
    assert other.feed.out_ref not in refs


@pytest.mark.parametrize("first", ["other-oracle", "named-oracle"])
def test_from_backend_prices_the_principal_through_the_oracle_the_loan_names(
    monkeypatch: pytest.MonkeyPatch,
    first: str,
) -> None:
    # A token principal is priced too, through the oracle the loan names for it,
    # never through the collateral's oracle nor another version's oracle.
    fix = fixture("change_collateral_single")
    capture = ChangeCollateralSnapshot.from_capture(fix)
    (position,) = capture.positions
    (collateral,) = capture.oracles
    collateral_token = position.loan_datum.collateral.oracle_token_asset
    named, named_token = other_oracle(collateral, ORACLE_V2_POLICY)
    other, other_token = other_oracle(collateral, _OTHER_ORACLE_POLICY)
    loan = _with_datum(
        position.loan,
        lambda d: replace(
            d,
            principal_asset=Asset(bytes.fromhex(_USDM[:56]), bytes.fromhex(_USDM[56:])),
            principal_oracle_asset=Asset(
                bytes.fromhex(named_token[:56]),
                bytes.fromhex(named_token[56:]),
            ),
        ),
    )
    principal = [
        registry_entry(other, other_token, token=_USDM),
        registry_entry(named, named_token, token=_USDM),
    ]
    if first == "named-oracle":
        principal.reverse()
    registry_backend(
        monkeypatch,
        [u for w in (collateral, named, other) for u in (w.feed, w.script_ref)],
    )
    entries = [*principal, registry_entry(collateral, _unit(collateral_token))]
    snapshot = _from_backend(
        monkeypatch,
        capture,
        loan=loan,
        changes=[(loan.out_ref, 10**15)],
        provider=FluidTokensProviderClient(session=RegistrySession(entries)),
    )
    (resolved,) = snapshot.positions
    witness = snapshot.principal_oracle(resolved)
    assert witness is not None
    assert (witness.feed, witness.script_ref) == (named.feed, named.script_ref)
    assert snapshot.collateral_oracle(resolved) is not None
    assert other.feed not in [w.feed for w in snapshot.oracles]


class _NoRegistry:
    """A provider that fails the test if the registry is asked for a price."""

    def fetch_oracle_witness(self, **_: object) -> None:
        raise AssertionError("the price registry must not be called")


@pytest.mark.parametrize(
    "oracle_token",
    list(NO_ORACLE_TOKENS.values()),
    ids=list(NO_ORACLE_TOKENS),
)
@pytest.mark.parametrize("side", ["collateral", "principal"])
def test_from_backend_refuses_a_priced_side_that_names_no_oracle(
    monkeypatch: pytest.MonkeyPatch,
    oracle_token: Asset,
    side: str,
) -> None:
    # No feed can hold the side's oracle token, so no signed price of that side
    # exists and the registry is not asked for one.
    fix = fixture("change_collateral_single")
    capture = ChangeCollateralSnapshot.from_capture(fix)
    (position,) = capture.positions
    if side == "collateral":
        loan = _with_datum(
            position.loan,
            lambda d: replace(
                d,
                collateral=replace(d.collateral, oracle_token_asset=oracle_token),
            ),
        )
        oracles = None
    else:
        loan = _with_datum(
            position.loan,
            lambda d: replace(
                d,
                principal_asset=Asset(
                    bytes.fromhex(_USDM[:56]),
                    bytes.fromhex(_USDM[56:]),
                ),
                principal_oracle_asset=oracle_token,
            ),
        )
        oracles = capture.oracles
    with pytest.raises(ValueError, match=f"names no oracle for its {side}"):
        _from_backend(
            monkeypatch,
            capture,
            loan=loan,
            oracles=oracles,
            provider=_NoRegistry(),
        )
