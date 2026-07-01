# noqa
import json
from enum import Enum

from pycardano import Address
from pycardano import PlutusV2Script
from pycardano import RawPlutusData
from pycardano import TransactionId
from pycardano import TransactionInput
from pycardano import TransactionOutput
from pycardano import UTxO
from pycardano import Value
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import RootModel
from pydantic import model_serializer
from pydantic import model_validator
from pydantic.alias_generators import to_camel
from pydantic_core import core_schema


class DendriteBaseModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    def __hash__(self) -> int:
        return hash(self.model_dump_json())


class PoolSelectorType(Enum):
    """How to identify a pool.

    DEX pools are generally identified by one of two mechanism:
    1. An address
    2. Presence of one or more NFTs (asset policy, with or without asset name)
    """

    address = "addresses"
    asset = "assets"


class PoolSelector(DendriteBaseModel):
    """Pool selection information for dbsync."""

    addresses: list[str]
    assets: list[str] | None = None


class BaseList(RootModel):
    """Utility class for list models."""

    def __hash__(self) -> int:
        return hash(self.model_dump_json())

    def __iter__(self):  # noqa
        return iter(self.root)

    def __getitem__(self, item):  # noqa
        return self.root[item]

    def __len__(self):  # noqa
        return len(self.root)


class BaseDict(BaseList):
    """Utility class for dict models."""

    def items(self):  # noqa: ANN201
        """Return iterable of key-value pairs."""
        return self.root.items()

    def keys(self):  # noqa: ANN201
        """Return iterable of keys."""
        return self.root.keys()

    def values(self):  # noqa: ANN201
        """Return iterable of values."""
        return self.root.values()

    def __getitem__(self, item: str):  # noqa: ANN204
        """Get item by key."""
        return self.root.get(item, 0)


def _digest_assets(value: object) -> dict[str, int]:
    """Normalize an ``Assets`` input to a lovelace-first ``dict[str, int]``.

    Accepts the same shapes the old pydantic ``_digest_assets`` before-validator did:
    another ``Assets`` / anything with ``.root``, a ``{"values": [...]}`` mapping of
    objects with ``unit``/``quantity``, a list of length-1 dicts, a plain dict, or
    kwargs (an items-able).
    """
    if hasattr(value, "root"):
        root = dict(value.root)
    elif (
        isinstance(value, dict)
        and "values" in value
        and isinstance(value["values"], list)
    ):
        root = {v.unit: v.quantity for v in value["values"]}
    elif isinstance(value, list) and value and isinstance(value[0], dict):
        if not all(len(v) == 1 for v in value):
            raise ValueError(
                "For a list of dictionaries, each dictionary must be of length 1.",
            )
        root = {k: v for d in value for k, v in d.items()}
    elif isinstance(value, dict):
        root = dict(value)
    else:
        root = dict(value.items())  # type: ignore[attr-defined]
    return dict(sorted(root.items(), key=lambda x: "" if x[0] == "lovelace" else x[0]))


class Assets:
    """Contains all tokens and quantities.

    A plain (non-pydantic) ``unit -> quantity`` mapping with lovelace-first ordering.
    ``.root`` is a live, mutable, insertion-ordered ``dict``: the pool math mutates it
    in place (``apply_swap``, finite-difference probes, and the non-ADA min-ADA append
    that must NOT re-sort), so it stays a real dict rather than an opaque model field.
    Dropping the pydantic ``RootModel`` removes the per-construction validation +
    accessor overhead that dominates tight construction/access loops;
    ``__get_pydantic_core_schema__`` keeps it usable as a field type on the pydantic
    pool-state models.
    """

    __slots__ = ("root",)

    def __init__(self, *args: object, **kwargs: object) -> None:
        """Build from a positional value, a ``root=`` dict, or unit kwargs."""
        if args:
            value: object = args[0]
        elif len(kwargs) == 1 and "root" in kwargs:
            value = kwargs["root"]
        else:
            value = kwargs
        self.root: dict[str, int] = _digest_assets(value)

    def items(self):  # noqa: ANN201
        """Return iterable of key-value pairs."""
        return self.root.items()

    def keys(self):  # noqa: ANN201
        """Return iterable of keys."""
        return self.root.keys()

    def values(self):  # noqa: ANN201
        """Return iterable of values."""
        return self.root.values()

    def __iter__(self):  # noqa: ANN204
        """Iterate over the units (keys)."""
        return iter(self.root)

    def __getitem__(self, item: str) -> int:
        """Get quantity by unit (missing -> 0)."""
        return self.root.get(item, 0)

    def __len__(self) -> int:
        """Number of distinct units."""
        return len(self.root)

    def __contains__(self, item: str) -> bool:
        """Whether ``item`` is a present unit."""
        return item in self.root

    def unit(self, index: int = 0) -> str:
        """Units of asset at `index`."""
        return next(iter(self.root)) if index == 0 else list(self.root)[index]

    def quantity(self, index: int = 0) -> int:
        """Quantity of the asset at `index`."""
        if index == 0:
            return next(iter(self.root.values()))
        return list(self.root.values())[index]

    def __add__(self, b: "Assets") -> "Assets":
        """Add two assets."""
        keys = set(self.keys()) | set(b.keys())
        return Assets(**{key: self[key] + b[key] for key in keys})

    def __sub__(self, b: "Assets") -> "Assets":
        """Subtract two assets."""
        keys = set(self.keys()) | set(b.keys())
        return Assets(**{key: self[key] - b[key] for key in keys})

    def __eq__(self, other: object) -> bool:
        """Equal iff the same ``unit -> quantity`` mapping."""
        if isinstance(other, Assets):
            return self.root == other.root
        return NotImplemented

    def __hash__(self) -> int:
        """Hash of the canonical ``(unit, quantity)`` items."""
        return hash(tuple(self.root.items()))

    def __repr__(self) -> str:
        """Debug representation."""
        return f"Assets(root={self.root!r})"

    # --- pydantic-model API surface the pool code calls (no pydantic engine) ---
    def model_dump(self, **_kwargs: object) -> dict[str, int]:
        """Return the ``unit -> quantity`` dict (mirrors ``RootModel.model_dump``)."""
        return dict(self.root)

    def model_dump_json(self, **_kwargs: object) -> str:
        """Return the mapping as a compact JSON object string."""
        return json.dumps(self.root, separators=(",", ":"))

    def copy(self, **_kwargs: object) -> "Assets":
        """Return an independent copy (fresh dict, order preserved)."""
        return self.model_construct(root=dict(self.root))

    def model_copy(self, **_kwargs: object) -> "Assets":
        """Return an independent copy (fresh dict, order preserved)."""
        return self.model_construct(root=dict(self.root))

    @classmethod
    def model_validate(cls, obj: object, **_kwargs: object) -> "Assets":
        """Coerce ``obj`` to ``Assets`` (an existing instance passes through)."""
        return obj if isinstance(obj, cls) else cls(obj)

    @classmethod
    def model_validate_json(cls, data: str, **_kwargs: object) -> "Assets":
        """Parse a JSON object string into ``Assets``."""
        return cls(json.loads(data))

    @classmethod
    def model_construct(
        cls,
        root: dict[str, int] | None = None,
        **kwargs: int,
    ) -> "Assets":
        """No-sort, no-validate construction (mirrors pydantic ``model_construct``).

        Callers pass an already-canonical ``root`` (e.g. ``reset_assets`` in the hot
        path), so this must NOT re-sort — it sets ``.root`` verbatim.
        """
        obj = cls.__new__(cls)
        obj.root = root if root is not None else dict(kwargs)
        return obj

    # --- pydantic field-type protocol (models with `assets: Assets` still validate) ---
    @classmethod
    def __get_pydantic_core_schema__(
        cls,
        _source: object,
        _handler: object,
    ) -> object:
        """Validate/serialize as a plain dict inside pydantic models."""
        return core_schema.no_info_plain_validator_function(
            cls._pyd_validate,
            serialization=core_schema.plain_serializer_function_ser_schema(
                lambda a: dict(a.root),
                when_used="always",
            ),
        )

    @classmethod
    def _pyd_validate(cls, value: object) -> "Assets":
        """Coerce a validation input to ``Assets`` (instance passthrough)."""
        return value if isinstance(value, cls) else cls(value)


class ScriptReference(DendriteBaseModel):
    tx_hash: str | None
    tx_index: int | None
    address: str | None
    assets: Assets | None
    datum_hash: str | None
    datum_cbor: str | None
    script: str | None

    def to_utxo(self) -> UTxO | None:
        if self.tx_hash is not None:
            assert self.tx_index is not None
            assert self.script is not None or self.datum_cbor is not None
            assert self.assets is not None
            assert self.address is not None
            return UTxO(
                input=TransactionInput(
                    transaction_id=TransactionId(bytes.fromhex(self.tx_hash)),
                    index=self.tx_index,
                ),
                output=TransactionOutput(
                    address=Address.decode(self.address),
                    amount=Value(coin=self.assets["lovelace"]),
                    script=(
                        None
                        if self.script is None
                        else PlutusV2Script(bytes.fromhex(self.script))
                    ),
                    datum=(
                        None
                        if self.datum_cbor is None
                        else RawPlutusData.from_cbor(self.datum_cbor)
                    ),
                ),
            )
        else:
            return None


class BlockInfo(DendriteBaseModel):
    epoch_slot_no: int
    block_no: int
    tx_count: int
    block_time: int


class BlockList(BaseList):
    root: list[BlockInfo]


class PoolStateInfo(DendriteBaseModel):
    address: str
    tx_hash: str
    tx_index: int
    block_time: int
    block_index: int
    block_hash: str
    datum_hash: str
    datum_cbor: str
    assets: Assets
    plutus_v2: bool


class PoolStateList(BaseList):
    root: list[PoolStateInfo]


class SwapSubmitInfo(DendriteBaseModel):
    address_inputs: list[str] = Field(..., alias="submit_address_inputs")
    address_stake: str = Field(..., alias="submit_address_stake")
    assets: Assets = Field(..., alias="submit_assets")
    block_hash: str = Field(..., alias="submit_block_hash")
    block_time: int = Field(..., alias="submit_block_time")
    block_index: int = Field(..., alias="submit_block_index")
    datum_hash: str = Field(..., alias="submit_datum_hash")
    datum_cbor: str = Field(..., alias="submit_datum_cbor")
    metadata: list[list | dict | str | int | None] | None = Field(
        ...,
        alias="submit_metadata",
    )
    tx_hash: str = Field(..., alias="submit_tx_hash")
    tx_index: int = Field(..., alias="submit_tx_index")


class SwapExecuteInfo(DendriteBaseModel):
    address: str
    tx_hash: str
    tx_index: int
    block_time: int
    block_index: int
    block_hash: str
    assets: Assets


class SwapStatusInfo(DendriteBaseModel):
    swap_input: SwapSubmitInfo
    swap_output: SwapExecuteInfo | PoolStateInfo | None = None

    @model_validator(mode="before")
    def from_dbsync(cls, values: dict) -> dict:
        swap_input = SwapSubmitInfo.model_validate(values)

        if "datum_cbor" in values and values["datum_cbor"] is not None:
            swap_output = PoolStateInfo.model_validate(values)
        elif "address" in values and values["address"] is not None:
            swap_output = SwapExecuteInfo.model_validate(values)
        else:
            swap_output = None

        return {
            "swap_input": swap_input,
            "swap_output": swap_output,
        }

    @model_serializer(mode="plain", when_used="always")
    def to_dbsync(self) -> dict:
        output = {key: None for key in PoolStateInfo.model_fields}
        if self.swap_output is not None:
            output.update(self.swap_output.model_dump())

        return self.swap_input.model_dump(by_alias=True) | output


class SwapTransactionInfo(BaseList):
    root: list[SwapStatusInfo]

    @model_validator(mode="before")
    def from_dbsync(cls, values: list):
        if not all(
            item["submit_tx_hash"] == values[0]["submit_tx_hash"] for item in values
        ):
            raise ValueError(
                "All transaction info must have the same submission transaction.",
            )
        return values


class SwapTransactionList(BaseList):
    root: list[SwapTransactionInfo]

    @model_validator(mode="before")
    def from_dbsync(cls, values: list):
        if len(values) == 0:
            return []

        output = []

        tx_hash = values[0]["submit_tx_hash"]
        start = 0
        for end, record in enumerate(values):
            if record["submit_tx_hash"] == tx_hash:
                continue

            output.append(values[start:end])

            start = end
            tx_hash = record["submit_tx_hash"]

        if start < len(values):
            output.append(values[start : end + 1])

        return output


class OrderType(Enum):
    swap = 0
    deposit = 1
    withdraw = 2


class TokenSummary(DendriteBaseModel):
    """Summary of token information."""

    ticker: str
    name: str
    policy_id: str
    policy_name: str
    decimals: int
    price_numerator: int = 0
    price_denominator: int = 0
