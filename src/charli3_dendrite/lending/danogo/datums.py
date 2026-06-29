"""Danogo on-chain datums (parse-only models).

`PoolDatum` and `LoanDatum` are confirmed against mainnet CBOR (see the round-trip
tests over captured datums). The Market Param datum mixes a `Map` keyed by anonymous
`(policy, name)` tuples with scalar fields, so it is parsed structurally by
`market.py` rather than via a fixed `PlutusData` class.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List

from pycardano import IndefiniteList
from pycardano import PlutusData


def asset_unit(policy: bytes, name: bytes) -> str:
    """Dendrite unit string for a (policy, name) asset ('lovelace' for ADA)."""
    if not policy and not name:
        return "lovelace"
    return policy.hex() + name.hex()


@dataclass
class TupleAsset(PlutusData):
    """(policy_id, asset_name) as a `Constr`; ADA is (b"", b"")."""

    CONSTR_ID = 0
    policy: bytes
    name: bytes

    def unit(self) -> str:
        """Dendrite unit string ('lovelace' for ADA)."""
        return asset_unit(self.policy, self.name)


@dataclass
class PRational(PlutusData):
    """Rational number (numerator, denominator)."""

    CONSTR_ID = 0
    num: int
    denom: int


@dataclass
class ProtocolDatum(PlutusData):
    """Protocol Config datum: authorized script hashes."""

    CONSTR_ID = 0
    pool_skh: bytes
    loan_skh: bytes
    config_pool_skh: bytes
    oracle_skh: bytes


@dataclass
class PoolDatum(PlutusData):
    """Pool UTxO datum.

    ``alt_supply_tokens_rate`` re-prices the pool's alternative supply tokens. Its
    on-chain CBOR encoding is conditional, matching Plutus list serialization:

    - EMPTY (single-supply-token markets) is a definite-length empty array (``80``).
    - NON-EMPTY (alt-supply markets) is an indefinite-length array (``9f..ff``) whose
      entries are each an indefinite-length ``PRational`` constr (``d8799f..ff``).

    A plain ``List[PRational]`` field reproduces the empty case (``80``) but emits a
    DEFINITE outer array (``81..``) when non-empty, which diverges from on-chain.
    ``__post_init__`` reconciles both: an empty list is kept as-is (``80``); a
    non-empty list is rebuilt into an ``IndefiniteList`` of fresh ``PRational``
    entries so it (and each entry) re-serializes indefinite. The same rebuild also
    coerces decoded raw entries back into ``PRational`` (mirroring the
    ``IndefiniteList`` round-trip pattern used by the redeemers).
    """

    CONSTR_ID = 0
    total_supply: int
    circulating_dtoken: int
    total_borrow: int
    borrow_apy: int
    undistributed_fee: int
    interest_index: int
    interest_time: int
    alt_supply_tokens_rate: List[PRational] | IndefiniteList

    def __post_init__(self) -> None:
        """Pin ``alt_supply_tokens_rate`` to its conditional on-chain encoding."""
        items = [
            r if isinstance(r, PRational) else PRational.from_primitive(r)
            for r in self.alt_supply_tokens_rate
        ]
        # Empty -> plain list (`80`); non-empty -> indefinite array (`9f..ff`).
        self.alt_supply_tokens_rate = IndefiniteList(items) if items else items


@dataclass
class OwnerNft(PlutusData):
    """Owner-NFT reference: a `Constr` wrapping a bare `[policy, name]` list.

    On-chain the policy is the loan script hash and the name is the per-owner key,
    so `unit()` yields the borrower's owner-NFT unit (used as the loan id).
    """

    CONSTR_ID = 0
    asset: IndefiniteList  # on-chain [policy, name] is an indefinite-length list

    def unit(self) -> str:
        """Dendrite unit string for the owner NFT."""
        policy = self.asset[0] if len(self.asset) > 0 else b""
        name = self.asset[1] if len(self.asset) > 1 else b""
        return asset_unit(policy, name)


@dataclass
class LoanDatum(PlutusData):
    """Loan UTxO datum.

    Field order/shape confirmed against mainnet CBOR: an owner-NFT `Constr`, the
    borrowed token as a bare `[policy, name]` list (NOT a `Constr`), the borrowed
    amount, and the interest index captured when the loan was opened.
    """

    CONSTR_ID = 0
    owner_nft: OwnerNft
    token: IndefiniteList  # on-chain [policy, name] is an indefinite-length list
    loan_amount: int
    initial_interest_index: int

    def token_unit(self) -> str:
        """Dendrite unit string for the borrowed token ('lovelace' for ADA)."""
        policy = self.token[0] if len(self.token) > 0 else b""
        name = self.token[1] if len(self.token) > 1 else b""
        return asset_unit(policy, name)
