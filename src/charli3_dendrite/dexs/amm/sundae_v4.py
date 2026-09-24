"""SundaeSwap V4 ("Vault Architecture") datum and redeemer types.

These PlutusData classes are a faithful transcription of the on-chain shapes used
by the SundaeSwap-finance/sundae-v4 contracts (the modular vault pool, the order
validator, and the per-invariant-module configs). They cover parsing only: the
datum / redeemer / transcript CBOR layer. Pricing math and transaction building
live elsewhere.

Encoding facts that are load-bearing (each is reproduced byte-for-byte on a
``from_cbor`` -> ``to_cbor`` round trip):

* Every record is a single-constructor type at the stated ``CONSTR_ID``; field
  order is the on-chain declaration order, which the validators index by, so it
  must not be reordered.
* Aiken 2-tuples ``(a, b)`` serialise as a bare 2-element array, NOT a
  constructor. So ``assets`` decodes as ``[[AssetClass, reserve], ...]`` and
  ``module_state`` as ``[[module_hash, config_hash], ...]``. Those fields are
  modelled as :class:`~pycardano.IndefiniteList` of 2-element lists, never as a
  list of constructor-bearing dataclasses.
* On-chain the non-empty arrays are indefinite-length (``9f ... ff``) and empty
  constructors are the definite empty array (``80``); pycardano reproduces both,
  so the modelled classes round trip byte-exact.
* ``Bool`` is ``False`` = constructor 0 / ``True`` = constructor 1.
  ``Option<T>`` is ``Some`` = constructor 0 ``[T]`` / ``None`` = constructor 1.
* ``Data`` fields (an order's ``constraints`` / ``extension``, a transcript
  entry's ``operation_data``, a destination's optional inline ``datum``) are
  opaque and decoded lazily as :class:`~pycardano.RawPlutusData`; their inner
  constructor index is the dispatch tag and is not frozen into a dataclass.

Three distinct "tag" namespaces exist and are kept separate:

* the action-map tag (``ActionEntry.tag`` / ``PoolAction.tag``): the canonical
  scheme is ``1`` = upgrade (read by the vault to resolve the ``Upgrade``
  redeemer), ``100``-``199`` = trade / operate actions (``100`` = the swap
  action every deployed pool config carries) and ``200``-``299`` = treasury /
  admin actions (``200`` = the treasury sweep, the one action allowed to
  decrease the pool's lovelace surplus);
* the transcript operation tag (``TranscriptEntry.operation_tag``): per-invariant
  module (constant-sum uses 3 = swap, 4 = withdraw, 5 = swap+claim,
  6 = deposit);
* the basic-order constraint kind (the constructor index of a basic order's
  constraint payload, :class:`BasicConstraintKind`): 0 = deposit, 1 = withdraw,
  2 = swap, 3 = claim. The on-chain constraint reads the payload positionally
  and ignores the index; it is dispatch metadata for the scooper.

Deployment-specific values (applied script hashes, reference scripts, config
tokens, the flat service fee) come from the per-network manifest
(:class:`SundaeV4Deployment`); the class family targets one network at a time.
The deployed order packages are ``basic`` (:data:`BasicConstraint`, four kinds)
and ``strategy``, each paired with the fee constraint; the only deployed
invariant module is constant-sum.

Two derivations the contracts pin are reproduced here: a pool's ``identifier``
is the first 28 bytes of the blake2b-256 of the serialised seed output reference
(:func:`pool_identifier`), and a module's ``module_state`` slot holds the
blake2b-256 of its serialised config (:func:`module_config_hash`).
"""

from __future__ import annotations

import functools
import hashlib
import importlib.resources
import json
from dataclasses import dataclass
from enum import IntEnum
from typing import TYPE_CHECKING
from typing import Any
from typing import ClassVar
from typing import Union

import cbor2  # type: ignore[import-not-found]
from pycardano import Address
from pycardano import DeserializeException
from pycardano import IndefiniteList
from pycardano import Network
from pycardano import PlutusData
from pycardano import PlutusV3Script
from pycardano import RawPlutusData
from pycardano import Redeemer
from pycardano import ScriptHash
from pycardano import TransactionBuilder
from pycardano import UTxO
from pycardano import VerificationKeyHash
from pycardano.serialization import CBORTag
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr
from pydantic import model_validator

from charli3_dendrite.backend import get_backend
from charli3_dendrite.dataclasses.datums import AssetClass
from charli3_dendrite.dataclasses.datums import PlutusFullAddress
from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dataclasses.models import DendriteBaseModel
from charli3_dendrite.dataclasses.models import PoolSelector
from charli3_dendrite.dataclasses.models import RedeemerRecord
from charli3_dendrite.dexs.amm.multi_asset import AbstractMultiAssetPoolState
from charli3_dendrite.dexs.core.errors import InvalidPoolError
from charli3_dendrite.dexs.core.errors import ModuleConfigUnavailableError
from charli3_dendrite.dexs.core.errors import NotAPoolError

# ---------------------------------------------------------------------------
# Shared sub-types
# ---------------------------------------------------------------------------


@dataclass
class BoolFalse(PlutusData):
    """Aiken ``Bool`` ``False`` (constructor 0, no fields)."""

    CONSTR_ID = 0


@dataclass
class BoolTrue(PlutusData):
    """Aiken ``Bool`` ``True`` (constructor 1, no fields)."""

    CONSTR_ID = 1


Bool = Union[BoolFalse, BoolTrue]


@dataclass
class Rational(PlutusData):
    """A rational number ``num / den`` (every fee, price weight and share)."""

    CONSTR_ID = 0
    num: int
    den: int


@dataclass
class OutputReference(PlutusData):
    """A reference to a transaction output ``(transaction_id, output_index)``."""

    CONSTR_ID = 0
    transaction_id: bytes
    output_index: int


# -- MultisigScript (the standard Sundae multisig union) --------------------


@dataclass
class MultisigSignature(PlutusData):
    """Require a signature from ``key_hash`` (constructor 0)."""

    CONSTR_ID = 0
    key_hash: bytes


@dataclass
class MultisigAllOf(PlutusData):
    """Require every sub-script in ``scripts`` to be satisfied (constructor 1)."""

    CONSTR_ID = 1
    scripts: IndefiniteList


@dataclass
class MultisigAnyOf(PlutusData):
    """Require any one sub-script in ``scripts`` (constructor 2)."""

    CONSTR_ID = 2
    scripts: IndefiniteList


@dataclass
class MultisigAtLeast(PlutusData):
    """Require at least ``required`` of ``scripts`` (constructor 3)."""

    CONSTR_ID = 3
    required: int
    scripts: IndefiniteList


@dataclass
class MultisigBefore(PlutusData):
    """Valid only before ``time`` (constructor 4)."""

    CONSTR_ID = 4
    time: int


@dataclass
class MultisigAfter(PlutusData):
    """Valid only after ``time`` (constructor 5)."""

    CONSTR_ID = 5
    time: int


@dataclass
class MultisigScriptHash(PlutusData):
    """Require a withdrawal/spend of ``script_hash`` (constructor 6)."""

    CONSTR_ID = 6
    script_hash: bytes


MultisigScript = Union[
    MultisigSignature,
    MultisigAllOf,
    MultisigAnyOf,
    MultisigAtLeast,
    MultisigBefore,
    MultisigAfter,
    MultisigScriptHash,
]


# -- Destination: where an order's proceeds are paid ------------------------


@dataclass
class DestinationFixed(PlutusData):
    """Pay to a concrete address (constructor 0).

    ``datum`` is an ``Option<Data>`` (``Some`` = constructor 0 ``[d]``,
    ``None`` = constructor 1); it is kept opaque as raw plutus data.
    """

    CONSTR_ID = 0
    address: PlutusFullAddress
    datum: RawPlutusData


@dataclass
class DestinationSelf(PlutusData):
    """Pay back to the consuming UTxO's own address (constructor 1).

    This is the partial-fill continuation target: a partially filled order's
    remainder returns to the order script address.
    """

    CONSTR_ID = 1


Destination = Union[DestinationFixed, DestinationSelf]


# ---------------------------------------------------------------------------
# Pool datum and action map
# ---------------------------------------------------------------------------


@dataclass
class ActionEntry(PlutusData):
    """One row of the pool's action map: ``tag`` -> required ``modules``.

    ``enabled`` is a :data:`Bool`. ``modules`` is an
    :class:`~pycardano.IndefiniteList` of module script hashes (``bytes``) that
    must all participate, as withdraw scripts, for this action. The invariant
    module hash listed here is the de-facto pool-type marker: there is no datum
    field naming the curve; the curve is the module hash.
    """

    CONSTR_ID = 0
    tag: int
    enabled: Bool
    modules: IndefiniteList


@dataclass
class SundaeV4PoolDatum(PlutusData):
    """The N-asset vault resting datum (constructor 0).

    ``assets`` is the reserve declaration as an
    :class:`~pycardano.IndefiniteList` of ``[AssetClass, reserve]`` 2-element
    lists, NOT the UTxO value bag; reward / bounty / fee / preminted-LP riders in
    the value are not part of LP accounting, so any reserve projection reads this
    field. ``identifier`` is the one-shot pool id (also the pool NFT asset name);
    pool identity is the NFT, not the script address. ``actions`` is the action
    map. ``module_state`` is an :class:`~pycardano.IndefiniteList` of
    ``[module_hash, config_hash]`` 2-element lists, where ``config_hash`` commits
    to a module config supplied off-datum at use time, so live pricing params are
    not present in the resting datum. ``min_surplus`` is the floor on the pool's
    lovelace surplus (the lovelace above the declared ADA reserve), copied from
    the pool config at creation; ``extension`` is reserved room for future
    pool-level fields, pinned across every datum-preserving spend.

    Field order is access-frequency ordered on chain (the first four fields are
    read by modules every step) and is load-bearing: new fields are only ever
    appended.
    """

    CONSTR_ID = 0
    assets: IndefiniteList
    total_lp: int
    circulating_lp: int
    preminted_lp: int
    identifier: bytes
    actions: list[ActionEntry]
    module_state: IndefiniteList
    min_surplus: int
    extension: RawPlutusData


@dataclass
class PoolState(PlutusData):
    """The vault-tracked reserve vector plus LP counters (constructor 0).

    These are the first four fields of :class:`SundaeV4PoolDatum`, in the same
    order. It appears as ``state_after`` inside every :class:`TranscriptEntry`.
    """

    CONSTR_ID = 0
    assets: IndefiniteList
    total_lp: int
    circulating_lp: int
    preminted_lp: int


@dataclass
class TranscriptEntry(PlutusData):
    """One scoop step (constructor 0).

    Field order is the on-chain declaration order: ``state_after``,
    ``fee_budget``, ``operation_tag``, ``operation_data``. The entry stores no
    explicit input/output amounts and no price; those are recovered by diffing
    ``state_after.assets`` against the prior step's state. ``operation_data`` is
    opaque (mostly empty for a plain swap; a constant-sum swap+claim carries a
    bounty-claim payload, see :class:`BountyClaim`).
    """

    CONSTR_ID = 0
    state_after: PoolState
    fee_budget: int
    operation_tag: int
    operation_data: RawPlutusData


@dataclass
class BountyClaim(PlutusData):
    """The ``operation_data`` of a constant-sum swap+claim step (constructor 0).

    To recover the true post-swap reserve before diffing, ``amount`` of ``asset``
    is added back into the post-step reserve (the on-chain check restores the
    claim and validates the remainder as a plain constant-sum swap).
    """

    CONSTR_ID = 0
    asset: AssetClass
    amount: int


# -- PoolRedeemer: the constructor index is the action class ----------------


@dataclass
class EscapeHatch(PlutusData):
    """Permissionless pro-rata LP exit (constructor 0).

    Burns ``redeemed_lp`` LP and pays ``redeemed_lp / total_lp`` of each reserve.
    Always enabled, no modules; a pool-liveness exit signal.
    """

    CONSTR_ID = 0
    redeemed_lp: int


@dataclass
class Upgrade(PlutusData):
    """Mutate the action map and/or migrate the vault (constructor 1).

    Governance gated, no fields. After an upgrade the pool may rest at a different
    script address, so the pool must be tracked by its NFT, not a fixed address.
    """

    CONSTR_ID = 1


@dataclass
class EmergencyDisable(PlutusData):
    """Flip one action's ``enabled`` flag (constructor 2).

    Refuses to touch the reserved tags 0/1/2.
    """

    CONSTR_ID = 2
    target_tag: int
    set_enabled: Bool


@dataclass
class PoolAction(PlutusData):
    """The scoop action (constructor 3).

    ``tag`` selects the action-map row (which modules must run). ``transcript``
    is the per-step trade record (the trade-synthesis source). ``pool_input_index``
    / ``pool_output_index`` give O(1) lookup of the pool UTxO in the transaction.
    """

    CONSTR_ID = 3
    tag: int
    transcript: list[TranscriptEntry]
    pool_input_index: int
    pool_output_index: int


@dataclass
class Destroy(PlutusData):
    """Pool teardown (constructor 4), the only spend that may burn the pool NFT.

    Every action-map module runs its ``Destroy`` redeemer covering the pool, the
    NFT and reference token are burned, and every accounted LP token is burned
    with them. Appended last so the earlier constructor indices stay stable.
    """

    CONSTR_ID = 4


PoolRedeemer = Union[EscapeHatch, Upgrade, EmergencyDisable, PoolAction, Destroy]


# ---------------------------------------------------------------------------
# Order datum and redeemers
# ---------------------------------------------------------------------------


@dataclass
class SundaeV4OrderDatum(PlutusData):
    """An order's constraints (constructor 0); never an executed trade.

    This models the *deployed* order datum, which carries seven fields. An order
    binds to a mutable order-config settings entry (``config_token``) and the
    constraint modules it requires are sourced from that entry, so the datum
    grew a settings-binding field after the original constraint-as-opaque-``Data``
    shape. Some compiled blueprints predate that field and describe only six
    fields (``constraints`` / ``extension`` as bare ``Data``); the live on-chain
    datum is authoritative and the seven-field shape is what round trips
    byte-exact, so it is the shape modelled here. Do not collapse this back to the
    six-field blueprint shape.

    Field order follows the deployed contract. ``owner`` is a
    :data:`MultisigScript` (in practice almost always a single signature).
    ``destination`` is where proceeds are paid. ``service_budget`` is the order's
    lifetime allocation for service fees in lovelace: each continuation fill
    deducts exactly the protocol's flat ``base_fee`` from it, and the terminal
    fill (the one whose output leaves the order address) deducts
    ``min(max_per_execution, service_budget)``. ``max_per_execution`` is the
    immutable per-scoop cap on that deduction and doubles as the inclusion gate:
    an order whose cap is below ``base_fee`` is never scooped. A basic order is
    always terminal, so it pays ``min(max_per_execution, service_budget)`` on
    its single fill; setting both equal to ``base_fee`` pays exactly the fee.
    ``config_token`` is the token name of the order-config settings entry that
    governs this order; the per-entry ``constraint_module_hash`` keys below are
    exactly that settings entry's required-constraint set (the deployed
    packages are ``[trade constraint, fee constraint]``).
    ``constraints`` is an :class:`~pycardano.IndefiniteList` of
    ``[constraint_module_hash, payload]`` 2-element lists; each payload is opaque
    and tag-dispatched (its inner constructor index is the constraint tag).
    ``extension`` is opaque and is preserved byte-for-byte across partial-fill
    continuations.

    The constraint payloads and ``extension`` are typed as ``Data`` upstream and
    are intentionally not frozen into per-variant dataclasses; they are kept as
    raw plutus data so the parser tolerates field drift. ``constraints`` itself is
    modelled as a CBOR array, which matches the deployed modular-order shape; an
    order whose ``constraints`` body were instead a non-list ``Data`` value would
    need this field relaxed to :class:`~pycardano.RawPlutusData`.
    """

    CONSTR_ID = 0
    owner: MultisigScript
    destination: Destination
    service_budget: int
    max_per_execution: int
    config_token: bytes
    constraints: IndefiniteList
    extension: RawPlutusData


@dataclass
class SwapConstraint(PlutusData):
    """The ``swap``-role order constraint payload (constraint tag 2).

    The swap role (a partial-fill swap that also carries the route constraint)
    is not part of the deployed launch packages — no order config requires it —
    so this class is parse-only; a plain V4 swap is placed as a
    :class:`BasicSwap`.

    This is the payload carried by the ``swapOrder`` entry of an order datum's
    ``constraints`` list (the keyed ``[module_hash, payload]`` pairs). The
    constraint-tag namespace assigns ``2`` to a swap, so the on-chain payload is a
    constructor-2 record; the ``swap_order`` withdraw validator reads it by field
    position (``unconstr_fields``), so field order is load-bearing.

    Fields (deployed ``lib/constraints/swap.ak`` ``SwapFields``):

    * ``offered`` — the :class:`AssetClass` the order is selling (lovelace is the
      empty-policy/empty-name asset).
    * ``original_offered`` — the full offered amount at creation; the fee budget is
      pro-rated against it (``fee_budget = fee_allowance * offered_this_fill /
      original_offered``).
    * ``remaining_offered`` — the still-unfilled amount; equal to
      ``original_offered`` on a fresh order, and decremented on each partial-fill
      continuation.
    * ``min_received`` — the per-asset fill floor, an
      :class:`~pycardano.IndefiniteList` of ``[AssetClass, min_amount]`` 2-element
      lists (an Aiken ``List<(AssetClass, Int)>``). The on-chain ``check_fill_ratio``
      enforces ``received * original_offered >= min_amount * offered_this_fill`` so
      the ratio is preserved across partial fills.
    """

    CONSTR_ID = 2
    offered: AssetClass
    original_offered: int
    remaining_offered: int
    min_received: IndefiniteList


@dataclass
class StrategyConstraint(PlutusData):
    """The ``strategy``-role order constraint payload (constructor 0).

    This is the payload carried by the ``strategyOrder`` entry of an order datum's
    ``constraints`` list. Unlike a swap, a strategy order does not name a concrete
    fill in its datum; it is a *signed delegation*. The on-chain
    ``extract_strategy_constraints`` reads it by field position
    (``unconstr_fields``), so field order is load-bearing.

    Fields (deployed ``lib/types/strategy.ak`` ``StrategyConstraints``):

    * ``auth`` — the :data:`MultisigScript` allowed to sign a
      :class:`StrategyExecution` (the off-chain executor / delegate). At scoop time
      the redeemer carries a :class:`SignedStrategyExecution` whose signatures must
      satisfy this multisig.
    * ``final_destinations`` — the candidate payout addresses a signed execution may
      select between (by index, via ``StrategyExecution.final``). Modelled as an
      :class:`~pycardano.IndefiniteList` of :class:`Destination` (an Aiken
      ``List<Destination>``); on-chain the non-empty list is indefinite-length, so
      this field round trips byte-exact only as an ``IndefiniteList``, not a plain
      typed ``list`` (which pycardano would emit definite-length).
    """

    CONSTR_ID = 0
    auth: MultisigScript
    final_destinations: IndefiniteList


class BasicConstraintKind(IntEnum):
    """The constructor index of a basic order's constraint payload.

    The four kinds share one field shape; the index is how the scooper tells a
    deposit, a withdrawal, a routing-free swap and a bounty claim apart. The
    on-chain ``basic_order`` constraint reads the payload positionally and never
    inspects the index.
    """

    DEPOSIT = 0
    WITHDRAW = 1
    SWAP = 2
    CLAIM = 3


@dataclass
class _BasicFields(PlutusData):
    """The field shape every basic-order constraint kind shares.

    Fields (deployed ``lib/constraints/basic.ak`` ``BasicFields``):

    * ``offered`` — what the order is selling, an
      :class:`~pycardano.IndefiniteList` of ``[AssetClass, amount]`` 2-element
      lists (an Aiken ``List<(AssetClass, Int)>``); a deposit offers every pool
      asset, a swap offers one.
    * ``min_received`` — the per-asset fill floor, an
      :class:`~pycardano.IndefiniteList` of ``[AssetClass, min_amount]`` 2-element
      lists. The floor is absolute (no partial-fill ratio) and, for a lovelace
      leg, is measured gross of the service fee.

    A basic order settles in one terminal fill to a ``Fixed`` destination; the
    ``basic_order`` withdraw validator rejects a ``Self`` destination.
    """

    offered: IndefiniteList
    min_received: IndefiniteList

    @property
    def kind(self) -> BasicConstraintKind:
        """The constraint kind this payload's constructor index encodes."""
        return BasicConstraintKind(self.CONSTR_ID)


@dataclass
class BasicDeposit(_BasicFields):
    """A proportional deposit: every pool asset offered, LP tokens received."""

    CONSTR_ID = 0


@dataclass
class BasicWithdraw(_BasicFields):
    """A proportional withdrawal: LP tokens offered, every pool asset received."""

    CONSTR_ID = 1


@dataclass
class BasicSwap(_BasicFields):
    """A routing-free swap: one asset offered, a floor on what comes back.

    This is how a plain V4 swap is placed on the deployed order packages; the
    partial-fill swap constraint (:class:`SwapConstraint`) is not deployed.
    """

    CONSTR_ID = 2


@dataclass
class BasicClaim(_BasicFields):
    """A bounty claim against a constant-sum pool's accrued rebalance obligation."""

    CONSTR_ID = 3


BasicConstraint = Union[BasicDeposit, BasicWithdraw, BasicSwap, BasicClaim]

# CBOR tags 121..127 carry Plutus constructor indices 0..6 inline.
_CONSTR_TAG_FIRST = 121
_CONSTR_TAG_LAST = 127

_BASIC_KINDS: dict[int, type[_BasicFields]] = {
    BasicConstraintKind.DEPOSIT: BasicDeposit,
    BasicConstraintKind.WITHDRAW: BasicWithdraw,
    BasicConstraintKind.SWAP: BasicSwap,
    BasicConstraintKind.CLAIM: BasicClaim,
}


def parse_basic_constraint(payload: RawPlutusData | CBORTag | bytes) -> BasicConstraint:
    """Decode a basic order's constraint payload into its kind-specific class.

    ``payload`` is the opaque ``Data`` carried under the ``basic_order`` key of
    an order datum's ``constraints`` list (or its CBOR bytes). The payload's
    constructor index selects the kind.

    Raises:
        ValueError: if the constructor index is not one of the four kinds.
    """
    if isinstance(payload, RawPlutusData):
        raw = payload.data
    elif isinstance(payload, bytes):
        raw = RawPlutusData.from_cbor(payload).data
    else:
        raw = payload
    tag = raw.tag if isinstance(raw, CBORTag) else None
    index = (
        tag - _CONSTR_TAG_FIRST
        if tag is not None and _CONSTR_TAG_FIRST <= tag <= _CONSTR_TAG_LAST
        else None
    )
    cls = _BASIC_KINDS.get(index) if index is not None else None
    if cls is None:
        raise ValueError(f"Not a basic-order constraint payload (constructor {index}).")
    return cls.from_primitive(raw)


# -- Strategy execution: the off-chain-signed fill a strategy order delegates ----
#
# A strategy order's datum (its StrategyConstraint) is only a delegation; the
# actual fill is a StrategyExecution signed by the constraint's ``auth`` and
# supplied, wrapped in a SignedStrategyExecution, in the scoop redeemer. These
# types are modelled for parse-completeness — deciding and signing an execution is
# off-chain executor territory, not built here.


@dataclass
class OptionSomeInt(PlutusData):
    """Aiken ``Option<Int>`` ``Some(value)`` (constructor 0)."""

    CONSTR_ID = 0
    value: int


@dataclass
class OptionNone(PlutusData):
    """Aiken ``Option<Int>`` ``None`` (constructor 1, no fields)."""

    CONSTR_ID = 1


OptionInt = Union[OptionSomeInt, OptionNone]


@dataclass
class IntervalBoundNegativeInfinity(PlutusData):
    """An interval bound at negative infinity (constructor 0, no fields)."""

    CONSTR_ID = 0


@dataclass
class IntervalBoundFinite(PlutusData):
    """A finite interval bound at ``value`` (constructor 1)."""

    CONSTR_ID = 1
    value: int


@dataclass
class IntervalBoundPositiveInfinity(PlutusData):
    """An interval bound at positive infinity (constructor 2, no fields)."""

    CONSTR_ID = 2


IntervalBoundType = Union[
    IntervalBoundNegativeInfinity,
    IntervalBoundFinite,
    IntervalBoundPositiveInfinity,
]


@dataclass
class IntervalBound(PlutusData):
    """One end of a :class:`ValidityRange` (constructor 0).

    ``bound_type`` is the :data:`IntervalBoundType` (negative-infinity / finite /
    positive-infinity) and ``is_inclusive`` a :data:`Bool` flag.
    """

    CONSTR_ID = 0
    bound_type: IntervalBoundType
    is_inclusive: Bool


@dataclass
class ValidityRange(PlutusData):
    """A POSIX-time validity range (Aiken ``Interval<Int>``; constructor 0).

    The ``cardano/transaction.ValidityRange`` an execution is valid within; the
    ``strategy`` constraint's ``check_validity_range`` requires it to include the
    transaction's own validity range.
    """

    CONSTR_ID = 0
    lower_bound: IntervalBound
    upper_bound: IntervalBound


@dataclass
class StrategyExecution(PlutusData):
    """A single signed strategy fill (constructor 0).

    Fields (deployed ``lib/types/strategy.ak`` ``StrategyExecution``):

    * ``order_ref`` — the :class:`OutputReference` of the order UTxO this execution
      authorises (binds the signature to one specific order).
    * ``validity_range`` — the :class:`ValidityRange` the execution is valid within.
    * ``min_deltas`` — the complete signed value-change spec for the order, an
      :class:`~pycardano.IndefiniteList` of ``[AssetClass, min_delta]`` 2-element
      lists (an Aiken ``List<(AssetClass, Int)>``): the minimum net delta
      (output minus input) per asset, measured gross of the service fee. A
      positive entry is a floor on what must be received, a negative entry is
      signed permission to consume up to that amount, and an unlisted asset may
      not leave the order.
    * ``final`` — an :data:`OptionInt`: ``Some(index)`` selects a payout from the
      constraint's ``final_destinations`` (a terminal fill), ``None`` pays the
      order's own ``destination`` (a continuation).
    * ``extension`` — opaque ``Data``, kept as :class:`~pycardano.RawPlutusData`.
    """

    CONSTR_ID = 0
    order_ref: OutputReference
    validity_range: ValidityRange
    min_deltas: IndefiniteList
    final: OptionInt
    extension: RawPlutusData


@dataclass
class SignedStrategyExecution(PlutusData):
    """A :class:`StrategyExecution` plus its signatures (constructor 0).

    Fields (deployed ``lib/types/strategy.ak`` ``SignedStrategyExecution``):

    * ``execution`` — the :class:`StrategyExecution` being authorised.
    * ``signatures`` — an :class:`~pycardano.IndefiniteList` of
      ``[verification_key, signature]`` 2-element lists (an Aiken
      ``List<(VerificationKey, Signature)>``); these must satisfy the order's
      ``StrategyConstraint.auth`` multisig over the serialised ``execution``.
    """

    CONSTR_ID = 0
    execution: StrategyExecution
    signatures: IndefiniteList


@dataclass
class OrderCancel(PlutusData):
    """Order spend redeemer: owner-signed cancel (constructor 0)."""

    CONSTR_ID = 0


@dataclass
class OrderScoop(PlutusData):
    """Order spend redeemer: defer to the order withdraw script (constructor 1).

    ``own_input_index`` names this order's own position among the inputs.
    """

    CONSTR_ID = 1
    own_input_index: int


OrderRedeemer = Union[OrderCancel, OrderScoop]


@dataclass
class ConfigRef(PlutusData):
    """A reference to an order-config settings entry (constructor 0).

    ``ref_index`` is the reference-input index carrying the config entry; ``token``
    is its config token name. Shape from the deployed source
    (``txpipe/audit-fixes-bounty-cli`` ``lib/types/order.ak``).
    """

    CONSTR_ID = 0
    ref_index: int
    token: bytes


@dataclass
class OrderValidatorEntry(PlutusData):
    """One row of the order withdraw redeemer (constructor 0).

    ``output_index`` names this order's output; ``config_index`` selects which
    referenced config (in the redeemer's ``configs``) governs it. Entries align
    positionally with the order inputs.

    Shape follows the deployed source; the sample scoop carried no OrderValidator
    redeemer, so this is not yet byte-exact-fixture-validated.
    """

    CONSTR_ID = 0
    output_index: int
    config_index: int


@dataclass
class OrderValidatorRedeemer(PlutusData):
    """The order withdraw redeemer that validates all orders at once (ctor 0).

    ``configs`` are the referenced order-config entries; ``entries`` align with the
    order inputs. Shape follows the deployed source
    (``txpipe/audit-fixes-bounty-cli``); not yet fixture-validated.
    """

    CONSTR_ID = 0
    configs: list[ConfigRef]
    entries: list[OrderValidatorEntry]


# ---------------------------------------------------------------------------
# Per-invariant-module configs
#
# These arrive in a module's Operate redeemer at scoop time and are bound to a
# pool's ``module_state`` by ``blake2b_256(serialise_data(config))``; they are
# not present in the resting pool datum.
# ---------------------------------------------------------------------------


@dataclass
class ConstantProductConfig(PlutusData):
    """Constant-product module config (constructor 0): a single ``fee``."""

    CONSTR_ID = 0
    fee: Rational


@dataclass
class ConstantSumConfig(PlutusData):
    """Constant-sum module config (constructor 0).

    ``prices`` is one integer price weight per asset, positionally aligned with
    the pool ``assets``, modelled as an :class:`~pycardano.IndefiniteList` so the
    non-empty array serialises the same way Aiken's ``serialise_data`` emits an
    on-chain ``List`` (the config is hashed off-datum, so this encoding is
    load-bearing once builders fill it). ``fee`` is the swap fee. ``bounty_k``
    parameterises the integrated rebalance bounty (``num == 0`` disables it).
    ``balance_fee`` is the fee rate charged on the swap portion of a bounty-claim
    step in place of ``fee`` (``0 <= balance_fee <= fee``; zero is the full
    waiver).
    """

    CONSTR_ID = 0
    prices: IndefiniteList
    fee: Rational
    bounty_k: Rational
    balance_fee: Rational


@dataclass
class ConcentratedLiquidityConfig(PlutusData):
    """Concentrated-liquidity module config (constructor 0): one band.

    ``sqrt_price_a`` / ``sqrt_price_b`` are the band bounds (kept as exact integer
    rationals; the on-chain math is pure integer cross-products). ``fee`` is the
    swap fee.
    """

    CONSTR_ID = 0
    sqrt_price_a: Rational
    sqrt_price_b: Rational
    fee: Rational


@dataclass
class FeeSplitConfig(PlutusData):
    """Fee-split module config (constructor 0): the treasury ``protocol_share``."""

    CONSTR_ID = 0
    protocol_share: Rational


# ---------------------------------------------------------------------------
# Module redeemers
#
# Every pool module is a withdraw-zero validator with the same three-way
# redeemer: ``Create`` (constructor 0, whose FIRST field is the config the pool
# datum's ``module_state`` commits to), ``Operate`` (constructor 1, one entry per
# pool the module covers in this transaction) and ``Destroy`` (constructor 2).
# The per-pool ``Operate`` entry is where a passive indexer recovers the live
# module config: ``module_state`` holds only its hash.
# ---------------------------------------------------------------------------


@dataclass
class DestroyEntry(PlutusData):
    """A module's covering entry at pool teardown (constructor 0)."""

    CONSTR_ID = 0
    pool_oref: OutputReference


@dataclass
class ConstantSumEntry(PlutusData):
    """One pool the constant-sum module operates on (constructor 0).

    ``config`` is the full :class:`ConstantSumConfig`; its hash must equal the
    module's ``module_state`` slot in both the pool input and output datums.
    """

    CONSTR_ID = 0
    pool_oref: OutputReference
    config: ConstantSumConfig


@dataclass
class ConstantSumCreate(PlutusData):
    """Constant-sum module ``Create`` (constructor 0)."""

    CONSTR_ID = 0
    initial_state: ConstantSumConfig
    pool_output_index: int


@dataclass
class ConstantSumOperate(PlutusData):
    """Constant-sum module ``Operate`` (constructor 1)."""

    CONSTR_ID = 1
    entries: list[ConstantSumEntry]


@dataclass
class ConstantSumDestroy(PlutusData):
    """Constant-sum module ``Destroy`` (constructor 2)."""

    CONSTR_ID = 2
    entries: list[DestroyEntry]


ConstantSumRedeemer = Union[ConstantSumCreate, ConstantSumOperate, ConstantSumDestroy]


@dataclass
class FeeSplitEntry(PlutusData):
    """One pool the fee-split module operates on (constructor 0)."""

    CONSTR_ID = 0
    pool_oref: OutputReference
    config: FeeSplitConfig


@dataclass
class FeeSplitCreate(PlutusData):
    """Fee-split module ``Create`` (constructor 0).

    ``settings_ref_index`` names the pool-config settings node among the
    reference inputs and ``stake_list_ref_index`` the approved-stake-list node
    that decides the low fee tier.
    """

    CONSTR_ID = 0
    config: FeeSplitConfig
    pool_output_index: int
    settings_ref_index: int
    stake_list_ref_index: int


@dataclass
class FeeSplitOperate(PlutusData):
    """Fee-split module ``Operate`` (constructor 1)."""

    CONSTR_ID = 1
    entries: list[FeeSplitEntry]


@dataclass
class FeeSplitDestroy(PlutusData):
    """Fee-split module ``Destroy`` (constructor 2)."""

    CONSTR_ID = 2
    entries: list[DestroyEntry]


FeeSplitRedeemer = Union[FeeSplitCreate, FeeSplitOperate, FeeSplitDestroy]


@dataclass
class FairnessEntry(PlutusData):
    """One pool the fairness module covers (constructor 0).

    ``scooper_idx`` indexes the settings datum's ``authorized_scoopers`` list;
    that scooper's multisig must be satisfied by the transaction.
    """

    CONSTR_ID = 0
    pool_oref: OutputReference
    scooper_idx: int


@dataclass
class FairnessCreate(PlutusData):
    """Fairness module ``Create`` (constructor 0, no config)."""

    CONSTR_ID = 0


@dataclass
class FairnessOperate(PlutusData):
    """Fairness module ``Operate`` (constructor 1)."""

    CONSTR_ID = 1
    entries: list[FairnessEntry]


@dataclass
class FairnessDestroy(PlutusData):
    """Fairness module ``Destroy`` (constructor 2)."""

    CONSTR_ID = 2
    entries: list[DestroyEntry]


FairnessRedeemer = Union[FairnessCreate, FairnessOperate, FairnessDestroy]


# ---------------------------------------------------------------------------
# Settings nodes
#
# The global settings UTxO (settings NFT with the empty asset name) plus one
# token-named node per configuration entry, all resting at the settings
# validator and read as reference inputs. A node's token name is a one-byte
# type prefix (0 = mutable, 1 = immutable, 2 = permissioned) followed by the
# first 31 bytes of the blake2b-256 of its serialised seed output reference.
# ---------------------------------------------------------------------------


@dataclass
class ScoopersSome(PlutusData):
    """``Option<List<MultisigScript>>`` ``Some`` (constructor 0)."""

    CONSTR_ID = 0
    scoopers: IndefiniteList


@dataclass
class ScoopersNone(PlutusData):
    """``Option<List<MultisigScript>>`` ``None`` (constructor 1)."""

    CONSTR_ID = 1


OptionScoopers = Union[ScoopersSome, ScoopersNone]


@dataclass
class SettingsDatum(PlutusData):
    """The global settings datum (constructor 0).

    ``authorized_scoopers`` is the multisig whitelist the fairness module
    indexes; ``security_council`` may flip a pool action's ``enabled`` flag via
    the ``EmergencyDisable`` pool redeemer.
    """

    CONSTR_ID = 0
    settings_admin: MultisigScript
    treasury_admin: MultisigScript
    authorized_scoopers: OptionScoopers
    security_council: MultisigScript
    extension: RawPlutusData


@dataclass
class PoolConfig(PlutusData):
    """An approved pool configuration node (constructor 0).

    A pool is created against one of these: its datum ``actions`` must equal
    ``actions``, its address must be ``pool_validator``, and its ``min_surplus``
    is copied from here. ``module_params`` carries per-module creation
    parameters keyed by module hash (opaque here); ``mint_permission`` is an
    ``Option<MultisigScript>`` gating who may create pools bound to this config,
    kept opaque.
    """

    CONSTR_ID = 0
    pool_validator: bytes
    actions: list[ActionEntry]
    module_params: IndefiniteList
    mint_permission: RawPlutusData
    min_surplus: int
    extension: RawPlutusData


@dataclass
class OrderConfig(PlutusData):
    """An approved order configuration node (constructor 0).

    ``required_constraints`` is the ordered list of constraint module hashes an
    order binding this node (by ``config_token``) must carry, in this order.
    """

    CONSTR_ID = 0
    label: bytes
    required_constraints: IndefiniteList


@dataclass
class FeeSettings(PlutusData):
    """The fee-settings node (constructor 0): the flat per-execution ``base_fee``."""

    CONSTR_ID = 0
    base_fee: int


# ---------------------------------------------------------------------------
# Derivations pinned by the contracts
# ---------------------------------------------------------------------------


def pool_identifier(seed_tx_hash: bytes, seed_index: int) -> bytes:
    """A pool's ``identifier`` from the seed output reference it was created from.

    The pool-mint policy derives it as the first 28 bytes of the blake2b-256 of
    the serialised ``OutputReference`` so that, with the 4-byte CIP-68 prefix,
    every pool token name fits the 32-byte limit.
    """
    oref = OutputReference(transaction_id=seed_tx_hash, output_index=seed_index)
    return hashlib.blake2b(oref.to_cbor(), digest_size=32).digest()[:28]


@dataclass(frozen=True)
class PinnedDeposit:
    """The unique constant-sum deposit the vault accepts for an offer.

    ``target_delta_v`` is the value delta the scoop declares, ``deltas`` the
    per-asset contributions (aligned to the pool's assets), ``lp_after`` the
    pool's total LP after the step and ``lp_minted`` the LP the depositor is
    owed. Anything offered above ``deltas`` is returned as change.
    """

    target_delta_v: int
    deltas: list[int]
    lp_after: int
    lp_minted: int


def constant_sum_pinned_deposit(
    reserves: list[int],
    prices: list[int],
    offered: list[int],
    total_lp: int,
) -> PinnedDeposit:
    """The target-pinned constant-sum deposit for ``offered`` against ``reserves``.

    A constant-sum deposit is proportional: the step declares a value delta
    ``t`` and the validator pins every reserve to move by ``ceil(r_i * t / V_b)``
    and the total LP to ``floor(lp_b * (V_b + t) / V_b)``, rounding against the
    depositor. The largest fillable ``t`` is capped by the scarcest offered
    asset, ``min_i floor(offered_i * V_b / r_i)``, so every pool asset must be
    offered.

    Raises:
        ValueError: on misaligned inputs, a pool with no value or LP, or an
            offer that leaves some pool asset out.
    """
    if not len(reserves) == len(prices) == len(offered):
        msg = "reserves, prices and offered must be aligned to the pool assets."
        raise ValueError(msg)
    if total_lp <= 0:
        msg = "The pool has no LP to deposit against."
        raise ValueError(msg)
    value_before = sum(r * p for r, p in zip(reserves, prices))
    if value_before <= 0:
        msg = "The pool holds no value."
        raise ValueError(msg)
    caps = [
        amount * value_before // reserve
        for amount, reserve in zip(offered, reserves)
        if reserve > 0
    ]
    target = min(caps) if caps else 0
    if target <= 0:
        msg = "A constant-sum deposit must offer every pool asset in proportion."
        raise ValueError(msg)
    deltas = [-(-(reserve * target) // value_before) for reserve in reserves]
    lp_after = total_lp * (value_before + target) // value_before
    return PinnedDeposit(
        target_delta_v=target,
        deltas=deltas,
        lp_after=lp_after,
        lp_minted=lp_after - total_lp,
    )


@dataclass(frozen=True)
class PinnedWithdraw:
    """The unique constant-sum withdrawal the vault accepts for an LP redemption.

    ``target_delta_v`` is the (negative) value delta the scoop declares,
    ``payouts`` the per-asset amounts paid out (aligned to the pool's assets),
    ``lp_after`` the pool's total LP after the step and ``lp_burned`` the LP
    actually consumed — at most the LP redeemed, and below it only on pools
    whose LP is finer-grained than their value.
    """

    target_delta_v: int
    payouts: list[int]
    lp_after: int
    lp_burned: int


def constant_sum_pinned_withdraw(
    reserves: list[int],
    prices: list[int],
    lp: int,
    total_lp: int,
) -> PinnedWithdraw:
    """The target-pinned constant-sum withdrawal of ``lp`` against ``reserves``.

    The step declares ``t = -floor(lp * V / total_lp)``; every reserve moves by
    ``ceil(r_i * t / V)`` (a payout of ``floor(r_i * |t| / V)``) and the total
    LP to ``floor(total_lp * (V + t) / V)``, rounding against the withdrawer.

    Raises:
        ValueError: misaligned inputs, a pool with no value, or ``lp`` outside
            ``(0, total_lp]``.
    """
    if len(reserves) != len(prices):
        msg = "reserves and prices must be aligned to the pool assets."
        raise ValueError(msg)
    if not 0 < lp <= total_lp:
        msg = "lp must be positive and at most the pool's total LP."
        raise ValueError(msg)
    value = sum(r * p for r, p in zip(reserves, prices))
    if value <= 0:
        msg = "The pool holds no value."
        raise ValueError(msg)
    target_delta_v = -(lp * value // total_lp)
    payouts = [r * -target_delta_v // value for r in reserves]
    lp_after = total_lp + (total_lp * target_delta_v) // value
    return PinnedWithdraw(
        target_delta_v=target_delta_v,
        payouts=payouts,
        lp_after=lp_after,
        lp_burned=total_lp - lp_after,
    )


def module_config_hash(config: PlutusData) -> bytes:
    """The ``module_state`` commitment of a module config.

    Every config-bearing module pins ``blake2b_256(serialise_data(config))``
    into its ``module_state`` slot at creation and re-checks it on every
    operate step; a config-less module's slot holds the one-byte serialised
    empty list instead.
    """
    return hashlib.blake2b(config.to_cbor(), digest_size=32).digest()


# ---------------------------------------------------------------------------
# Deployment manifest
#
# V4 validators are parameterised (by the settings policy, the pool policy, the
# base order hash, ...), so every applied script hash — and therefore every
# script address, constraint key and config token — differs per network. The
# manifest resource records each deployment as the Sundae API's ``protocols``
# query serves it: the applied validator hashes by blueprint title, the
# published reference-script UTxOs, and the token-named settings nodes.
# ---------------------------------------------------------------------------

_DEPLOYMENTS_RESOURCE = "sundae_v4_deployments.json"

# The lovelace rider locked alongside an order: it funds the min-UTxO of the
# payout output and is returned with the fill (or the cancel).
_ORDER_RIDER = 2_000_000


@functools.lru_cache(maxsize=1)
def _load_deployments() -> dict[str, Any]:
    resource = importlib.resources.files(__package__) / _DEPLOYMENTS_RESOURCE
    data = json.loads(resource.read_text())
    return {key: value for key, value in data.items() if not key.startswith("_")}


@dataclass(frozen=True)
class SundaeV4Deployment:
    """The applied V4 deployment on one Cardano network.

    ``validators`` maps a blueprint title (``pool.spend``, ``order.spend``,
    ``basic_order.withdraw``, ...) to its applied script hash; ``references``
    maps the same titles to the published reference-script UTxO; ``settings``
    maps a settings-node label (``basic-order``, ``strategy-order``,
    ``cs-pool``, ``fee-settings``, ``migration-authority``, ``stake-list``,
    ...) to its UTxO and decoded values. A network whose API lists more than
    one node under the same label stores the duplicate under ``<label>#2``
    (mainnet's two ``cs-pool`` nodes are ``cs-pool`` and ``cs-pool#2``).
    """

    network: str
    validators: dict[str, str]
    references: dict[str, tuple[str, int]]
    settings_policy: bytes
    pool_nft_policy: bytes
    settings: dict[str, dict[str, Any]]

    @classmethod
    def for_network(cls, network: str) -> SundaeV4Deployment:
        """The deployment on ``network`` (``preview`` / ``preprod`` / ``mainnet``).

        Raises:
            LookupError: if V4 is not deployed on that network.
        """
        try:
            data = _load_deployments()[network]
        except KeyError:
            msg = f"SundaeSwap V4 has no deployment on {network}."
            raise LookupError(msg) from None
        return cls(
            network=network,
            validators=dict(data["validators"]),
            references={
                title: (ref["tx_hash"], int(ref["index"]))
                for title, ref in data["references"].items()
            },
            settings_policy=bytes.fromhex(data["settings_policy"]),
            pool_nft_policy=bytes.fromhex(data["pool_nft_policy"]),
            settings={label: dict(entry) for label, entry in data["settings"].items()},
        )

    def validator(self, title: str) -> bytes:
        """The applied script hash of the validator with this blueprint title."""
        return bytes.fromhex(self.validators[title])

    def reference(self, title: str) -> tuple[str, int]:
        """The ``(tx_hash, index)`` of the validator's published reference script."""
        return self.references[title]

    def module_kind(self, script_hash: bytes) -> str | None:
        """The module kind (``constant_sum``, ``fee_split``, ...) of a withdraw script.

        Module validators are registered under ``<kind>.withdraw``; anything else
        (the vault, order and settings validators) is not a module.
        """
        for title, applied in self.validators.items():
            if bytes.fromhex(applied) == script_hash and title.endswith(".withdraw"):
                return title[: -len(".withdraw")]
        return None

    @property
    def pool_hash(self) -> bytes:
        """The vault (pool spend) validator hash: the pool script address."""
        return self.validator("pool.spend")

    @property
    def order_hash(self) -> bytes:
        """The order spend validator hash: the order script address."""
        return self.validator("order.spend")

    @property
    def basic_order_hash(self) -> bytes:
        """The basic-order constraint module hash."""
        return self.validator("basic_order.withdraw")

    @property
    def strategy_order_hash(self) -> bytes:
        """The strategy-order constraint module hash."""
        return self.validator("strategy_order.withdraw")

    @property
    def fee_constraint_hash(self) -> bytes:
        """The fee constraint module hash (the once-per-scoop fee aggregator)."""
        return self.validator("fee_constraint.withdraw")

    @property
    def constant_sum_hash(self) -> bytes:
        """The constant-sum invariant module hash."""
        return self.validator("constant_sum.withdraw")

    def config_token(self, label: str) -> bytes:
        """The token name of the settings node labelled ``label``."""
        return bytes.fromhex(self.settings[label]["token"])

    @property
    def basic_config_token(self) -> bytes:
        """The order-config token a basic order binds (``[basic_order, fee]``)."""
        return self.config_token("basic-order")

    @property
    def strategy_config_token(self) -> bytes:
        """The order-config token a strategy order binds (``[strategy_order, fee]``)."""
        return self.config_token("strategy-order")

    @property
    def base_fee(self) -> int:
        """The flat service fee (lovelace) charged per order execution."""
        return int(self.settings["fee-settings"]["base_fee"])

    @property
    def cardano_network(self) -> Network:
        """The pycardano network the deployment's addresses are encoded for."""
        return Network.MAINNET if self.network == "mainnet" else Network.TESTNET

    @property
    def pool_address(self) -> Address:
        """The vault script address (enterprise)."""
        return Address(
            payment_part=ScriptHash(self.pool_hash),
            network=self.cardano_network,
        )

    @property
    def order_address(self) -> Address:
        """The order script address (enterprise)."""
        return Address(
            payment_part=ScriptHash(self.order_hash),
            network=self.cardano_network,
        )


INVARIANT_MODULE_KINDS: frozenset[str] = frozenset(
    {"constant_sum", "constant_product", "concentrated_liquidity"},
)
_CONFIG_LESS_COMMITMENT = b"\x80"
_NFT_PREFIX = "000de140"
_LP_PREFIX = "0014df10"


def _list_items(value: Any) -> list:  # noqa: ANN401
    """The elements of a decoded Plutus list (definite, indefinite, or frozen)."""
    return list(value)


def _reserve_unit(asset_class: AssetClass) -> str:
    """The dendrite unit of an on-chain asset class (``lovelace`` for ADA)."""
    return (asset_class.policy.hex() + asset_class.asset_name.hex()) or "lovelace"


_CONFIG_TYPES: dict[str, type[PlutusData]] = {
    "constant_sum": ConstantSumConfig,
    "constant_product": ConstantProductConfig,
    "concentrated_liquidity": ConcentratedLiquidityConfig,
    "fee_split": FeeSplitConfig,
}
_CREATE_TAG = 121  # constructor 0
_OPERATE_TAG = 122  # constructor 1


def _config_candidates(data_cbor: str, kind: str | None) -> list[Any]:
    """Config preimages a module redeemer may carry.

    A ``Create`` redeemer (constructor 0) carries the config as its first
    field; an ``Operate`` redeemer (constructor 1) carries one
    ``[pool_oref, config]`` entry per pool. Known kinds are decoded to their
    typed config; others stay :class:`~pycardano.RawPlutusData`.
    """
    raw = RawPlutusData.from_cbor(bytes.fromhex(data_cbor))
    tag = raw.data
    if not isinstance(tag, CBORTag):
        return []
    fields = _list_items(tag.value)
    if tag.tag == _CREATE_TAG and fields:
        payloads = [fields[0]]
    elif tag.tag == _OPERATE_TAG and fields:
        payloads = [
            _list_items(entry.value)[1]
            for entry in _list_items(fields[0])
            if isinstance(entry, CBORTag) and len(_list_items(entry.value)) > 1
        ]
    else:
        return []
    typed = _CONFIG_TYPES.get(kind or "")
    candidates: list[Any] = []
    for payload in payloads:
        candidate: Any = RawPlutusData(payload)
        if typed is not None:
            try:
                candidate = typed.from_cbor(candidate.to_cbor())
            except (DeserializeException, TypeError, ValueError, KeyError):
                continue
        candidates.append(candidate)
    return candidates


def _matching_config(
    records: list[RedeemerRecord],
    module_hash: bytes,
    kind: str | None,
    commitment: bytes,
) -> Any | None:  # noqa: ANN401
    """The first config among ``records`` for ``module_hash`` hashing to ``commitment``.

    Shared by both legs of :meth:`SundaeV4Vault._resolve_from_backend`: the
    producing transaction's own redeemers and, failing that, each past
    transaction's redeemers visited while walking the pool NFT's history.
    """
    for record in records:
        if not record.script_hash:
            continue
        if bytes.fromhex(record.script_hash) != module_hash:
            continue
        for candidate in _config_candidates(record.data_cbor, kind):
            if module_config_hash(candidate) == commitment:
                return candidate
    return None


class SundaeV4Vault(DendriteBaseModel):
    """A SundaeSwap V4 pool UTxO: the N-asset vault every pool type is bound to.

    Built from a raw UTxO (value bag + inline datum). ``reserves`` is the
    datum's reserve declaration re-keyed to dendrite units in canonical order
    (lovelace first, then policy + name); ``datum_units`` preserves the
    declaration order that positional module configs (constant-sum prices) are
    aligned to. ``surplus`` is the UTxO lovelace above the declared lovelace
    reserve. The vault prices nothing: :meth:`pools` yields one pool type per
    invariant module bound on the action map. ``_config_cache`` grows by one
    entry per distinct resolved module config (keyed by its commitment hash)
    across every vault in the process; :meth:`clear_config_cache` drops it.
    """

    model_config = ConfigDict(
        alias_generator=None,
        populate_by_name=True,
        arbitrary_types_allowed=True,
        extra="ignore",
    )

    tx_hash: str
    tx_index: int
    datum_cbor: str
    assets: Assets
    block_time: int = 0
    block_index: int = 0
    datum_hash: str | None = None
    identifier: bytes
    pool_nft: Assets
    lp_token: Assets
    reserves: Assets
    datum_units: list[str]
    total_lp: int
    circulating_lp: int
    preminted_lp: int
    min_surplus: int
    actions: list[ActionEntry]
    module_state: dict[bytes, bytes]
    surplus: int
    module_configs: dict[bytes, Any] = Field(default_factory=dict)

    _deployment: ClassVar[SundaeV4Deployment] = SundaeV4Deployment.for_network(
        "mainnet",
    )
    _config_cache: ClassVar[dict[bytes, Any]] = {}
    _datum: SundaeV4PoolDatum | None = PrivateAttr(default=None)

    # -- class family ---------------------------------------------------------

    @classmethod
    def select_network(cls, network: str) -> None:
        """Point the V4 class family at the deployment on ``network``."""
        SundaeV4Vault._deployment = SundaeV4Deployment.for_network(network)

    @classmethod
    def deployment(cls) -> SundaeV4Deployment:
        """The deployment the class family currently targets."""
        return cls._deployment

    @classmethod
    def dex(cls) -> str:
        """Get the DEX name."""
        return "SundaeSwapV4"

    @classmethod
    def order_selector(cls) -> list[str]:
        """The order validator address."""
        return [cls._deployment.order_address.encode()]

    @classmethod
    def pool_selector(cls) -> PoolSelector:
        """The vault validator address."""
        return PoolSelector(addresses=[cls._deployment.pool_address.encode()])

    @classmethod
    def pool_datum_class(cls) -> type[SundaeV4PoolDatum]:
        """Get the pool datum class."""
        return SundaeV4PoolDatum

    @classmethod
    def order_datum_class(cls) -> type[SundaeV4OrderDatum]:
        """Get the order datum class."""
        return SundaeV4OrderDatum

    @classmethod
    def default_script_class(cls) -> type[PlutusV3Script]:
        """V4 validators are PlutusV3 scripts."""
        return PlutusV3Script

    # -- parse ----------------------------------------------------------------

    @model_validator(mode="before")
    @classmethod
    def parse_utxo(cls, values: dict[str, Any]) -> dict[str, Any]:
        """Derive the vault fields from the UTxO value and inline datum.

        Skipped when ``identifier`` is already present (a pre-parsed vault).

        Raises:
            NotAPoolError: no inline datum, a datum that is not a pool datum, or
                a value without the pool NFT.
            InvalidPoolError: a declared reserve's quantity exceeds what is
                held (a reserve class absent from the value counts as held 0),
                or the datum declares the same reserve class more than once.
            KeyError: a supplied ``module_configs`` key names a module hash
                not installed on this vault (absent from ``module_state``).
        """
        if "identifier" in values:
            return values
        datum = cls._parse_datum(values)
        assets = values["assets"]
        if not isinstance(assets, Assets):
            assets = Assets(**assets)
            values["assets"] = assets
        policy = cls._deployment.pool_nft_policy.hex()
        nft_unit = policy + _NFT_PREFIX + datum.identifier.hex()
        lp_unit = policy + _LP_PREFIX + datum.identifier.hex()
        if assets.root.get(nft_unit) != 1:
            msg = f"SundaeV4Vault: pool NFT {nft_unit} is not in the UTxO value."
            raise NotAPoolError(msg)
        datum_units: list[str] = []
        reserves: dict[str, int] = {}
        for entry in _list_items(datum.assets):
            fields = _list_items(entry)
            unit = _reserve_unit(AssetClass.from_primitive(fields[0]))
            quantity = int(fields[1])
            held = assets.root.get(unit, 0)
            if held < quantity:
                msg = (
                    f"SundaeV4Vault: declared reserve {unit}={quantity} exceeds the "
                    f"held {held}."
                )
                raise InvalidPoolError(msg)
            datum_units.append(unit)
            reserves[unit] = quantity
        if len(set(datum_units)) != len(datum_units):
            msg = "SundaeV4Vault: the datum declares a reserve class more than once."
            raise InvalidPoolError(msg)
        values.update(
            identifier=datum.identifier,
            pool_nft=Assets(**{nft_unit: 1}),
            lp_token=Assets(**{lp_unit: assets.root.get(lp_unit, 0)}),
            reserves=Assets(**reserves),
            datum_units=datum_units,
            total_lp=datum.total_lp,
            circulating_lp=datum.circulating_lp,
            preminted_lp=datum.preminted_lp,
            min_surplus=datum.min_surplus,
            actions=list(datum.actions),
            module_state={
                bytes(_list_items(slot)[0]): bytes(_list_items(slot)[1])
                for slot in _list_items(datum.module_state)
            },
            surplus=assets.root.get("lovelace", 0) - reserves.get("lovelace", 0),
        )
        module_state: dict[bytes, bytes] = values["module_state"]
        for module_hash, config in values.get("module_configs", {}).items():
            if module_config_hash(config) != module_state[module_hash]:
                msg = (
                    f"SundaeV4Vault: supplied config for module "
                    f"{module_hash.hex()} does not match its module_state "
                    f"commitment."
                )
                raise InvalidPoolError(msg)
        return values

    @classmethod
    def _parse_datum(cls, values: dict[str, Any]) -> SundaeV4PoolDatum:
        """Decode the inline datum as a pool datum or raise ``NotAPoolError``."""
        datum_cbor = values.get("datum_cbor")
        if not datum_cbor:
            msg = "SundaeV4Vault: the UTxO carries no inline datum."
            raise NotAPoolError(msg)
        try:
            datum = SundaeV4PoolDatum.from_cbor(datum_cbor)
        except (
            DeserializeException,
            TypeError,
            ValueError,
            KeyError,
            cbor2.CBORDecodeError,
        ) as e:
            msg = f"SundaeV4Vault: datum is not a pool datum: {e}"
            raise NotAPoolError(msg) from e
        if datum.to_cbor() != bytes.fromhex(datum_cbor):
            msg = "SundaeV4Vault: datum does not round-trip as a pool datum."
            raise NotAPoolError(msg)
        return datum

    # -- identity + datum -----------------------------------------------------

    @property
    def pool_id(self) -> str:
        """The pool NFT unit."""
        return self.pool_nft.unit()

    @property
    def datum(self) -> SundaeV4PoolDatum:
        """The decoded pool datum."""
        if self._datum is None:
            self._datum = SundaeV4PoolDatum.from_cbor(self.datum_cbor)
        return self._datum

    # -- action map -----------------------------------------------------------

    def action(self, tag: int) -> ActionEntry | None:
        """The action-map entry for ``tag``, if declared."""
        for entry in self.actions:
            if entry.tag == tag:
                return entry
        return None

    def modules_for(self, tag: int) -> list[bytes]:
        """The module hashes that must run for action ``tag`` (empty if none)."""
        entry = self.action(tag)
        if entry is None:
            return []
        return [bytes(module) for module in _list_items(entry.modules)]

    def module_kind(self, module_hash: bytes) -> str | None:
        """The kind of an installed module, via the deployment's validator titles."""
        return self._deployment.module_kind(module_hash)

    def invariant_modules(self) -> list[tuple[int, bytes, str]]:
        """``(tag, module_hash, kind)`` for every enabled invariant-module binding.

        One entry per (action tag, invariant module) pair, in action-map order;
        a vault that binds the same invariant on several tags lists it once per
        tag.
        """
        out: list[tuple[int, bytes, str]] = []
        for entry in self.actions:
            if not isinstance(entry.enabled, BoolTrue):
                continue
            for module in _list_items(entry.modules):
                kind = self.module_kind(bytes(module))
                if kind in INVARIANT_MODULE_KINDS:
                    out.append((entry.tag, bytes(module), kind))
        return out

    def pools(self) -> list[SundaeV4ConstantSumPool]:
        """One pool type per enabled invariant-module binding on the action map.

        Raises:
            NotImplementedError: an invariant kind without a pool type yet.
            ModuleConfigUnavailableError: a config could not be resolved.
        """
        out: list[SundaeV4ConstantSumPool] = []
        for tag, module, kind in self.invariant_modules():
            if kind != "constant_sum":
                msg = f"SundaeV4Vault: no pool type for the {kind} module yet."
                raise NotImplementedError(msg)
            config = self.module_config(module)
            out.append(SundaeV4ConstantSumPool.from_vault(self, tag, config))
        return out

    # -- module configs -------------------------------------------------------

    @classmethod
    def clear_config_cache(cls) -> None:
        """Drop every cached module config preimage."""
        SundaeV4Vault._config_cache.clear()

    def module_config(self, module_hash: bytes) -> Any:  # noqa: ANN401
        """The config preimage committed in ``module_state[module_hash]``.

        Resolution order: supplied (constructor ``module_configs`` or
        :meth:`supply_module_config`), then the class-level cache keyed by the
        commitment hash, then the last transaction that ran the module via the
        active backend — usually the transaction that produced this vault's own
        UTxO, else found by walking the pool NFT's UTxO history (see
        :meth:`_resolve_from_backend`). A config-less module (its slot holds the
        serialised empty list) resolves to ``None``. Every resolved config is
        hash-verified.

        Raises:
            KeyError: ``module_hash`` is not installed on this vault.
            ModuleConfigUnavailableError: the backend cannot serve the preimage.
            InvalidPoolError: a served preimage does not match the commitment.
        """
        commitment = self.module_state[module_hash]
        if commitment == _CONFIG_LESS_COMMITMENT:
            return None
        if module_hash in self.module_configs:
            return self.module_configs[module_hash]
        config = self._config_cache.get(commitment)
        if config is None:
            config = self._resolve_from_backend(module_hash, commitment)
            self._config_cache[commitment] = config
        self.module_configs[module_hash] = config
        return config

    def supply_module_config(
        self,
        module_hash: bytes,
        config: Any,  # noqa: ANN401
    ) -> None:
        """Attach a caller-held config preimage after verifying its commitment.

        Raises:
            KeyError: ``module_hash`` is not installed on this vault.
            InvalidPoolError: the config does not hash to the committed value.
        """
        commitment = self.module_state[module_hash]
        if module_config_hash(config) != commitment:
            msg = (
                f"SundaeV4Vault: config for module {module_hash.hex()} does not "
                f"match its module_state commitment."
            )
            raise InvalidPoolError(msg)
        self.module_configs[module_hash] = config
        self._config_cache[commitment] = config

    def _resolve_from_backend(
        self,
        module_hash: bytes,
        commitment: bytes,
    ) -> Any:  # noqa: ANN401
        """Find the config preimage in the last transaction that ran this module.

        The transaction that produced this vault's own UTxO carries the
        module's redeemer whenever that spend was itself a scoop (an
        ``Operate`` entry) or the pool's own creation (a ``Create``). A purely
        administrative spend — a treasury sweep, an upgrade, an
        emergency-disable — need not touch a given module at all, so when the
        producing transaction carries no matching redeemer the pool NFT's UTxO
        history is walked newest to oldest, via the active backend's
        ``get_pool_utxos``, until a past transaction's redeemers commit to this
        config; the pool's creation transaction, at the bottom of that history,
        always carries the module's ``Create`` redeemer.

        Raises:
            ModuleConfigUnavailableError: the active backend cannot read
                redeemers or pool history, or no transaction in the pool's
                history carries a redeemer committing to ``commitment``.
        """
        kind = self.module_kind(module_hash)
        records = self._redeemers_or_unavailable(self.tx_hash, module_hash)
        config = _matching_config(records, module_hash, kind, commitment)
        if config is not None:
            return config
        for tx_hash in self._history_tx_hashes(module_hash):
            records = self._redeemers_or_unavailable(tx_hash, module_hash)
            config = _matching_config(records, module_hash, kind, commitment)
            if config is not None:
                return config
        msg = (
            f"SundaeV4Vault: no transaction in {self.pool_nft.unit()}'s history "
            f"carries the config committed for module {module_hash.hex()}."
        )
        raise ModuleConfigUnavailableError(msg)

    @staticmethod
    def _redeemers_or_unavailable(
        tx_hash: str,
        module_hash: bytes,
    ) -> list[RedeemerRecord]:
        """``get_redeemers(tx_hash)``, translating a backend that can't serve them."""
        try:
            return get_backend().get_redeemers(tx_hash)
        except NotImplementedError as e:
            msg = (
                f"SundaeV4Vault: the active backend cannot read redeemers, so the "
                f"config for module {module_hash.hex()} must be supplied."
            )
            raise ModuleConfigUnavailableError(msg) from e

    def _history_tx_hashes(self, module_hash: bytes) -> list[str]:
        """This pool NFT's past producing transactions, newest first.

        Excludes this vault's own ``tx_hash``. Raises
        ``ModuleConfigUnavailableError`` if the active backend cannot list
        historical pool UTxOs.
        """
        try:
            states = get_backend().get_pool_utxos(
                addresses=[self._deployment.pool_address.encode()],
                assets=[self.pool_nft.unit()],
                historical=True,
            )
        except NotImplementedError as e:
            msg = (
                f"SundaeV4Vault: the active backend cannot read pool history, so "
                f"the config for module {module_hash.hex()} must be supplied."
            )
            raise ModuleConfigUnavailableError(msg) from e
        history = sorted(
            states,
            key=lambda state: (state.block_time, state.block_index),
            reverse=True,
        )
        return [state.tx_hash for state in history if state.tx_hash != self.tx_hash]


class _SundaeV4OrderBuilders:
    """Order builders shared by every V4 pool type: basic, strategy and cancel."""

    if TYPE_CHECKING:
        vault: SundaeV4Vault

    def deployment(self) -> SundaeV4Deployment:
        """The deployment the bound vault targets."""
        return self.vault.deployment()

    @classmethod
    def pool_datum_class(cls) -> type[SundaeV4PoolDatum]:
        """Get the pool datum class."""
        return SundaeV4Vault.pool_datum_class()

    @classmethod
    def order_datum_class(cls) -> type[SundaeV4OrderDatum]:
        """Get the order datum class."""
        return SundaeV4Vault.order_datum_class()

    @classmethod
    def default_script_class(cls) -> type[PlutusV3Script]:
        """V4 validators are PlutusV3 scripts."""
        return SundaeV4Vault.default_script_class()

    @classmethod
    def order_owner(cls, address: Address) -> MultisigSignature:
        """The single-signature ``owner`` an order placed from ``address`` gets.

        Keyed on the address's stake key when it has one, otherwise on its
        payment key, so a wallet's orders share one owner across its payment
        addresses. A cancel then needs that key among the transaction's required
        signers.

        Raises:
            ValueError: if the address has no verification-key credential.
        """
        for part in (address.staking_part, address.payment_part):
            if isinstance(part, VerificationKeyHash):
                return MultisigSignature(key_hash=bytes(part))
        msg = "The order owner must be a verification-key credential."
        raise ValueError(msg)

    @classmethod
    def _fixed_destination(
        cls,
        address: Address,
        datum_target: PlutusData | None = None,
    ) -> DestinationFixed:
        """A ``DestinationFixed`` paying ``address`` with an optional inline datum."""
        datum: RawPlutusData = (
            RawPlutusData(CBORTag(121, [datum_target.to_primitive()]))
            if datum_target is not None
            else _none_option()
        )
        return DestinationFixed(
            address=PlutusFullAddress.from_address(address),
            datum=datum,
        )

    def _order_datum(
        self,
        *,
        address_source: Address,
        destination: Destination,
        config_token: bytes,
        constraints: list[tuple[bytes, PlutusData | RawPlutusData]],
        owner: MultisigScript | None,
        service_budget: int | None,
        max_per_execution: int | None,
    ) -> SundaeV4OrderDatum:
        """Assemble an order datum around a constraint list.

        Both fee fields default to the deployment's ``base_fee``: a single-shot
        order then pays exactly the current fee, and any headroom above it is
        the caller's explicit choice (a basic order's terminal fill takes
        ``min(max_per_execution, service_budget)`` in full).
        """
        base_fee = self.deployment().base_fee
        return SundaeV4OrderDatum(
            owner=owner if owner is not None else self.order_owner(address_source),
            destination=destination,
            service_budget=base_fee if service_budget is None else service_budget,
            max_per_execution=(
                base_fee if max_per_execution is None else max_per_execution
            ),
            config_token=config_token,
            constraints=IndefiniteList(
                [IndefiniteList([key, payload]) for key, payload in constraints],
            ),
            extension=_void(),
        )

    def basic_datum(
        self,
        address_source: Address,
        offered: Assets,
        min_received: Assets,
        kind: BasicConstraintKind = BasicConstraintKind.SWAP,
        *,
        address_target: Address | None = None,
        datum_target: PlutusData | None = None,
        owner: MultisigScript | None = None,
        service_budget: int | None = None,
        max_per_execution: int | None = None,
    ) -> SundaeV4OrderDatum:
        """Build the order datum for a V4 basic order of the given ``kind``.

        A basic order offers ``offered`` and requires at least ``min_received``,
        settled in one terminal fill to ``address_target`` (defaulting to
        ``address_source``). It binds the ``basic-order`` config: the constraints
        are the basic-order payload (its constructor index is ``kind``) followed
        by the fee constraint's no-op payload.
        """
        deployment = self.deployment()
        payload = _BASIC_KINDS[kind](
            offered=_asset_amounts(offered),
            min_received=_asset_amounts(min_received),
        )
        target = address_target if address_target is not None else address_source
        return self._order_datum(
            address_source=address_source,
            destination=self._fixed_destination(target, datum_target),
            config_token=deployment.basic_config_token,
            constraints=[
                (deployment.basic_order_hash, payload),
                (deployment.fee_constraint_hash, _void()),
            ],
            owner=owner,
            service_budget=service_budget,
            max_per_execution=max_per_execution,
        )

    def swap_datum(
        self,
        address_source: Address,
        in_assets: Assets,
        out_assets: Assets,
        extra_assets: Assets | None = None,
        address_target: Address | None = None,
        datum_target: PlutusData | None = None,
        *,
        owner: MultisigScript | None = None,
        service_budget: int | None = None,
        max_per_execution: int | None = None,
    ) -> SundaeV4OrderDatum:
        """Build the order datum for a V4 swap: a routing-free basic order.

        ``in_assets`` is the single asset being sold and ``out_assets`` the single
        asset asked for, at the minimum amount that must come back. The scooper
        chooses the pool(s); the order only pins the floor.

        Raises:
            ValueError: if more than one asset is offered or asked.
        """
        if len(in_assets) != 1 or len(out_assets) != 1:
            msg = "A swap offers exactly one asset and asks for exactly one asset."
            raise ValueError(msg)
        return self.basic_datum(
            address_source,
            in_assets,
            out_assets,
            BasicConstraintKind.SWAP,
            address_target=address_target,
            datum_target=datum_target,
            owner=owner,
            service_budget=service_budget,
            max_per_execution=max_per_execution,
        )

    def deposit_datum(
        self,
        address_source: Address,
        offered: Assets,
        min_lp: Assets,
        *,
        address_target: Address | None = None,
        datum_target: PlutusData | None = None,
        owner: MultisigScript | None = None,
        service_budget: int | None = None,
        max_per_execution: int | None = None,
    ) -> SundaeV4OrderDatum:
        """Build the order datum for a V4 deposit: a basic order of kind deposit.

        ``offered`` holds every pool asset (a constant-sum deposit must be
        proportional) and ``min_lp`` the floor on the pool's LP token.
        """
        return self.basic_datum(
            address_source,
            offered,
            min_lp,
            BasicConstraintKind.DEPOSIT,
            address_target=address_target,
            datum_target=datum_target,
            owner=owner,
            service_budget=service_budget,
            max_per_execution=max_per_execution,
        )

    def withdraw_datum(
        self,
        address_source: Address,
        lp: Assets,
        min_received: Assets,
        *,
        address_target: Address | None = None,
        datum_target: PlutusData | None = None,
        owner: MultisigScript | None = None,
        service_budget: int | None = None,
        max_per_execution: int | None = None,
    ) -> SundaeV4OrderDatum:
        """Build the order datum for a V4 withdrawal: a basic order of kind withdraw.

        ``lp`` is the pool's LP token being redeemed and ``min_received`` the
        per-asset floor on the proportional payout.
        """
        return self.basic_datum(
            address_source,
            lp,
            min_received,
            BasicConstraintKind.WITHDRAW,
            address_target=address_target,
            datum_target=datum_target,
            owner=owner,
            service_budget=service_budget,
            max_per_execution=max_per_execution,
        )

    def strategy_datum(
        self,
        address_source: Address,
        auth: MultisigScript,
        final_destinations: list[Address],
        *,
        address_target: Address | None = None,
        datum_target: PlutusData | None = None,
        owner: MultisigScript | None = None,
        service_budget: int | None = None,
        max_per_execution: int | None = None,
    ) -> SundaeV4OrderDatum:
        """Build the order datum for a V4 strategy order.

        A strategy order is a *signed delegation*: the datum carries a
        :class:`StrategyConstraint` (the ``auth`` multisig allowed to sign fills and
        the candidate ``final_destinations`` it may pay out to), not a concrete
        trade. The order rests paying back to itself (a :class:`DestinationSelf`
        continuation) unless ``address_target`` is given; at scoop time the
        executor supplies a signed :class:`StrategyExecution` that selects the fill
        and, when terminal, one of ``final_destinations`` by index. Deciding and
        signing that execution is off-chain executor work and is not built here.

        The order binds the ``strategy-order`` config: the constraints are the
        strategy constraint followed by the fee constraint's no-op payload.
        """
        deployment = self.deployment()
        strategy = StrategyConstraint(
            auth=auth,
            final_destinations=IndefiniteList(
                [self._fixed_destination(dest) for dest in final_destinations],
            ),
        )
        destination: Destination = (
            self._fixed_destination(address_target, datum_target)
            if address_target is not None
            else DestinationSelf()
        )
        return self._order_datum(
            address_source=address_source,
            destination=destination,
            config_token=deployment.strategy_config_token,
            constraints=[
                (deployment.strategy_order_hash, strategy),
                (deployment.fee_constraint_hash, _void()),
            ],
            owner=owner,
            service_budget=service_budget,
            max_per_execution=max_per_execution,
        )

    @classmethod
    def cancel_redeemer(cls) -> Redeemer:
        """The order spend redeemer for an owner cancel (``OrderCancel``).

        The order validator's ``Cancel`` branch only checks that the order
        ``owner`` multisig is satisfied (the owner key hash among the
        transaction's signatories), so a cancel needs no settings reference input
        and no withdraw validator — just this redeemer on the order input and the
        owner as a required signer.
        """
        return Redeemer(OrderCancel())

    @classmethod
    def cancel_tx(
        cls,
        order_utxo: UTxO,
        order_ref_utxo: UTxO,
        owner: VerificationKeyHash,
        tx_builder: TransactionBuilder,
    ) -> TransactionBuilder:
        """Add an owner-cancel of ``order_utxo`` to ``tx_builder``.

        Spends the live order UTxO with the :meth:`cancel_redeemer`, supplying the
        order validator from ``order_ref_utxo`` as a reference script (so the
        script bytes need not be embedded) and registering ``owner`` as a required
        signer so the validator's owner-multisig check is satisfied.
        """
        tx_builder.add_script_input(
            utxo=order_utxo,
            script=order_ref_utxo,
            redeemer=cls.cancel_redeemer(),
        )
        signers = list(tx_builder.required_signers or [])
        if owner not in signers:
            signers.append(owner)
        tx_builder.required_signers = signers
        return tx_builder


class SundaeV4ConstantSumPool(_SundaeV4OrderBuilders, AbstractMultiAssetPoolState):
    """The constant-sum module bound to a vault on one action tag.

    ``prices`` is the config's positional price vector re-keyed to units;
    ``reserves`` and ``total_lp`` are a snapshot of the vault's at construction
    so :meth:`apply_swap` never mutates the vault. A swap step may raise any
    subset of the reserves and lower another; the on-chain check is on value:
    ``out = floor((V_in - floor(V_in * fee)) / p_out)`` with the pool keeping
    the fee and the sub-``p_out`` remainder. That value is the only output the
    fee-band check ever admits — a lower amount fails it exactly like a higher
    one — so a step whose out reserve cannot cover it, or (with the bounty on)
    whose value increase cannot cover the obligation dock, quotes zero rather
    than a partial fill.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    vault: SundaeV4Vault
    tag: int
    config: ConstantSumConfig
    total_lp: int
    prices: dict[str, int]

    _deposit_rider: ClassVar[Assets] = Assets(lovelace=_ORDER_RIDER)

    @classmethod
    def from_vault(
        cls,
        vault: SundaeV4Vault,
        tag: int,
        config: ConstantSumConfig,
    ) -> SundaeV4ConstantSumPool:
        """Bind the constant-sum module's ``config`` to ``vault`` on action ``tag``.

        Raises:
            InvalidPoolError: the config's price vector does not cover the
                reserves, or the vault's datum order and reserve units disagree.
        """
        price_list = [int(p) for p in _list_items(config.prices)]
        if len(price_list) != len(vault.datum_units):
            msg = (
                f"SundaeV4ConstantSumPool: {len(price_list)} prices for "
                f"{len(vault.datum_units)} reserves."
            )
            raise InvalidPoolError(msg)
        if set(vault.datum_units) != set(vault.reserves.root):
            msg = (
                "SundaeV4ConstantSumPool: the vault's datum order and "
                "reserve units disagree."
            )
            raise InvalidPoolError(msg)
        return cls(
            vault=vault,
            tag=tag,
            config=config,
            reserves=Assets(**dict(vault.reserves.root)),
            total_lp=vault.total_lp,
            prices=dict(zip(vault.datum_units, price_list)),
        )

    # -- identity -------------------------------------------------------------

    @classmethod
    def dex(cls) -> str:
        """Get the DEX name."""
        return SundaeV4Vault.dex()

    @classmethod
    def pool_selector(cls) -> PoolSelector:
        """The vault validator address."""
        return SundaeV4Vault.pool_selector()

    @classmethod
    def order_selector(cls) -> list[str]:
        """The order validator address."""
        return SundaeV4Vault.order_selector()

    @property
    def pool_id(self) -> str:
        """The vault's pool NFT unit qualified by the action tag."""
        return f"{self.vault.pool_id}:{self.tag}"

    @property
    def stake_address(self) -> Address:
        """The order script address an order UTxO is locked at."""
        return self.vault.deployment().order_address

    def batcher_fee(
        self,
        in_assets: Assets | None = None,
        out_assets: Assets | None = None,
        extra_assets: Assets | None = None,
    ) -> Assets:
        """The flat ``base_fee`` an order locks for service fees."""
        return Assets(lovelace=self.vault.deployment().base_fee)

    def deposit(
        self,
        in_assets: Assets | None = None,
        out_assets: Assets | None = None,
    ) -> Assets:
        """The lovelace rider returned with the order's payout."""
        return self._deposit_rider

    # -- curve ----------------------------------------------------------------

    def _fee(self) -> tuple[int, int]:
        """The config's swap fee as ``(num, den)``."""
        return (self.config.fee.num, self.config.fee.den)

    def _bounty(self) -> tuple[int, int]:
        """The config's bounty rate as ``(num, den)`` (``num == 0`` disables it)."""
        return (self.config.bounty_k.num, self.config.bounty_k.den)

    def _value(self, reserves: dict[str, int]) -> int:
        """The pool value of a reserve vector: the reserve/price dot product."""
        return sum(reserves[u] * p for u, p in self.prices.items())

    def _imbalance(self, reserves: dict[str, int], value: int) -> int:
        """The pool imbalance: the sum of squared per-asset value deviations."""
        n = len(self.prices)
        return sum((n * reserves[u] * p - value) ** 2 for u, p in self.prices.items())

    def _dock(self, before: dict[str, int], after: dict[str, int]) -> int:
        """The obligation a step accrues, ceiled to whole value units (0 if none)."""
        k_num, k_den = self._bounty()
        if k_num == 0:
            return 0
        v_b, v_a = self._value(before), self._value(after)
        q_b, q_a = self._imbalance(before, v_b), self._imbalance(after, v_a)
        accrual = k_num * (q_a * v_b - q_b * v_a)
        if accrual <= 0:
            return 0
        n = len(self.prices)
        den = k_den * n * n * v_a * v_b
        return -(-accrual // den)

    def _check_units(self, asset: Assets, out_unit: str) -> None:
        """Validate that ``out_unit`` and every unit of ``asset`` are reserves.

        Raises:
            ValueError: ``out_unit`` is not a reserve, or ``asset`` is empty,
                offers a non-reserve or ``out_unit`` itself, or offers a
                negative quantity.
        """
        if out_unit not in self.prices:
            msg = f"out_unit {out_unit} is not a reserve of this pool."
            raise ValueError(msg)
        if len(asset) == 0:
            msg = "The offered asset is empty."
            raise ValueError(msg)
        for unit, quantity in asset.items():
            if unit not in self.prices or unit == out_unit:
                msg = f"Offered unit {unit} is not a reserve distinct from {out_unit}."
                raise ValueError(msg)
            if quantity < 0:
                msg = f"Offered quantity of {unit} is negative."
                raise ValueError(msg)

    def price(self, unit_in: str, unit_out: str) -> tuple[int, int]:
        """The fixed integer price weights ``(p_in, p_out)``.

        Raises:
            KeyError: ``unit_in`` or ``unit_out`` is not a reserve of this pool.
        """
        return (self.prices[unit_in], self.prices[unit_out])

    def _exact_quote(self, value_in: int, p_out: int) -> int:
        """The unique output the fee bracket admits for a value inflow.

        ``value_in`` is the offered side's value (already price-weighted);
        the tag-3 fee band admits exactly this output for that inflow and
        rejects every other amount, smaller or larger.
        """
        fee_num, fee_den = self._fee()
        return (value_in - value_in * fee_num // fee_den) // p_out

    def get_amount_out(
        self,
        asset: Assets,
        out_unit: str,
        precise: bool = True,
    ) -> tuple[Assets, float]:
        """Output of ``out_unit`` for offering ``asset``, and the price impact.

        The fee-consistent quote is the *only* value the tag-3 fee band ever
        admits: the band's width is exactly the floor-division remainder the
        quote already carries, so any smaller output fails it precisely like
        a larger one. A step whose out reserve cannot cover the quote, or
        whose bounty dock the quote cannot cover, is therefore unfillable in
        one step and the output is zero rather than a partial fill.

        Raises:
            ValueError: ``out_unit`` is not a reserve, or ``asset`` offers a
                non-reserve or ``out_unit`` itself.
        """
        self._check_units(asset, out_unit)
        value_in = sum(q * self.prices[u] for u, q in asset.items())
        p_out = self.prices[out_unit]
        quote = self._exact_quote(value_in, p_out)
        out = quote if quote <= self.reserves.root[out_unit] else 0
        if out > 0 and self._bounty()[0] > 0:
            before = dict(self.reserves.root)
            after = dict(before)
            for u, q in asset.items():
                after[u] += q
            after[out_unit] -= out
            if value_in - out * p_out < self._dock(before, after):
                out = 0
        out_assets = Assets(**{out_unit: out})
        if value_in == 0 or out == 0:
            return out_assets, 0.0
        return out_assets, 1.0 - (out * p_out) / value_in

    def get_amount_in(
        self,
        asset: Assets,
        in_unit: str,
        precise: bool = True,
    ) -> tuple[Assets, float]:
        """Minimum ``in_unit`` whose payout reaches the single-asset ``asset``.

        Raises:
            ValueError: ``asset`` is not exactly one reserve, or ``in_unit`` is
                not a reserve distinct from it, or the amount is not positive.
            InvalidPoolError: the desired output exceeds the reserve, or no
                input amount reaches it (every amount admissible within the
                reserve leaves the fee-consistent quote, or the bounty dock,
                short of the desired output).
        """
        if len(asset) != 1:
            msg = "The desired output must be exactly one asset."
            raise ValueError(msg)
        out_unit, desired = asset.unit(), asset.quantity()
        self._check_units(Assets(**{in_unit: 1}), out_unit)
        if desired <= 0:
            msg = "The desired output must be positive."
            raise ValueError(msg)
        reserve = self.reserves.root[out_unit]
        if desired > reserve:
            msg = f"Desired output {desired} exceeds the reserve {reserve}."
            raise InvalidPoolError(msg)
        fee_num, fee_den = self._fee()
        p_in, p_out = self.price(in_unit, out_unit)

        def produced(amount: int) -> int:
            """The output the pool pays for offering ``amount`` of ``in_unit``."""
            offer = Assets(**{in_unit: amount})
            return self.get_amount_out(offer, out_unit)[0].quantity()

        amount_max = self._amount_ceiling(reserve, p_in, p_out, fee_num, fee_den)
        amount_in = max(
            1,
            min(
                amount_max - 1,
                -(-(desired * p_out * fee_den) // (p_in * (fee_den - fee_num))),
            ),
        )
        while amount_in > 1 and produced(amount_in - 1) >= desired:
            amount_in -= 1
        while amount_in < amount_max and produced(amount_in) < desired:
            amount_in += 1
        if produced(amount_in) < desired:
            msg = (
                f"SundaeV4ConstantSumPool: no amount of {in_unit} reaches "
                f"{desired} of {out_unit} within the reserve."
            )
            raise InvalidPoolError(msg)
        in_assets = Assets(**{in_unit: amount_in})
        return in_assets, 1.0 - (desired * p_out) / (amount_in * p_in)

    def _amount_ceiling(
        self,
        reserve: int,
        p_in: int,
        p_out: int,
        fee_num: int,
        fee_den: int,
    ) -> int:
        """The smallest ``amount`` whose exact quote exceeds ``reserve``.

        ``_exact_quote(amount * p_in, p_out)`` is non-decreasing in ``amount``,
        so this ceiling is found by binary search: an initial closed-form
        estimate is doubled until it overshoots, then bisected down to the
        exact boundary. No amount at or beyond this ceiling can ever be
        admitted, so it bounds the ``get_amount_in`` search without an
        arbitrary iteration cap.
        """
        hi = -(-((reserve + 1) * p_out * fee_den) // (p_in * (fee_den - fee_num))) + 1
        while self._exact_quote(hi * p_in, p_out) <= reserve:
            hi *= 2
        lo = 1
        while lo < hi:
            mid = (lo + hi) // 2
            if self._exact_quote(mid * p_in, p_out) > reserve:
                hi = mid
            else:
                lo = mid + 1
        return lo

    def apply_swap(self, asset_in: Assets, asset_out: Assets) -> None:
        """Move every touched reserve as if the swap settled.

        Raises:
            KeyError: ``asset_in`` or ``asset_out`` names a unit that is not a
                reserve of this pool.
        """
        for unit, quantity in asset_in.items():
            self.reserves.root[unit] = self.reserves.root[unit] + quantity
        for unit, quantity in asset_out.items():
            self.reserves.root[unit] = self.reserves.root[unit] - quantity

    # -- liquidity ------------------------------------------------------------

    def pinned_deposit(self, offered: Assets) -> PinnedDeposit:
        """The proportional deposit the vault accepts for ``offered``.

        ``deltas`` is aligned to the vault's declaration order
        (``vault.datum_units``). Every reserve must be offered.
        """
        order = self.vault.datum_units
        return constant_sum_pinned_deposit(
            reserves=[self.reserves.root[u] for u in order],
            prices=[self.prices[u] for u in order],
            offered=[offered.root.get(u, 0) for u in order],
            total_lp=self.total_lp,
        )

    def pinned_withdraw(self, lp: int) -> PinnedWithdraw:
        """The proportional payout for redeeming ``lp`` LP tokens.

        ``payouts`` is aligned to the vault's declaration order.
        """
        order = self.vault.datum_units
        return constant_sum_pinned_withdraw(
            reserves=[self.reserves.root[u] for u in order],
            prices=[self.prices[u] for u in order],
            lp=lp,
            total_lp=self.total_lp,
        )


def _void() -> RawPlutusData:
    """The empty constructor-0 record: the no-op payload and default extension."""
    return RawPlutusData(CBORTag(121, []))


def _none_option() -> RawPlutusData:
    """An ``Option<Data>`` ``None`` (constructor 1): a bare destination datum."""
    return RawPlutusData(CBORTag(122, []))


def _asset_class(unit: str) -> AssetClass:
    """The on-chain asset class of a dendrite unit (``lovelace`` is empty/empty)."""
    if unit == "lovelace":
        return AssetClass(policy=b"", asset_name=b"")
    return AssetClass(
        policy=bytes.fromhex(unit[:56]),
        asset_name=bytes.fromhex(unit[56:]),
    )


def _asset_amounts(assets: Assets) -> IndefiniteList:
    """An Aiken ``List<(AssetClass, Int)>`` in canonical (policy, name) order."""
    ordered = sorted(
        assets.root.items(),
        key=lambda item: "" if item[0] == "lovelace" else item[0],
    )
    return IndefiniteList(
        [IndefiniteList([_asset_class(unit), int(amount)]) for unit, amount in ordered],
    )


SundaeV4Vault.model_rebuild()
SundaeV4ConstantSumPool.model_rebuild()
