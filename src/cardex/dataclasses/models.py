from typing import Optional

from pydantic import BaseModel
from pydantic import RootModel
from pydantic import model_validator


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

    def __getitem__(self, item):
        return self.root.get(item, 0)


class Assets(BaseDict):
    """Contains all tokens and quantities."""

    root: dict[str, int] = {}

    def unit(self, index: int = 0) -> str:
        """Units of asset at `index`."""
        return list(self.keys())[index]

    def quantity(self, index: int = 0) -> int:
        """Quantity of the asset at `index`."""
        return list(self.values())[index]

    @model_validator(mode="before")
    def _digest_assets(cls, values):
        if hasattr(values, "root"):
            root = values.root
        elif "values" in values and isinstance(values["values"], list):
            root = {v.unit: v.quantity for v in values["values"]}
        else:
            root = {k: v for k, v in values.items()}
        root = dict(
            sorted(root.items(), key=lambda x: "" if x[0] == "lovelace" else x[0]),
        )

        return root

    def __add__(a, b):
        """Add two assets."""
        intersection = set(a.keys()) | set(b.keys())

        result = {key: a[key] + b[key] for key in intersection}

        return Assets(**result)

    def __sub__(a, b):
        """Subtract two assets."""
        intersection = set(a.keys()) | set(b.keys())

        result = {key: a[key] - b[key] for key in intersection}

        return Assets(**result)


class BlockInfo(BaseModel):
    epoch_slot_no: int
    block_no: int
    tx_index: int
    block_time: int

    @classmethod
    def from_dbsync(cls, item: tuple):
        return cls(
            epoch_slot_no=item[0],
            block_no=item[1],
            tx_index=item[2],
            block_time=item[3],
        )


class BlockList(BaseList):
    root: list[BlockInfo]

    @classmethod
    def from_dbsync(cls, items: list[tuple]):
        return cls(root=[BlockInfo.from_dbsync(item) for item in items])


class PoolStateInfo(BaseModel):
    address: str
    tx_hash: str
    tx_index: int
    block_time: int
    block_hash: str
    datum_hash: str
    datum_cbor: Optional[str]
    assets: Assets

    @classmethod
    def from_dbsync(cls, item: tuple):
        assets = Assets(lovelace=item[7], **{a["unit"]: a["quantity"] for a in item[8]})

        return cls(
            address=item[0],
            tx_hash=item[1],
            tx_index=item[2],
            block_time=item[3],
            block_hash=item[4],
            datum_hash=item[5],
            datum_cbor=item[6],
            assets=assets,
        )


class PoolStateList(BaseList):
    root: list[PoolStateInfo]

    @classmethod
    def from_dbsync(cls, items: list[tuple]):
        return cls(root=[PoolStateInfo.from_dbsync(item) for item in items])
