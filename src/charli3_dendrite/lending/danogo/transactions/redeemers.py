"""Danogo loan-validator redeemers as pycardano PlutusData (byte-exact).

The `CreateLoan` redeemer is used for both `Spend(pool_skh)` and `Mint(loan_skh)` in a
create-loan transaction (the pool spend is delegated to the loan mint). Field
order/shape is verified to round-trip the on-chain CBOR (see test_create_loan_redeemer).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Union

from pycardano import IndefiniteList
from pycardano import PlutusData


@dataclass
class SomeInt(PlutusData):
    """Plutus ``Some(Int)`` == Constr0([x])."""

    CONSTR_ID = 0
    value: int


@dataclass
class NoneVal(PlutusData):
    """Plutus ``None`` == Constr1([])."""

    CONSTR_ID = 1


OptionInt = Union[SomeInt, NoneVal]


@dataclass
class OutputReference(PlutusData):
    """Plutus ``OutputReference`` == Constr0([tx_id_bytes, output_index]).

    The transaction id is a bare ByteArray here (not a nested ``TransactionId``
    constructor), matching the on-chain create-loan redeemer.
    """

    CONSTR_ID = 0
    transaction_id: bytes
    output_index: int


@dataclass
class CreateLoan(PlutusData):
    """CreateLoan redeemer (Spend(pool) and Mint(loan)).

    The 4th variant (alt index 3) of the loan validator's redeemer union, so
    ``CONSTR_ID = 3`` (CBOR tag 124).
    """

    CONSTR_ID = 3
    pool_out_idx: int
    loan_out_idx: int
    fee_out_idx: OptionInt
    protocol_cfg_ref_idx: int
    market_ref_idx: int
    pool_in_out_ref: OutputReference


@dataclass
class DecreaseLoanAmount(PlutusData):
    """DecreaseLoanAmount redeemer (repay).

    The 6th variant (alt index 5) of the loan validator's redeemer union, so
    ``CONSTR_ID = 5`` (CBOR tag 126). One instance is reused byte-for-byte across
    the pool ``Spend``, the loan ``Spend``, the loan ``Mint`` burn (full repay),
    and the ``Withdraw(loan_skh)`` orchestration hub.

    Mirrors :class:`CreateLoan`'s ref fields + ``pool_in_out_ref``; the only shape
    difference is ``loan_out_idx`` is an ``Option<Int>`` (``None`` on full repay,
    ``Some(idx)`` on partial). ``fee_out_idx`` is always ``Some`` on repay.
    """

    CONSTR_ID = 5
    pool_out_idx: int
    loan_out_idx: OptionInt
    fee_out_idx: OptionInt
    protocol_cfg_ref_idx: int
    market_ref_idx: int
    pool_in_out_ref: OutputReference


@dataclass
class IncreaseLoanAmount(PlutusData):
    """IncreaseLoanAmount redeemer (borrow more against an existing loan).

    The 5th variant (alt index 4) of the loan validator's redeemer union, so
    ``CONSTR_ID = 4`` (CBOR tag 125). One instance is reused byte-for-byte across the
    pool ``Spend``, the loan ``Spend``, and the ``Withdraw(pool_skh)`` orchestration
    hub (NOT repay's ``Withdraw(loan_skh)``); an increase-loan tx mints nothing.

    Mirrors :class:`CreateLoan`'s field shape; the only difference is that
    ``loan_out_idx`` is a PLAIN ``int`` -- the loan output is always present on an
    increase (the loan is never closed) -- not an ``Option<Int>``. ``fee_out_idx`` is
    ``None`` on markets with a zero origination-fee rate (no fee output).
    """

    CONSTR_ID = 4
    pool_out_idx: int
    loan_out_idx: int
    fee_out_idx: OptionInt
    protocol_cfg_ref_idx: int
    market_ref_idx: int
    pool_in_out_ref: OutputReference


@dataclass
class ModifyCollaterals(PlutusData):
    """ModifyCollaterals redeemer (add and/or remove collateral, no repay).

    The 10th variant (alt index 9) of the loan validator's redeemer union, so
    ``CONSTR_ID = 9`` (CBOR tag 1282, hex prefix ``d90502``). Carried only on the
    loan ``Spend``; the pool is a REFERENCE input (read for the interest index), not
    spent, so there is no pool spend, no fee output, and no mint. The loan datum is
    unchanged in -> out (only the locked collateral value moves), so the redeemer
    carries no datum-mutation fields.

    Four plain ``int`` index fields and no ``pool_in_out_ref`` (the pool is referenced
    by index, not consumed): ``loan_out_idx`` locates the continuing loan output,
    ``protocol_cfg_ref_idx`` / ``market_ref_idx`` / ``pool_ref_idx`` locate the
    protocol-config, market-param, and pool reference inputs.
    """

    CONSTR_ID = 9
    loan_out_idx: int
    protocol_cfg_ref_idx: int
    market_ref_idx: int
    pool_ref_idx: int


@dataclass
class PoolMarketIndexer(PlutusData):
    """Per-pool indices for a TopupWithdraw action == Constr0([...]).

    The on-chain fields are wrapped in an indefinite-length list, which is
    pycardano's default for a ``PlutusData`` with fields.
    """

    CONSTR_ID = 0
    pool_out_idx: int
    fee_out_idx: OptionInt
    market_ref_idx: int


@dataclass
class TopupWithdraw(PlutusData):
    """TopupWithdraw redeemer (Spend(pool) and Mint(pool)).

    The 3rd variant (alt index 2) of the loan validator's redeemer union, so
    ``CONSTR_ID = 2`` (CBOR tag 123). The spend and mint redeemers are
    byte-for-byte identical, so one class covers both.

    ``pools`` is an indefinite-length list of :class:`PoolMarketIndexer`; the
    on-chain form is ``9f...ff`` (outer list) with each entry as ``d8799f...ff``
    (nested constr). Reproducing that byte-exact constrains the typing here:

    - The field MUST stay an untyped ``IndefiniteList``. A plain
      ``List[PoolMarketIndexer]`` makes pycardano serialize the OUTER list as a
      DEFINITE-length array (``81...``) instead of ``9f...ff``.
    - ``__post_init__`` MUST rebuild ``PoolMarketIndexer`` entries. Letting the
      decoded raw entries stand re-serializes each NESTED constr as a
      DEFINITE-length body (``d87983...``) instead of ``d8799f...ff``.

    Do NOT "simplify" to ``List[PoolMarketIndexer]`` (or drop ``__post_init__``):
    it silently breaks byte-exactness (verified against the captured fixtures).
    """

    CONSTR_ID = 2
    protocol_cfg_ref_idx: int
    pools: IndefiniteList  # elements are PoolMarketIndexer (see class docstring)

    def __post_init__(self) -> None:
        """Coerce decoded pool entries back into ``PoolMarketIndexer``."""
        # Decoding leaves each element as a raw ``CBORTag`` whose body is a
        # DEFINITE list; rebuilding ``PoolMarketIndexer`` instances makes them
        # re-serialize with the on-chain indefinite-length (``d8799f...ff``)
        # body. Required for byte-exact reproduction -- see the class docstring.
        self.pools = IndefiniteList(
            [
                p
                if isinstance(p, PoolMarketIndexer)
                else PoolMarketIndexer.from_primitive(p)
                for p in self.pools
            ],
        )
