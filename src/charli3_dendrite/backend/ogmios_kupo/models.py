"""Pydantic models for the Kupo response data."""

from enum import Enum
from typing import Optional
from typing import Union

from pydantic import Field  # type: ignore
from pydantic import RootModel

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


class KupoDatumResponse(DendriteBaseModel):
    """Kupo response for datum requests."""

    datum: str


class KupoScriptResponse(DendriteBaseModel):
    """Kupo response for script requests."""

    script: str
    language: Optional[str] = None


class KupoGenericResponse(RootModel):
    """Root model for generic Kupo response."""

    root: Union[list[KupoResponse], KupoDatumResponse, KupoScriptResponse, dict]

    @classmethod
    def model_validate(cls: type, obj: Union[list, dict]) -> "KupoGenericResponse":
        """Validate the input object and return the appropriate model."""
        if isinstance(obj, list):
            return cls(root=obj)
        if isinstance(obj, dict):
            if "datum" in obj:
                return cls(root=KupoDatumResponse(**obj))
            if "script" in obj:
                return cls(root=KupoScriptResponse(**obj))

        return cls(root=obj)
