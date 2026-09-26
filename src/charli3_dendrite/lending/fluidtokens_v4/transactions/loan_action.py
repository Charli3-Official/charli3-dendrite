"""The shape every V4 loan action (repay, change collateral, recast) shares.

Each spends one or more loan UTxOs with an empty redeemer and proves the borrower by
putting each loan's borrower bond in an output. A loan dispatch withdraw names the
action, and the action's own withdraw script checks every spent loan against one
per-loan entry of its redeemer, in the ledger's input order.

The action script reads a loan's continuing output and its lender payment by position
among the outputs to the loan and asset-manager scripts, and its bond by absolute
output index. So these builders refuse a transaction that already pays either script,
and fill the bond indexes only once every output is in place.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pycardano import Address
from pycardano import Asset as PyAsset
from pycardano import AssetName
from pycardano import MultiAsset
from pycardano import RawCBOR
from pycardano import Redeemer
from pycardano import ScriptHash
from pycardano import TransactionBuilder
from pycardano import TransactionOutput
from pycardano import VerificationKeyHash

from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.lending.fluidtokens.transactions._common import ref_index
from charli3_dendrite.lending.fluidtokens.transactions.borrow_terms import (
    min_output_lovelace,
)
from charli3_dendrite.lending.fluidtokens.transactions.utxos import Utxo
from charli3_dendrite.lending.fluidtokens.transactions.utxos import loan_id_from_out_ref
from charli3_dendrite.lending.fluidtokens.transactions.utxos import ogmios_entry
from charli3_dendrite.lending.fluidtokens.transactions.utxos import to_pycardano_utxo
from charli3_dendrite.lending.fluidtokens.transactions.utxos import utxo_value
from charli3_dendrite.lending.fluidtokens_v4 import constants as c
from charli3_dendrite.lending.fluidtokens_v4.datums import Asset
from charli3_dendrite.lending.fluidtokens_v4.datums import AssetManagerDatumWithToken
from charli3_dendrite.lending.fluidtokens_v4.datums import LoanDatum
from charli3_dendrite.lending.fluidtokens_v4.datums import TxOutRef
from charli3_dendrite.lending.fluidtokens_v4.transactions.common import (
    add_zero_withdrawals,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.common import min_ada
from charli3_dendrite.lending.fluidtokens_v4.transactions.common import out_ref_of
from charli3_dendrite.lending.fluidtokens_v4.transactions.common import script_hash_of
from charli3_dendrite.lending.fluidtokens_v4.transactions.common import (
    withdraw_redeemer_position,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.redeemers import (
    AssetManagerMintRedeemer,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.redeemers import BoolTrue
from charli3_dendrite.lending.fluidtokens_v4.transactions.redeemers import (
    LoanMintRedeemer,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.redeemers import (
    LoanSpendRedeemer,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.redeemers import (
    LoanWithdrawRedeemer,
)
from charli3_dendrite.lending.transactions.snapshot import PoolActionSnapshot
from charli3_dendrite.lending.units import constr
from charli3_dendrite.utility import asset_to_value

if TYPE_CHECKING:
    from pycardano import PlutusData

# Plutus ``Bool`` constructor alternative of ``True``.
_BOOL_TRUE = 1


def loan_nft_name(loan: Utxo) -> bytes:
    """The name of the one loan NFT a loan UTxO holds."""
    names = [n for p, n, q in loan.assets if p == c.LOAN_POLICY and q == 1]
    if len(names) != 1:
        raise ValueError("loan UTxO must hold exactly one loan NFT")
    return bytes.fromhex(names[0])


@dataclass
class LoanPosition:
    """A loan UTxO and the UTxO holding its borrower bond."""

    loan: Utxo
    borrower_bond: Utxo

    @property
    def out_ref(self) -> tuple[str, int]:
        """The loan UTxO's out-ref."""
        if self.loan.out_ref is None:
            raise ValueError("loan UTxO is missing its out-ref")
        return self.loan.out_ref

    @property
    def loan_datum(self) -> LoanDatum:
        """The loan's datum."""
        if self.loan.datum is None:
            raise ValueError("loan UTxO is missing its datum")
        return LoanDatum.from_cbor(self.loan.datum)

    @property
    def loan_id(self) -> bytes:
        """The loan NFT name, shared by both bonds."""
        return loan_nft_name(self.loan)

    @property
    def receipt_name(self) -> bytes:
        """The repayment-receipt name: the hash of the loan's out-ref."""
        return loan_id_from_out_ref(self.out_ref)


@dataclass
class LoanActionSnapshot(PoolActionSnapshot):
    """What every loan action resolves: the loans, the scripts and the window."""

    positions: Sequence[LoanPosition]
    funding: list[Utxo]
    config: Utxo
    loan_spend_script_ref: Utxo
    loan_policy_script_ref: Utxo
    action_script_ref: Utxo
    valid_from: int
    valid_to: int

    @property
    def ordered_positions(self) -> list[LoanPosition]:
        """The positions in the order the ledger sorts their loan inputs."""
        return sorted(
            self.positions,
            key=lambda p: (bytes.fromhex(p.out_ref[0]), p.out_ref[1]),
        )


def add_loan_action(
    tx_builder: TransactionBuilder,
    *,
    snapshot: LoanActionSnapshot,
    action_type: PlutusData,
    action: PlutusData,
) -> LoanWithdrawRedeemer:
    """Spend the loans and bonds and add the dispatch and action withdrawals.

    Funding inputs are recorded for Ogmios evaluation; a funding UTxO that is one of
    the loans or bonds already spent is skipped. Returns the dispatch redeemer, whose
    config index :func:`finish_loan_action` fills.
    """
    loan_payees = (c.LOAN_SPEND_SKH, c.ASSET_MANAGER_SPEND_SKH)
    for output in tx_builder.outputs:
        payment = output.address.payment_part
        if isinstance(payment, ScriptHash) and payment.payload.hex() in loan_payees:
            raise ValueError(
                "a loan action must be the only payer of the loan and asset-manager "
                "scripts in its transaction",
            )
    if not snapshot.positions:
        raise ValueError("a loan action needs at least one loan")

    tx_builder.validity_start = snapshot.valid_from
    tx_builder.ttl = snapshot.valid_to

    bonds: dict[tuple[str, int], Utxo] = {}
    for position in snapshot.ordered_positions:
        tx_builder.add_script_input(
            to_pycardano_utxo(position.loan),
            script=to_pycardano_utxo(snapshot.loan_spend_script_ref),
            redeemer=Redeemer(LoanSpendRedeemer()),
        )
        bond = position.borrower_bond
        if not isinstance(
            Address.decode(bond.address).payment_part,
            VerificationKeyHash,
        ):
            raise NotImplementedError("only wallet-held borrower bonds are supported")
        if bond.out_ref is None:
            raise ValueError("borrower bond UTxO is missing its out-ref")
        bonds[bond.out_ref] = bond
    for bond in bonds.values():
        tx_builder.add_input(to_pycardano_utxo(bond))
        snapshot.add_actor_additional_utxo(ogmios_entry(bond))
    spent = {p.out_ref for p in snapshot.positions} | set(bonds)
    for funding in snapshot.funding:
        if funding.out_ref in spent:
            continue
        tx_builder.add_input(to_pycardano_utxo(funding))
        snapshot.add_actor_additional_utxo(ogmios_entry(funding))

    tx_builder.reference_inputs.add(to_pycardano_utxo(snapshot.config))
    dispatch = LoanWithdrawRedeemer(config_ref_input_index=0, action_type=action_type)
    tx_builder.add_withdrawal_script(
        to_pycardano_utxo(snapshot.loan_policy_script_ref),
        Redeemer(dispatch),
    )
    tx_builder.add_withdrawal_script(
        to_pycardano_utxo(snapshot.action_script_ref),
        Redeemer(action),
    )
    add_zero_withdrawals(
        tx_builder,
        [c.LOAN_POLICY, script_hash_of(snapshot.action_script_ref)],
    )
    return dispatch


def add_bond_outputs(
    tx_builder: TransactionBuilder,
    positions: Sequence[LoanPosition],
) -> list[int]:
    """Return every bond UTxO to its owner unchanged; one output index per position.

    Several loans may share one bond UTxO and so one output.
    """
    outputs: dict[tuple[str, int], int] = {}
    indexes = []
    for position in positions:
        bond = position.borrower_bond
        if bond.out_ref is None:
            raise ValueError("borrower bond UTxO is missing its out-ref")
        if bond.out_ref not in outputs:
            outputs[bond.out_ref] = len(tx_builder.outputs)
            tx_builder.add_output(
                TransactionOutput(
                    Address.decode(bond.address),
                    utxo_value(bond.lovelace, bond.assets),
                    datum=RawCBOR(bytes.fromhex(bond.datum)) if bond.datum else None,
                ),
            )
        indexes.append(outputs[bond.out_ref])
    return indexes


def continuing_loan_output(
    position: LoanPosition,
    datum: PlutusData,
) -> TransactionOutput:
    """The loan continued with ``datum`` and its value unchanged.

    The contract requires the continuing loan to keep the spent loan's value, so its
    ADA cannot be topped up: raises ``ValueError`` if the new datum makes the output
    need more ADA than the loan holds, which the ledger would reject.
    """
    loan = position.loan
    output = TransactionOutput(
        Address.decode(loan.address),
        utxo_value(loan.lovelace, loan.assets),
        datum=datum,
    )
    floor = min_ada(output)
    if loan.lovelace < floor:
        raise ValueError(
            f"loan {position.out_ref} holds {loan.lovelace} lovelace, below the "
            f"{floor} its continuing output needs; the contract requires the loan's "
            "value unchanged, so it cannot be topped up",
        )
    return output


def asset_manager_output(
    position: LoanPosition,
    *,
    amount: int,
    action: bytes,
    data: PlutusData | bytes,
    slot: int,
    lovelace: int | None = None,
) -> TransactionOutput:
    """The lender's payment: ``amount`` of the principal at the asset manager.

    Addressed to the asset-manager script under the loan's stake credential and owned
    by the loan's lender bond. It holds only the principal (and lovelace), plus the
    repayment receipt when the loan issues them. ``lovelace`` overrides the ADA the
    output carries (for a byte-exact replay); by default it is ``amount`` for an ADA
    principal, else the minimum the output needs.
    """
    datum = position.loan_datum
    principal = datum.principal_asset.unit()
    assets: dict[str, int] = {}
    if principal != "lovelace":
        assets[principal] = amount
    if _issues_receipts(datum):
        assets[c.REPAYMENT_POLICY + position.receipt_name.hex()] = 1
    address = Address(
        payment_part=ScriptHash(bytes.fromhex(c.ASSET_MANAGER_SPEND_SKH)),
        staking_part=Address.decode(position.loan.address).staking_part,
        network=Address.decode(position.loan.address).network,
    )
    am_datum = AssetManagerDatumWithToken(
        input_output_reference=TxOutRef(
            tx_id=bytes.fromhex(position.out_ref[0]),
            index=position.out_ref[1],
        ),
        action=action,
        data=data,
        owner_asset=Asset(
            policy_id=bytes.fromhex(c.LENDER_BOND_POLICY),
            asset_name=position.loan_id,
        ),
    )
    if lovelace is None:
        floor = min_output_lovelace(
            address=address.encode(),
            assets=assets,
            datum=am_datum,
            slot=slot,
        )
        lovelace = max(amount, floor) if principal == "lovelace" else floor
    return TransactionOutput(
        address,
        asset_to_value(Assets(**{"lovelace": lovelace, **assets})),
        datum=am_datum,
    )


def _issues_receipts(datum: LoanDatum) -> bool:
    return constr(datum.repayment_receipts)[0] == _BOOL_TRUE


def issues_receipts(positions: Sequence[LoanPosition]) -> bool:
    """True if the loans issue repayment receipts; a batch must agree."""
    flags = {_issues_receipts(p.loan_datum) for p in positions}
    if len(flags) > 1:
        raise ValueError("loans that issue receipts and loans that do not cannot mix")
    return flags == {True}


def require_open_before_closing(closes: Sequence[bool], action: str) -> None:
    """Raise unless every loan that stays open sorts before every loan that closes.

    The action script reads the i-th loan's continuing output as the i-th output to
    the loan script, so a loan that closes (and has no such output) may not sort
    before one that continues.
    """
    if any(closed and not later for closed, later in zip(closes, closes[1:])):
        raise ValueError(
            f"loans that stay open must sort before loans the {action} closes",
        )


def add_loan_burn(
    tx_builder: TransactionBuilder,
    *,
    snapshot: LoanActionSnapshot,
    closing: Sequence[LoanPosition],
) -> LoanMintRedeemer | None:
    """Burn the NFT of every closing loan; returns the mint redeemer to finish."""
    if not closing:
        return None
    redeemer = LoanMintRedeemer(
        config_ref_input_index=0,
        is_pool_origin=BoolTrue(),
        origin_withdraw_redeemer_index=0,
    )
    tx_builder.add_minting_script(
        to_pycardano_utxo(snapshot.loan_policy_script_ref),
        redeemer=Redeemer(redeemer),
    )
    burn = MultiAsset(
        {
            ScriptHash(bytes.fromhex(c.LOAN_POLICY)): PyAsset(
                {AssetName(p.loan_id): -1 for p in closing},
            ),
        },
    )
    tx_builder.mint = burn if tx_builder.mint is None else tx_builder.mint + burn
    return redeemer


def add_receipt_mint(
    tx_builder: TransactionBuilder,
    *,
    script_ref: Utxo | None,
    positions: Sequence[LoanPosition],
) -> AssetManagerMintRedeemer | None:
    """Mint one repayment receipt per loan when the loans issue them."""
    if not issues_receipts(positions):
        return None
    if script_ref is None:
        raise ValueError("receipts need the asset-manager policy reference script")
    first = positions[0].out_ref
    redeemer = AssetManagerMintRedeemer(
        config_ref_input_index=0,
        input_ref=TxOutRef(tx_id=bytes.fromhex(first[0]), index=first[1]),
        loan_withdraw_redeemer_index=0,
        loan_claim_action_withdraw_redeemer_index=0,
    )
    tx_builder.add_minting_script(
        to_pycardano_utxo(script_ref),
        redeemer=Redeemer(redeemer),
    )
    mint = MultiAsset(
        {
            ScriptHash(bytes.fromhex(c.REPAYMENT_POLICY)): PyAsset(
                {AssetName(p.receipt_name): 1 for p in positions},
            ),
        },
    )
    tx_builder.mint = mint if tx_builder.mint is None else tx_builder.mint + mint
    return redeemer


def finish_loan_action(
    tx_builder: TransactionBuilder,
    *,
    snapshot: LoanActionSnapshot,
    dispatch: LoanWithdrawRedeemer,
    action: PlutusData,
    burn: LoanMintRedeemer | None = None,
    receipts: AssetManagerMintRedeemer | None = None,
) -> dict[tuple[str, int], int]:
    """Fill the config and redeemer indexes once the transaction is laid out.

    Every input, reference input, mint and withdrawal of the transaction must be in
    place: the indexes filled here go stale if one is added afterwards (outputs may
    still be added, except to the loan and asset-manager scripts). Returns the
    reference-input index map for any index the caller still has to fill. Discard the
    builder if a loan action raises part-way.
    """
    refs = ref_index(tx_builder)
    config_index = refs[out_ref_of(snapshot.config)]
    dispatch.config_ref_input_index = config_index
    action.config_ref_input_index = config_index  # type: ignore[attr-defined]
    dispatch_position = withdraw_redeemer_position(tx_builder, c.LOAN_POLICY)
    if burn is not None:
        burn.config_ref_input_index = config_index
        # A burn never reads the origin index; it points at the loan dispatch.
        burn.origin_withdraw_redeemer_index = dispatch_position
    if receipts is not None:
        receipts.config_ref_input_index = config_index
        receipts.loan_withdraw_redeemer_index = dispatch_position
    return refs
