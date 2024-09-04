"""Pydantic models for the Kupo response data."""

from enum import Enum
from typing import Optional

from pydantic import Field  # type: ignore
from pydantic import RootModel

from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dataclasses.models import DendriteBaseModel


class CreatedAtSpentAt(DendriteBaseModel):
    """Created at and spent at times for Kupo response."""

    slot_no: int
    header_hash: str


class AssetValue(DendriteBaseModel):
    """Asset value for Kupo response."""

    coins: int
    assets: dict[str, int] = Field(default_factory=dict)


class DatumType(str, Enum):
    """Datum type for Kupo response."""

    HASH = "hash"
    INLINE = "inline"


class KupoResponse(DendriteBaseModel):
    """Kupo response data model."""

    transaction_index: int
    transaction_id: str
    output_index: int
    address: str
    value: AssetValue
    datum_hash: Optional[str] = None
    datum_type: Optional[DatumType] = None
    script_hash: Optional[str] = None
    created_at: CreatedAtSpentAt
    spent_at: Optional[CreatedAtSpentAt] = None


class KupoResponseList(RootModel):
    """Root model for Kupo response list."""

    root: list[KupoResponse]


class PoolStateInfo(DendriteBaseModel):
    """Pool state info for Kupo response."""

    address: str
    tx_hash: str
    tx_index: int
    block_time: int
    block_index: int
    block_hash: str
    datum_hash: Optional[str] = None
    datum_cbor: str = ""
    assets: Assets
    plutus_v2: bool
