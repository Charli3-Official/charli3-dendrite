"""Classify and decode raw FluidTokens V4 UTxO records.

The indexing entry point: :func:`parse_utxo` turns any UTxO record seen at a V4
spend-script credential into its typed state, and :func:`entity_selectors` gives the
credential (and identity policy) of every V4 UTxO kind so an indexer knows what to
watch. Neither function touches a backend.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Union

import cbor2  # type: ignore[import-not-found]
from pycardano import Address
from pycardano import ScriptHash
from pycardano.exception import DecodingException
from pycardano.exception import DeserializeException
from pycardano.exception import InvalidAddressInputException

from charli3_dendrite.dataclasses.models import PoolSelector
from charli3_dendrite.dataclasses.models import PoolStateInfo
from charli3_dendrite.lending.fluidtokens_v4 import constants as c
from charli3_dendrite.lending.fluidtokens_v4.datums import ConfigDatum
from charli3_dendrite.lending.fluidtokens_v4.state import FluidV4AssetManagerState
from charli3_dendrite.lending.fluidtokens_v4.state import FluidV4LenderManagerState
from charli3_dendrite.lending.fluidtokens_v4.state import FluidV4LoanState
from charli3_dendrite.lending.fluidtokens_v4.state import FluidV4LockedBorrowerState
from charli3_dendrite.lending.fluidtokens_v4.state import FluidV4PoolManagerState
from charli3_dendrite.lending.fluidtokens_v4.state import FluidV4PoolState
from charli3_dendrite.lending.fluidtokens_v4.state import FluidV4RequestState
from charli3_dendrite.lending.fluidtokens_v4.state import identity_name
from charli3_dendrite.lending.units import script_payment_address

FluidV4State = Union[
    FluidV4PoolState,
    FluidV4PoolManagerState,
    FluidV4LoanState,
    FluidV4RequestState,
    FluidV4AssetManagerState,
    FluidV4LenderManagerState,
    FluidV4LockedBorrowerState,
]


class EntityKind(str, Enum):
    """The kinds of V4 UTxO, one per spend script."""

    POOL = "pool"
    POOL_MANAGER = "pool_manager"
    LOAN = "loan"
    REQUEST = "request"
    ASSET_MANAGER = "asset_manager"
    LENDER_MANAGER = "lender_manager"
    LOCKED_BORROWER_MANAGER = "locked_borrower_manager"


_STATE_CLASS: dict[EntityKind, type[FluidV4State]] = {
    EntityKind.POOL: FluidV4PoolState,
    EntityKind.POOL_MANAGER: FluidV4PoolManagerState,
    EntityKind.LOAN: FluidV4LoanState,
    EntityKind.REQUEST: FluidV4RequestState,
    EntityKind.ASSET_MANAGER: FluidV4AssetManagerState,
    EntityKind.LENDER_MANAGER: FluidV4LenderManagerState,
    EntityKind.LOCKED_BORROWER_MANAGER: FluidV4LockedBorrowerState,
}

# Everything a malformed record can raise while its datum is decoded. Truncated CBOR
# raises cbor2's end-of-stream error, which is not a ValueError.
_DECODE_ERRORS = (
    cbor2.CBORDecodeError,
    DeserializeException,
    ValueError,
    TypeError,
    KeyError,
    IndexError,
    AttributeError,
)


@dataclass(frozen=True)
class EntitySelector:
    """Where one V4 UTxO kind lives, and the token every genuine one holds.

    ``identity_policy`` is None for kinds without an identity token (asset, lender and
    locked-borrower managers); for those, a decodable datum is the only check.
    """

    kind: EntityKind
    payment_credential: str
    identity_policy: str | None = None

    @property
    def address(self) -> str:
        """Enterprise address with this kind's payment credential (backend queries)."""
        return script_payment_address(self.payment_credential)

    def pool_selector(self) -> PoolSelector:
        """Backend selector for every UTxO at this credential."""
        return PoolSelector(addresses=[self.address])


def entity_selectors(
    config: ConfigDatum | None = None,
) -> dict[EntityKind, EntitySelector]:
    """Selectors for every V4 UTxO kind, read from ``config``.

    ``config`` defaults to the constants (``constants.default_config``); pass the
    result of ``constants.resolve_config`` to follow the live config.
    """
    cfg = config if config is not None else c.default_config()
    kinds = (
        (EntityKind.POOL, cfg.pool_spend_script_hash, cfg.pool_policy_id),
        (
            EntityKind.POOL_MANAGER,
            cfg.pool_manager_spend_script_hash,
            cfg.pool_manager_policy_id,
        ),
        (EntityKind.LOAN, cfg.loan_spend_script_hash, cfg.loan_policy_id),
        (EntityKind.REQUEST, cfg.request_spend_script_hash, cfg.request_policy_id),
        (EntityKind.ASSET_MANAGER, cfg.asset_manager_spend_script_hash, None),
        (
            EntityKind.LOCKED_BORROWER_MANAGER,
            cfg.locked_borrower_manager_spend_script_hash,
            None,
        ),
        (EntityKind.LENDER_MANAGER, bytes.fromhex(c.LENDER_MANAGER_SPEND_SKH), None),
    )
    return {
        kind: EntitySelector(
            kind=kind,
            payment_credential=credential.hex(),
            identity_policy=policy.hex() if policy is not None else None,
        )
        for kind, credential, policy in kinds
    }


def script_credential(address: str) -> str | None:
    """Payment script hash (hex) of a bech32 address; None for any other address.

    Every V4 entity sits at a script credential, so a key-hash payment part (even one
    whose bytes equal a V4 script hash) returns None, as does an unparseable address.
    pycardano asserts key-hash sizes and decodes a bech32 Byron header as CBOR, so a
    malformed address can also raise ``AssertionError`` or ``DeserializeException``.
    """
    try:
        part = Address.decode(address).payment_part
    except (
        AssertionError,
        DecodingException,
        DeserializeException,
        InvalidAddressInputException,
        TypeError,
        ValueError,
    ):
        return None
    return part.payload.hex() if isinstance(part, ScriptHash) else None


def parse_utxo(
    info: PoolStateInfo,
    selectors: Mapping[EntityKind, EntitySelector] | None = None,
) -> FluidV4State | None:
    """Classify one UTxO record and decode it into its V4 state.

    Returns None, never raising, when the record is not a genuine V4 entity: its
    payment credential is not a V4 spend script, it lacks its kind's identity token
    (exactly one, quantity 1), it carries no inline datum, or its datum does not decode
    as its kind's type. Anyone can send a UTxO to a script address, so the identity
    token, not the address, is what makes a pool, pool manager, loan or request genuine.
    """
    active = selectors if selectors is not None else entity_selectors()
    credential = script_credential(info.address)
    selector = next(
        (s for s in active.values() if s.payment_credential == credential),
        None,
    )
    if selector is None or not info.datum_cbor:
        return None
    if (
        selector.identity_policy is not None
        and identity_name(info.assets, selector.identity_policy) is None
    ):
        return None
    try:
        return _STATE_CLASS[selector.kind].from_record(info)
    except _DECODE_ERRORS:
        return None
