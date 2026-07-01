"""Pure assembly of a Danogo create-loan transaction's components.

The ``CreateLoan`` redeemer is derived entirely from the context's role indices and the
spent pool ``OutputReference``. The owner-NFT name is ``blake2b_224`` of the pool
input's transaction id (the seed that makes each loan id unique). Per the design spec,
the loan amount / interest index are carried from the snapshot (the validator enforces
the protocol math at evaluation time), not re-derived here.
"""

from __future__ import annotations

from dataclasses import dataclass

from charli3_dendrite.lending.danogo.datums import LoanDatum
from charli3_dendrite.lending.danogo.oracles.redeemer import OraclePriceCalcRdmr
from charli3_dendrite.lending.danogo.transactions.context import CreateLoanContext
from charli3_dendrite.lending.danogo.transactions.loan_ids import owner_nft_name
from charli3_dendrite.lending.danogo.transactions.oracle_redeemer import (
    assemble_oracle_redeemer,
)
from charli3_dendrite.lending.danogo.transactions.redeemers import CreateLoan
from charli3_dendrite.lending.danogo.transactions.redeemers import NoneVal
from charli3_dendrite.lending.danogo.transactions.redeemers import OutputReference
from charli3_dendrite.lending.danogo.transactions.redeemers import SomeInt


@dataclass
class CreateLoanComponents:
    """The byte-matchable pieces of a create-loan tx (no balancing)."""

    create_loan_redeemer: CreateLoan
    oracle_redeemer: OraclePriceCalcRdmr
    owner_nft_name: str
    mint: list[tuple[str, str, int]]
    loan_datum: LoanDatum
    validity: tuple[int | None, int | None]


def assemble_components(ctx: CreateLoanContext) -> CreateLoanComponents:
    """Assemble the create-loan redeemers, mint, and loan datum from the snapshot."""
    tx_id_hex, out_idx = ctx.pool_in_out_ref
    out_ref = OutputReference(
        transaction_id=bytes.fromhex(tx_id_hex),
        output_index=out_idx,
    )
    fee_idx: SomeInt | NoneVal = (
        SomeInt(ctx.fee_out_idx) if ctx.fee_out_idx is not None else NoneVal()
    )
    create_loan = CreateLoan(
        pool_out_idx=ctx.pool_out_idx,
        loan_out_idx=ctx.loan_out_idx,
        fee_out_idx=fee_idx,
        protocol_cfg_ref_idx=ctx.protocol_cfg_ref_idx,
        market_ref_idx=ctx.market_ref_idx,
        pool_in_out_ref=out_ref,
    )

    owner_name = owner_nft_name(ctx.pool_in_out_ref)
    mint = [
        (ctx.loan_skh, ctx.market_name, 1),
        (ctx.loan_skh, owner_name, 1),
    ]

    loan_out = ctx.outputs[ctx.loan_out_idx]
    if loan_out.datum is None:
        raise ValueError("create-loan output is missing its inline datum")
    loan_datum = LoanDatum.from_cbor(bytes.fromhex(loan_out.datum))

    oracle_rdmr, _ = assemble_oracle_redeemer(ctx)
    return CreateLoanComponents(
        create_loan_redeemer=create_loan,
        oracle_redeemer=oracle_rdmr,
        owner_nft_name=owner_name,
        mint=mint,
        loan_datum=loan_datum,
        validity=(ctx.invalid_before, ctx.invalid_hereafter),
    )
