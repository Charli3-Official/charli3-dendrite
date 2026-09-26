"""Resolve the live UTxOs a V4 builder needs from a dbsync backend.

The config UTxO is found by its NFT and names every script the protocol runs; the
action scripts (borrow, repay, change collateral, recast) change when FluidTokens
upgrades them, so their reference scripts are always looked up by the hash the live
config names, never by a hard-coded out-ref.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from charli3_dendrite.lending.fluidtokens.transactions.resolve import db_query_rows
from charli3_dendrite.lending.fluidtokens.transactions.resolve import resolve_funding
from charli3_dendrite.lending.fluidtokens.transactions.resolve import resolve_script_ref
from charli3_dendrite.lending.fluidtokens.transactions.resolve import (
    resolve_utxo_by_asset,
)
from charli3_dendrite.lending.fluidtokens.transactions.resolve import (
    resolve_utxo_by_outref,
)
from charli3_dendrite.lending.fluidtokens_v4 import constants as c
from charli3_dendrite.lending.fluidtokens_v4.datums import ConfigDatum
from charli3_dendrite.lending.fluidtokens_v4.datums import PoolManagerDatum
from charli3_dendrite.lending.fluidtokens_v4.transactions.loan_action import (
    LoanPosition,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.loan_action import (
    loan_nft_name,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from charli3_dendrite.backend.backend_base import AbstractBackend
    from charli3_dendrite.lending.fluidtokens.transactions.utxos import Utxo


def resolve_utxo(
    backend: AbstractBackend,
    out_ref: tuple[str, int],
    *,
    allow_spent: bool = False,
) -> Utxo:
    """The UTxO at ``out_ref``; ``allow_spent`` also finds one already spent."""
    return resolve_utxo_by_outref(backend, *out_ref, allow_spent=allow_spent)


def resolve_config_utxo(backend: AbstractBackend) -> Utxo:
    """The live V4 config UTxO."""
    return resolve_utxo_by_asset(backend, c.CONFIG_NFT_POLICY, c.CONFIG_NFT_NAME)


def config_datum(config: Utxo) -> ConfigDatum:
    """The config UTxO's datum."""
    if config.datum is None:
        raise ValueError("config UTxO is missing its datum")
    return ConfigDatum.from_cbor(config.datum)


def resolve_script(backend: AbstractBackend, script_hash: bytes | str) -> Utxo:
    """The newest unspent UTxO carrying the reference script ``script_hash``."""
    hex_hash = script_hash.hex() if isinstance(script_hash, bytes) else script_hash
    return resolve_script_ref(backend, hex_hash)


def resolve_pool_manager(backend: AbstractBackend, pool_id: bytes) -> PoolManagerDatum:
    """The datum of the pool manager of pool ``pool_id`` (the NFTs share a name)."""
    manager = resolve_utxo_by_asset(backend, c.POOL_MANAGER_POLICY, pool_id.hex())
    if manager.datum is None:
        raise ValueError(f"the pool manager of pool {pool_id.hex()} has no datum")
    return PoolManagerDatum.from_cbor(manager.datum)


def resolve_wallet_funding(
    backend: AbstractBackend,
    address: str,
    *,
    exclude: Sequence[Utxo] = (),
) -> list[Utxo]:
    """Unspent UTxOs at ``address`` for fees and change, minus those already spent."""
    taken = {u.out_ref for u in exclude}
    return [u for u in resolve_funding(backend, address) if u.out_ref not in taken]


def resolve_loan_position(
    backend: AbstractBackend,
    loan_out_ref: tuple[str, int],
    *,
    borrower_address: str,
    allow_spent: bool = False,
) -> LoanPosition:
    """A loan UTxO and the wallet UTxO holding its borrower bond.

    Raises ``ValueError`` if the bond is not held at ``borrower_address``.
    """
    loan = resolve_utxo(backend, loan_out_ref, allow_spent=allow_spent)
    bond = resolve_utxo_by_asset(
        backend,
        c.BORROWER_BOND_POLICY,
        loan_nft_name(loan).hex(),
        allow_spent=allow_spent,
    )
    if bond.address != borrower_address:
        raise ValueError(
            f"the borrower bond of loan {loan_out_ref} is not held by "
            f"{borrower_address}",
        )
    return LoanPosition(loan=loan, borrower_bond=bond)


# The config field naming each loan action's withdraw script.
_ACTION_SCRIPT_FIELD = {
    "repay": "loan_repay_action_script_hash",
    "change_collateral": "loan_change_collateral_action_script_hash",
    "recast": "loan_recast_action_script_hash",
}


@dataclass
class LoanActionContext:
    """What every loan action resolves before its own terms."""

    positions: list[LoanPosition]
    funding: list[Utxo]
    config: Utxo
    scripts: ConfigDatum
    loan_spend_script_ref: Utxo
    loan_policy_script_ref: Utxo
    action_script_ref: Utxo


def resolve_loan_action(
    backend: AbstractBackend,
    *,
    action: str,
    loan_out_refs: Sequence[tuple[str, int]],
    borrower_address: str,
    funding: Sequence[Utxo] | None = None,
    allow_spent: bool = False,
) -> LoanActionContext:
    """Resolve the loans, their bonds, the funding and the scripts of a loan action.

    ``action`` is ``"repay"``, ``"change_collateral"`` or ``"recast"``.
    """
    if not loan_out_refs:
        raise ValueError("a loan action needs at least one loan")
    positions = [
        resolve_loan_position(
            backend,
            out_ref,
            borrower_address=borrower_address,
            allow_spent=allow_spent,
        )
        for out_ref in loan_out_refs
    ]
    config = resolve_config_utxo(backend)
    scripts = config_datum(config)
    spent = [u for p in positions for u in (p.loan, p.borrower_bond)]
    return LoanActionContext(
        positions=positions,
        funding=list(funding)
        if funding is not None
        else resolve_wallet_funding(backend, borrower_address, exclude=spent),
        config=config,
        scripts=scripts,
        loan_spend_script_ref=resolve_script(backend, scripts.loan_spend_script_hash),
        loan_policy_script_ref=resolve_script(backend, scripts.loan_policy_id),
        action_script_ref=resolve_script(
            backend,
            getattr(scripts, _ACTION_SCRIPT_FIELD[action]),
        ),
    )


def default_window(
    backend: AbstractBackend,
    *,
    valid_from: int | None,
    valid_to: int | None,
    slots: int,
) -> tuple[int, int]:
    """``(valid_from, valid_to)``: from the tip unless given, ``slots`` long."""
    from charli3_dendrite.lending.transactions.infra import current_slot

    start = valid_from if valid_from is not None else current_slot(backend)
    return start, valid_to if valid_to is not None else start + slots


# Header byte of a mainnet reward address with a script credential.
_SCRIPT_REWARD_HEADER = 0xF1


def reward_account_registered(backend: AbstractBackend, script_hash: str) -> bool:
    """True if the script's reward account is registered on chain.

    A withdraw script runs only through a withdrawal from its reward account, and the
    ledger rejects a withdrawal from an unregistered account (Ogmios evaluation does
    not check this).
    """
    account = bytes([_SCRIPT_REWARD_HEADER]) + bytes.fromhex(script_hash)
    rows = db_query_rows(
        backend,
        """SELECT
             (SELECT max(r.tx_id) FROM stake_registration r
                JOIN stake_address a ON a.id = r.addr_id
               WHERE a.hash_raw = %(account)s) AS registered,
             (SELECT max(d.tx_id) FROM stake_deregistration d
                JOIN stake_address a ON a.id = d.addr_id
               WHERE a.hash_raw = %(account)s) AS deregistered""",
        {"account": account},
    )
    registered = rows[0]["registered"] if rows else None
    deregistered = rows[0]["deregistered"] if rows else None
    return registered is not None and (
        deregistered is None or deregistered < registered
    )
