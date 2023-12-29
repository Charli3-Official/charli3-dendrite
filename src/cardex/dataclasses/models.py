# noqa
from enum import Enum

from pydantic import BaseModel
from pydantic import RootModel
from pydantic import model_validator


class PoolSelectorType(Enum):
    """How to identify a pool.

    DEX pools are generally identified by one of two mechanism:
    1. An address
    2. Presence of one or more NFTs (asset policy, with or without asset name)
    """

    address = "addresses"
    asset = "assets"


class PoolSelector(BaseModel):
    """Pool selection information for dbsync."""

    selector_type: PoolSelectorType
    selector: list[str]

    def to_dict(self) -> dict[str, list[str]]:
        """Dump the model to a dictionary for use in dbsync methods."""
        return {self.selector_type.value: self.selector}


class BaseList(RootModel):
    """Utility class for list models."""

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


class Assets(BaseDict):
    """Contains all tokens and quantities."""

    root: dict[str, int]

    def unit(self, index: int = 0) -> str:
        """Units of asset at `index`."""
        return list(self.keys())[index]

    def quantity(self, index: int = 0) -> int:
        """Quantity of the asset at `index`."""
        return list(self.values())[index]

    @model_validator(mode="before")
    def _digest_assets(cls, values: dict) -> dict:
        if hasattr(values, "root"):
            root = values.root
        elif "values" in values and isinstance(values["values"], list):
            root = {v.unit: v.quantity for v in values["values"]}
        elif isinstance(values, list) and isinstance(values[0], dict):
            if not all(len(v) == 1 for v in values):
                raise ValueError(
                    "For a list of dictionaries, each dictionary must be of length 1.",
                )
            root = dict(map(dict.popitem, values))
        else:
            root = dict(values.items())

        return dict(
            sorted(root.items(), key=lambda x: "" if x[0] == "lovelace" else x[0]),
        )

    def __add__(a: "Assets", b: "Assets") -> "Assets":
        """Add two assets."""
        intersection = set(a.keys()) | set(b.keys())

        result = {key: a[key] + b[key] for key in intersection}

        return Assets(**result)

    def __sub__(a: "Assets", b: "Assets") -> "Assets":
        """Subtract two assets."""
        intersection = set(a.keys()) | set(b.keys())

        result = {key: a[key] - b[key] for key in intersection}

        return Assets(**result)


class BlockInfo(BaseModel):
    epoch_slot_no: int
    block_no: int
    tx_count: int
    block_time: int


class BlockList(BaseList):
    root: list[BlockInfo]


class PoolStateInfo(BaseModel):
    address: str
    tx_hash: str
    tx_index: int
    block_time: int
    block_index: int
    block_hash: str
    datum_hash: str
    datum_cbor: str | None
    assets: Assets | None
    plutus_v2: bool


class PoolStateList(BaseList):
    root: list[PoolStateInfo]
