# noqa
from dataclasses import dataclass

from pycardano import PlutusData

from cardex.dataclasses.models import Assets


@dataclass
class AssetClass(PlutusData):
    """An asset class. Separates out token policy and asset name."""

    CONSTR_ID = 0

    policy: bytes
    asset_name: bytes

    @classmethod
    def from_assets(cls, asset: Assets) -> "AssetClass":
        """Parse an Assets object into an AssetClass object."""
        error_msg = "Only one asset may be supplied."
        if len(asset) == 1:
            raise ValueError(error_msg)

        if asset.unit() == "lovelace":
            policy = b""
            asset_name = b""
        else:
            policy = bytes.fromhex(asset.unit()[:56])
            asset_name = bytes.fromhex(asset.unit()[56:])

        return AssetClass(policy=policy, asset_name=asset_name)
