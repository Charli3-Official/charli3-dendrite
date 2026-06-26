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

* the action-map tag (``ActionEntry.tag`` / ``PoolAction.tag``): pool-instance
  defined; only 0/1/2 are reserved by :data:`PoolRedeemer`, higher tags are
  pool-defined;
* the transcript operation tag (``TranscriptEntry.operation_tag``): per-invariant
  module (constant-sum uses 3 = swap, 5 = swap+claim, 6 = deposit);
* the order constraint tag (the inner constructor index of an order constraint
  entry): 0 = deposit, 1 = withdraw, 2 = swap, 3 = claim.
"""

from dataclasses import dataclass
from typing import Union

from pycardano import IndefiniteList
from pycardano import PlutusData
from pycardano import RawPlutusData

from charli3_dendrite.dataclasses.datums import AssetClass
from charli3_dendrite.dataclasses.datums import PlutusFullAddress

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
    not present in the resting datum.

    Field order is access-frequency ordered on chain (the first four fields are
    read by modules every step) and is load-bearing.
    """

    CONSTR_ID = 0
    assets: IndefiniteList
    total_lp: int
    circulating_lp: int
    preminted_lp: int
    identifier: bytes
    actions: list[ActionEntry]
    module_state: IndefiniteList


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


PoolRedeemer = Union[EscapeHatch, Upgrade, EmergencyDisable, PoolAction]


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
    ``destination`` is where proceeds are paid. ``budget`` is the maximum scooper
    fee in lovelace. ``share_batcher`` is the batcher's basis-points cut of the
    fee surplus. ``config_token`` is the token name of the order-config settings
    entry that governs this order; the per-entry ``constraint_module_hash`` keys
    below are exactly that settings entry's required-constraint set.
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
    budget: int
    share_batcher: int
    config_token: bytes
    constraints: IndefiniteList
    extension: RawPlutusData


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
    """

    CONSTR_ID = 0
    prices: IndefiniteList
    fee: Rational
    bounty_k: Rational


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
