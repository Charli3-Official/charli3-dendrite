"""Resolve the live UTxOs a V4 builder needs from a dbsync backend.

The config UTxO is found by its NFT and names every script the protocol runs; the
action scripts (borrow, repay, change collateral, recast, pool edit and cancel)
change when FluidTokens upgrades them, so their reference scripts are always looked
up by the hash the live config names, never by a hard-coded out-ref. The pool
manager's owner checks are parameters of the pool-manager policy, so they are looked
up by their constant hashes, and only while the config names that policy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pycardano import Address

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
from charli3_dendrite.lending.fluidtokens_v4.datums import AssetManagerDatumWithToken
from charli3_dendrite.lending.fluidtokens_v4.datums import ConfigDatum
from charli3_dendrite.lending.fluidtokens_v4.datums import LenderManagerConfigDatum
from charli3_dendrite.lending.fluidtokens_v4.datums import LenderManagerDatum
from charli3_dendrite.lending.fluidtokens_v4.datums import PoolManagerDatum
from charli3_dendrite.lending.fluidtokens_v4.indexing import DECODE_ERRORS
from charli3_dendrite.lending.fluidtokens_v4.indexing import EntityKind
from charli3_dendrite.lending.fluidtokens_v4.indexing import EntitySelector
from charli3_dendrite.lending.fluidtokens_v4.indexing import entity_selectors
from charli3_dendrite.lending.fluidtokens_v4.loader import fetch_entities
from charli3_dendrite.lending.fluidtokens_v4.transactions.claim import (
    MAX_REPAYMENTS_PER_CLAIM,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.claim import ClaimPosition
from charli3_dendrite.lending.fluidtokens_v4.transactions.common import pool_nft_name
from charli3_dendrite.lending.fluidtokens_v4.transactions.loan_action import (
    LoanPosition,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.loan_action import (
    loan_nft_name,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.pool_action import (
    PoolPosition,
)
from charli3_dendrite.lending.transactions.infra import parse_out_ref

if TYPE_CHECKING:
    from collections.abc import Collection
    from collections.abc import Sequence

    from pycardano import PlutusData

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


def require_known_pool_manager(scripts: ConfigDatum) -> None:
    """Raise unless ``scripts`` names :data:`~.constants.POOL_MANAGER_POLICY`.

    A pool create, edit or cancel all rely on this module's pool-manager owner-check
    hashes, which are parameters of that one policy and unknown for any other.
    """
    if scripts.pool_manager_policy_id.hex() != c.POOL_MANAGER_POLICY:
        raise ValueError(
            "the live config names pool-manager policy "
            f"{scripts.pool_manager_policy_id.hex()}, whose owner checks are unknown",
        )


def resolve_lender_manager_config_utxo(backend: AbstractBackend) -> Utxo:
    """The live lender-manager config UTxO."""
    return resolve_utxo_by_asset(
        backend,
        c.LENDER_MANAGER_CONFIG_NFT_POLICY,
        c.LENDER_MANAGER_CONFIG_NFT_NAME,
    )


def lender_manager_config_datum(config: Utxo) -> LenderManagerConfigDatum:
    """The lender-manager config UTxO's datum."""
    if config.datum is None:
        raise ValueError("lender-manager config UTxO is missing its datum")
    return LenderManagerConfigDatum.from_cbor(config.datum)


def resolve_script(backend: AbstractBackend, script_hash: bytes | str) -> Utxo:
    """The newest unspent UTxO carrying the reference script ``script_hash``."""
    hex_hash = script_hash.hex() if isinstance(script_hash, bytes) else script_hash
    return resolve_script_ref(backend, hex_hash)


def resolve_pool_manager_utxo(
    backend: AbstractBackend,
    pool_id: bytes,
    *,
    allow_spent: bool = False,
) -> Utxo:
    """The pool-manager UTxO of pool ``pool_id`` (the NFTs share a name)."""
    return resolve_utxo_by_asset(
        backend,
        c.POOL_MANAGER_POLICY,
        pool_id.hex(),
        allow_spent=allow_spent,
    )


def resolve_pool_manager(backend: AbstractBackend, pool_id: bytes) -> PoolManagerDatum:
    """The datum of the pool manager of pool ``pool_id``."""
    manager = resolve_pool_manager_utxo(backend, pool_id)
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


def resolve_pool_position(
    backend: AbstractBackend,
    pool_out_ref: tuple[str, int],
    *,
    lender_address: str,
    allow_spent: bool = False,
) -> PoolPosition:
    """A pool UTxO and its pool manager.

    Raises ``ValueError`` unless the key of ``lender_address`` owns the pool manager.
    """
    pool = resolve_utxo(backend, pool_out_ref, allow_spent=allow_spent)
    position = PoolPosition(
        pool=pool,
        pool_manager=resolve_pool_manager_utxo(
            backend,
            pool_nft_name(pool),
            allow_spent=allow_spent,
        ),
    )
    payment = Address.decode(lender_address).payment_part
    if position.owner_pkh != bytes(payment):
        raise ValueError(
            f"pool {pool_out_ref} is owned by key {position.owner_pkh.hex()}, not "
            f"by {lender_address}",
        )
    return position


# The config field naming each pool action's withdraw script, and the pool-manager
# owner check the action runs.
_POOL_ACTION_SCRIPTS = {
    "edit": ("pool_edit_action_script_hash", c.POOL_MANAGER_EDIT_POOL_ACTION_SKH),
    "cancel": ("pool_cancel_action_script_hash", c.POOL_MANAGER_CANCEL_ACTION_SKH),
}


@dataclass
class PoolActionContext:
    """What a pool edit or cancel resolves before its own terms."""

    positions: list[PoolPosition]
    funding: list[Utxo]
    config: Utxo
    pool_spend_script_ref: Utxo
    pool_manager_spend_script_ref: Utxo
    pool_policy_script_ref: Utxo
    pool_manager_policy_script_ref: Utxo
    action_script_ref: Utxo
    manager_action_script_ref: Utxo


def resolve_pool_action(
    backend: AbstractBackend,
    *,
    action: str,
    pool_out_refs: Sequence[tuple[str, int]],
    lender_address: str,
    funding: Sequence[Utxo] | None = None,
    allow_spent: bool = False,
) -> PoolActionContext:
    """Resolve the pools, their managers, the funding and the scripts of a pool action.

    ``action`` is ``"edit"`` or ``"cancel"``. Raises ``ValueError`` when the live
    config names a pool-manager policy other than
    :data:`~.constants.POOL_MANAGER_POLICY`, whose owner checks this module knows, or
    when ``funding`` is not given and the lender's wallet resolves no UTxOs (an
    explicit empty ``funding`` stays allowed: the action then funds and burns against
    the pools themselves).
    """
    if not pool_out_refs:
        raise ValueError("a pool action needs at least one pool")
    positions = [
        resolve_pool_position(
            backend,
            out_ref,
            lender_address=lender_address,
            allow_spent=allow_spent,
        )
        for out_ref in pool_out_refs
    ]
    config = resolve_config_utxo(backend)
    scripts = config_datum(config)
    require_known_pool_manager(scripts)
    field, manager_action = _POOL_ACTION_SCRIPTS[action]
    spent = [u for p in positions for u in (p.pool, p.pool_manager)]
    if funding is not None:
        resolved_funding = list(funding)
    else:
        resolved_funding = resolve_wallet_funding(
            backend,
            lender_address,
            exclude=spent,
        )
        if not resolved_funding:
            raise ValueError(f"no UTxOs at {lender_address} fund the {action}")
    return PoolActionContext(
        positions=positions,
        funding=resolved_funding,
        config=config,
        pool_spend_script_ref=resolve_script(backend, scripts.pool_spend_script_hash),
        pool_manager_spend_script_ref=resolve_script(
            backend,
            scripts.pool_manager_spend_script_hash,
        ),
        pool_policy_script_ref=resolve_script(backend, scripts.pool_policy_id),
        pool_manager_policy_script_ref=resolve_script(
            backend,
            scripts.pool_manager_policy_id,
        ),
        action_script_ref=resolve_script(backend, getattr(scripts, field)),
        manager_action_script_ref=resolve_script(backend, manager_action),
    )


def _resolve_ref(backend: AbstractBackend, value: str) -> Utxo:
    """The UTxO at ``value`` (``tx_hash#index``)."""
    ref = parse_out_ref(value)
    return resolve_utxo(backend, (bytes(ref.transaction_id).hex(), ref.index))


def _has_inline_datum(utxo: Utxo, cls: type[PlutusData]) -> bool:
    """True if ``utxo`` carries an inline datum that decodes as ``cls``."""
    if not utxo.datum:
        return False
    try:
        cls.from_cbor(utxo.datum)
    except DECODE_ERRORS:
        return False
    return True


def _lender_bonds(
    backend: AbstractBackend,
    selector: EntitySelector,
    *,
    lender_pkh: bytes,
    bonds: Collection[bytes] | None,
) -> dict[str, tuple[str, str]]:
    """The lender's bonds at the lender manager: name -> (UTxO out-ref, principal)."""
    held: dict[str, tuple[str, str]] = {}
    for manager in fetch_entities(backend, selector):
        auth = manager.lender_auth  # type: ignore[attr-defined]
        if auth.kind == "signature" and auth.hash_hex == lender_pkh.hex():
            principal = manager.datum.principal_asset.unit()  # type: ignore[attr-defined]
            for name in manager.lender_bond_names:  # type: ignore[attr-defined]
                held[name] = (manager.out_ref, principal)
    if bonds is None:
        return held
    wanted = {bond.hex() for bond in bonds}
    missing = sorted(wanted - held.keys())
    if missing:
        raise ValueError(
            f"lender bonds {missing} of this lender are not at the lender manager",
        )
    return {name: bond for name, bond in held.items() if name in wanted}


def _repayments_by_size(
    backend: AbstractBackend,
    selector: EntitySelector,
    held: dict[str, tuple[str, str]],
) -> list[tuple[str, str]]:
    """(repayment, bond) out-refs of every repayment the bonds own, largest first."""
    candidates = []
    for payment in fetch_entities(backend, selector):
        unit = payment.owner_unit  # type: ignore[attr-defined]
        bond = (
            held.get(unit[len(c.LENDER_BOND_POLICY) :])
            if unit is not None and unit.startswith(c.LENDER_BOND_POLICY)
            else None
        )
        if bond is not None:
            amount = payment.assets.root.get(bond[1], 0)
            candidates.append((-amount, payment.out_ref, bond[0]))
    return [(payment_ref, bond_ref) for _, payment_ref, bond_ref in sorted(candidates)]


def resolve_claim_positions(
    backend: AbstractBackend,
    scripts: ConfigDatum,
    *,
    lender_pkh: bytes,
    bonds: Collection[bytes] | None = None,
    max_repayments: int = MAX_REPAYMENTS_PER_CLAIM,
) -> list[ClaimPosition]:
    """The lender's bond UTxOs at the lender manager and the repayments each owns.

    A bond UTxO is the lender's when its lender authorisation is the key
    ``lender_pkh``; a repayment is an asset-manager UTxO owned by one of its lender
    bonds. The ``max_repayments`` largest repayments, measured in their bond's
    principal, are kept, and bond UTxOs left owning none are dropped. A UTxO without
    an inline datum of its kind cannot be spent by a claim and is skipped. ``bonds``
    (loan ids) limits the result to those bonds and raises ``ValueError`` for one
    that is not the lender's at the lender manager.
    """
    selectors = entity_selectors(scripts)
    held = _lender_bonds(
        backend,
        selectors[EntityKind.LENDER_MANAGER],
        lender_pkh=lender_pkh,
        bonds=bonds,
    )
    bond_utxos: dict[str, Utxo | None] = {}
    owned: dict[str, list[Utxo]] = {}
    kept = 0
    for payment_ref, bond_ref in _repayments_by_size(
        backend,
        selectors[EntityKind.ASSET_MANAGER],
        held,
    ):
        if kept == max_repayments:
            break
        if bond_ref not in bond_utxos:
            bond_utxo = _resolve_ref(backend, bond_ref)
            bond_utxos[bond_ref] = (
                bond_utxo if _has_inline_datum(bond_utxo, LenderManagerDatum) else None
            )
        repayment = _resolve_ref(backend, payment_ref)
        if bond_utxos[bond_ref] is not None and _has_inline_datum(
            repayment,
            AssetManagerDatumWithToken,
        ):
            owned.setdefault(bond_ref, []).append(repayment)
            kept += 1
    return [
        ClaimPosition(bond=bond_utxos[bond_ref], repayments=repayments)  # type: ignore[arg-type]
        for bond_ref, repayments in sorted(owned.items())
    ]


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
