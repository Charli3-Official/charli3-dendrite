"""FluidTokens V4 change collateral: add or remove collateral on one or several loans.

Each loan continues with the same address and datum and its collateral set to the new
amount; the loan must stay at or under its liquidation LTV at the new amount, priced by
signed oracle prices for every side that is not ADA. Only liquidation-mode loans can
change their collateral.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field
from fractions import Fraction
from typing import TYPE_CHECKING

from pycardano import Address
from pycardano import RawCBOR
from pycardano import TransactionBuilder
from pycardano import TransactionOutput

from charli3_dendrite.lending.fluidtokens.transactions.borrow_terms import (
    BORROW_VALIDITY_SLOTS,
)
from charli3_dendrite.lending.fluidtokens.transactions.borrow_terms import signed_window
from charli3_dendrite.lending.fluidtokens.transactions.utxos import Utxo
from charli3_dendrite.lending.fluidtokens.transactions.utxos import script_ref_by_hash
from charli3_dendrite.lending.fluidtokens.transactions.utxos import utxo_from_dict
from charli3_dendrite.lending.fluidtokens.transactions.utxos import utxo_value
from charli3_dendrite.lending.fluidtokens_v4 import constants as c
from charli3_dendrite.lending.fluidtokens_v4.state import collateral_asset_unit
from charli3_dendrite.lending.fluidtokens_v4.transactions.common import is_policy_wide
from charli3_dendrite.lending.fluidtokens_v4.transactions.common import ledger_order
from charli3_dendrite.lending.fluidtokens_v4.transactions.common import min_ada
from charli3_dendrite.lending.fluidtokens_v4.transactions.common import out_ref_of
from charli3_dendrite.lending.fluidtokens_v4.transactions.loan_action import (
    LoanActionSnapshot,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.loan_action import (
    LoanPosition,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.loan_action import (
    add_bond_outputs,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.loan_action import (
    add_loan_action,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.loan_action import (
    finish_loan_action,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.loan_terms import (
    min_collateral,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.oracle import OracleWitness
from charli3_dendrite.lending.fluidtokens_v4.transactions.oracle import add_oracles
from charli3_dendrite.lending.fluidtokens_v4.transactions.oracle import (
    oracles_from_capture,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.oracle import witness_for
from charli3_dendrite.lending.fluidtokens_v4.transactions.redeemers import (
    ActionTypeChangeCollateral,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.redeemers import (
    ChangeCollateralData,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.redeemers import (
    LoanChangeCollateralActionWithdrawRedeemer,
)
from charli3_dendrite.utility import slot_to_posix_ms

if TYPE_CHECKING:
    from charli3_dendrite.backend.backend_base import AbstractBackend
    from charli3_dendrite.lending.fluidtokens.oracles.fluid_api import (
        FluidTokensProviderClient,
    )

# An ADA side is priced 1:1 without reading its oracle reference input; any valid
# reference-input index serves, and FluidTokens' own transactions use 0.
ADA_ORACLE_PLACEHOLDER = 0


@dataclass
class ChangeCollateralPosition(LoanPosition):
    """A loan whose collateral changes to ``new_collateral_amount``."""

    new_collateral_amount: int

    @property
    def collateral_unit(self) -> str:
        """The loan's collateral unit."""
        return collateral_asset_unit(self.loan_datum.collateral)


@dataclass
class ChangeCollateralSnapshot(LoanActionSnapshot):
    """Everything a change collateral of one or several loans needs."""

    positions: Sequence[ChangeCollateralPosition]
    oracles: list[OracleWitness] = field(default_factory=list)

    def collateral_oracle(self, position: LoanPosition) -> OracleWitness | None:
        """The witness pricing the loan's collateral (None for ADA)."""
        collateral = position.loan_datum.collateral
        if not collateral.policy_id:
            return None
        return witness_for(self.oracles, collateral.oracle_token_asset)

    def principal_oracle(self, position: LoanPosition) -> OracleWitness | None:
        """The witness pricing the loan's principal (None for ADA)."""
        datum = position.loan_datum
        if not datum.principal_asset.policy_id:
            return None
        return witness_for(self.oracles, datum.principal_oracle_asset)

    def price(self, oracle: OracleWitness | None) -> Fraction:
        """Lovelace per smallest unit (1 for ADA)."""
        return Fraction(1) if oracle is None else oracle.price

    @classmethod
    def from_capture(cls, fix: dict) -> ChangeCollateralSnapshot:
        """Rebuild the snapshot of a captured mainnet change collateral."""
        inputs = [utxo_from_dict(u) for u in fix["inputs"]]
        refs = [utxo_from_dict(u) for u in fix["ref_inputs"]]
        action = next(
            LoanChangeCollateralActionWithdrawRedeemer.from_cbor(r["cbor"])
            for r in fix["redeemers"]
            if r["script_hash"] == c.LOAN_CHANGE_COLLATERAL_ACTION_SKH
        )
        loans = ledger_order(
            u for u in inputs if u.holds_policy(c.LOAN_POLICY) and u.datum
        )
        positions = [
            ChangeCollateralPosition(
                loan=loan,
                borrower_bond=next(
                    u
                    for u in inputs
                    if u.holds(c.BORROWER_BOND_POLICY, data.loan_id.hex())
                ),
                new_collateral_amount=data.new_collateral_amount,
            )
            for loan, data in zip(loans, action.actions_for_each_input)
        ]
        used = {u.out_ref for p in positions for u in (p.loan, p.borrower_bond)}
        return cls(
            positions=positions,
            funding=[u for u in inputs if u.out_ref not in used],
            config=next(
                u for u in refs if u.holds(c.CONFIG_NFT_POLICY, c.CONFIG_NFT_NAME)
            ),
            loan_spend_script_ref=script_ref_by_hash(refs, c.LOAN_SPEND_SKH),
            loan_policy_script_ref=script_ref_by_hash(refs, c.LOAN_POLICY),
            action_script_ref=script_ref_by_hash(
                refs,
                c.LOAN_CHANGE_COLLATERAL_ACTION_SKH,
            ),
            valid_from=fix["invalid_before"],
            valid_to=fix["invalid_hereafter"],
            oracles=oracles_from_capture(
                fix,
                refs,
                exclude={c.LOAN_POLICY, c.LOAN_CHANGE_COLLATERAL_ACTION_SKH},
            ),
        )

    @classmethod
    def from_backend(
        cls,
        backend: AbstractBackend,
        *,
        changes: Sequence[tuple[tuple[str, int], int]],
        borrower_address: str,
        oracles: Sequence[OracleWitness] | None = None,
        provider: FluidTokensProviderClient | None = None,
        funding: Sequence[Utxo] | None = None,
        valid_from: int | None = None,
        valid_to: int | None = None,
        allow_spent: bool = False,
    ) -> ChangeCollateralSnapshot:
        """Resolve a collateral change for each ``(loan out-ref, new amount)``.

        Every side that is not ADA needs a signed price: pass ``oracles``, or leave
        them out to fetch each from the FluidTokens registry through ``provider``.
        The window defaults to the tip, inside every signed price, and each new amount
        is checked against the least the loan may keep at the window's upper bound.
        """
        from charli3_dendrite.lending.fluidtokens.oracles.fluid_api import (
            FluidTokensProviderClient,
        )
        from charli3_dendrite.lending.fluidtokens_v4.transactions.resolve import (
            resolve_loan_action,
        )
        from charli3_dendrite.lending.transactions.infra import current_slot

        context = resolve_loan_action(
            backend,
            action="change_collateral",
            loan_out_refs=[out_ref for out_ref, _ in changes],
            borrower_address=borrower_address,
            funding=funding,
            allow_spent=allow_spent,
        )
        positions = [
            ChangeCollateralPosition(
                loan=p.loan,
                borrower_bond=p.borrower_bond,
                new_collateral_amount=amount,
            )
            for p, (_, amount) in zip(context.positions, changes)
        ]
        snapshot = cls(
            positions=positions,
            funding=context.funding,
            config=context.config,
            loan_spend_script_ref=context.loan_spend_script_ref,
            loan_policy_script_ref=context.loan_policy_script_ref,
            action_script_ref=context.action_script_ref,
            valid_from=0,
            valid_to=0,
            oracles=list(oracles or []),
        )
        for position in positions:
            datum = position.loan_datum
            needed = [(datum.collateral.policy_id, datum.collateral.oracle_token_asset)]
            needed.append(
                (datum.principal_asset.policy_id, datum.principal_oracle_asset),
            )
            for policy, token in needed:
                if not policy or any(w.serves(token) for w in snapshot.oracles):
                    continue
                if oracles is not None:
                    raise ValueError("no oracle witness prices this loan's assets")
                provider = provider or FluidTokensProviderClient()
                unit = (
                    position.collateral_unit
                    if token == datum.collateral.oracle_token_asset
                    else datum.principal_asset.unit()
                )
                bundle = provider.fetch_oracle_witness(collateral_unit=unit)
                snapshot.oracles.append(OracleWitness.from_bundle(backend, bundle))
        tip = current_slot(backend) if valid_from is None or valid_to is None else 0
        snapshot.valid_from, snapshot.valid_to = signed_window(
            [w.reward for w in snapshot.oracles],
            valid_from=valid_from,
            valid_to=valid_to,
            tip=tip,
            cap=BORROW_VALIDITY_SLOTS,
        )
        for position in positions:
            minimum = min_collateral(
                position.loan_datum,
                valid_to_ms=slot_to_posix_ms(snapshot.valid_to),
                principal_price=snapshot.price(snapshot.principal_oracle(position)),
                collateral_price=snapshot.price(snapshot.collateral_oracle(position)),
            )
            if position.new_collateral_amount < minimum:
                raise ValueError(
                    f"loan {position.out_ref} must keep at least {minimum} collateral, "
                    f"not {position.new_collateral_amount}",
                )
        return snapshot


def build_change_collateral(
    tx_builder: TransactionBuilder,
    *,
    snapshot: ChangeCollateralSnapshot,
) -> None:
    """Add a change of collateral on every position of ``snapshot`` to ``tx_builder``.

    Added collateral comes from the funding inputs and removed collateral lands in the
    caller's change; the caller balances, signs and submits.

    Everything else the caller wants among the transaction's inputs, reference
    inputs, mints and withdrawals must be added before this call, which fills their
    indexes last; outputs may be added after, except to the loan and asset-manager
    scripts. Discard the builder if this raises.
    """
    positions: list[ChangeCollateralPosition] = snapshot.ordered_positions  # type: ignore[assignment]
    for position in positions:
        if position.new_collateral_amount <= 0:
            raise ValueError("a loan must keep a positive collateral amount")
    loans = [_continuing_loan(p) for p in positions]
    data = [
        ChangeCollateralData(
            borrower_bond_output_index=0,
            new_collateral_amount=p.new_collateral_amount,
            loan_id=p.loan_id,
            collateral_oracle_ref_input_index=ADA_ORACLE_PLACEHOLDER,
            principal_oracle_ref_input_index=ADA_ORACLE_PLACEHOLDER,
        )
        for p in positions
    ]
    action = LoanChangeCollateralActionWithdrawRedeemer(
        config_ref_input_index=0,
        actions_for_each_input=data,
    )
    dispatch = add_loan_action(
        tx_builder,
        snapshot=snapshot,
        action_type=ActionTypeChangeCollateral(),
        action=action,
    )
    collateral_oracles = [snapshot.collateral_oracle(p) for p in positions]
    principal_oracles = [snapshot.principal_oracle(p) for p in positions]
    add_oracles(
        tx_builder,
        [o for o in collateral_oracles + principal_oracles if o is not None],
    )

    for loan in loans:
        tx_builder.add_output(loan)
    bond_indexes = add_bond_outputs(tx_builder, positions)
    refs = finish_loan_action(
        tx_builder,
        snapshot=snapshot,
        dispatch=dispatch,
        action=action,
    )
    for entry, index, coll, prin in zip(
        data,
        bond_indexes,
        collateral_oracles,
        principal_oracles,
    ):
        entry.borrower_bond_output_index = index
        if coll is not None:
            entry.collateral_oracle_ref_input_index = refs[out_ref_of(coll.feed)]
        if prin is not None:
            entry.principal_oracle_ref_input_index = refs[out_ref_of(prin.feed)]


def _continuing_loan(position: ChangeCollateralPosition) -> TransactionOutput:
    """The loan with its collateral set to the new amount, all else unchanged.

    An ADA collateral is the loan's ADA, so it must cover the output's minimum ADA. A
    token collateral keeps the loan's ADA, topped up to the output's minimum when the
    new amount needs more (the contract ignores a token-collateral loan's ADA).
    """
    loan = position.loan
    unit = position.collateral_unit
    if is_policy_wide(position.loan_datum.collateral):
        raise NotImplementedError("policy-wide collateral is not supported")
    lovelace = loan.lovelace
    assets = list(loan.assets)
    if unit == "lovelace":
        lovelace = position.new_collateral_amount
    else:
        assets = [
            (p, n, position.new_collateral_amount if p + n == unit else q)
            for p, n, q in assets
        ]
    datum = RawCBOR(bytes.fromhex(loan.datum or ""))
    output = TransactionOutput(
        Address.decode(loan.address),
        utxo_value(lovelace, assets),
        datum=datum,
    )
    floor = min_ada(output)
    if lovelace >= floor:
        return output
    if unit == "lovelace":
        raise ValueError(
            f"loan {position.out_ref} must keep at least {floor} lovelace of ADA "
            f"collateral, its output's minimum ADA, not {lovelace}",
        )
    return TransactionOutput(
        Address.decode(loan.address),
        utxo_value(floor, assets),
        datum=datum,
    )
