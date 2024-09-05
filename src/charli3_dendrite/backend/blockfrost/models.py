"""Pydantic models for the Blockfrost API."""

from typing import Optional

from charli3_dendrite.dataclasses.models import BaseList
from charli3_dendrite.dataclasses.models import DendriteBaseModel


class AssetAmount(DendriteBaseModel):
    """Model for the asset amount in a UTxO."""

    unit: str
    quantity: str


class UTxO(DendriteBaseModel):
    """Model for the UTxO data."""

    address: str
    tx_hash: str
    output_index: int
    amount: list[AssetAmount]
    block: str
    data_hash: Optional[str] = None
    inline_datum: Optional[str] = None
    reference_script_hash: Optional[str] = None


class UTxOList(BaseList):
    """Model for the UTxO list data."""

    root: list[UTxO]


class TransactionInfo(DendriteBaseModel):
    """Model for the transaction info data."""

    block_time: int
    index: int
    block: str


class BlockFrostBlockInfo(DendriteBaseModel):
    """Model for the block info data."""

    time: int
    height: int
