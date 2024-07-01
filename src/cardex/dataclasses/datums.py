# noqa
"""Dataclasses for the different datums used in the Cardex project."""
from abc import ABC
from abc import abstractmethod
from dataclasses import dataclass
from typing import Union

from pycardano import Address
from pycardano import DatumHash
from pycardano import PlutusData
from pycardano import VerificationKeyHash

from cardex.dataclasses.models import Assets
from cardex.dataclasses.models import OrderType


@dataclass
class ReceiverDatum(PlutusData):
    """The receiver address."""

    CONSTR_ID = 0
    datum_hash: Union[DatumHash, None] = None


@dataclass
class PlutusPartAddress(PlutusData):
    """Encode a plutus address part (i.e. payment, stake, etc)."""

    CONSTR_ID = 0
    address: bytes


@dataclass
class PlutusScriptPartAddress(PlutusPartAddress):
    """Encode a plutus address part (i.e. payment, stake, etc)."""

    CONSTR_ID = 1


@dataclass
class PlutusNone(PlutusData):
    """Placeholder for a receiver datum."""

    CONSTR_ID = 1


@dataclass
class _PlutusConstrWrapper(PlutusData):
    """Hidden wrapper to match Minswap stake address constructs."""

    CONSTR_ID = 0
    wrapped: Union["_PlutusConstrWrapper", PlutusPartAddress, PlutusScriptPartAddress]


@dataclass
class PlutusFullAddress(PlutusData):
    """A full address, including payment and staking keys."""

    CONSTR_ID = 0
    payment: Union[PlutusPartAddress, PlutusScriptPartAddress]
    stake: Union[_PlutusConstrWrapper, PlutusNone]

    @classmethod
    def from_address(cls, address: Address) -> "PlutusFullAddress":
        """Parse an Address object to a PlutusFullAddress."""
        error_msg = "Only addresses with staking and payment parts are accepted."
        if None in [address.staking_part, address.payment_part]:
            raise ValueError(error_msg)
        if address.staking_part is not None:
            stake = _PlutusConstrWrapper(
                _PlutusConstrWrapper(
                    PlutusPartAddress(bytes.fromhex(str(address.staking_part))),
                ),
            )
        else:
            stake = PlutusNone
        return PlutusFullAddress(
            PlutusPartAddress(bytes.fromhex(str(address.payment_part))),
            stake=stake,
        )

    def to_address(self) -> Address:
        """Convert back to an address."""
        payment_part = VerificationKeyHash(self.payment.address[:28])
        if isinstance(self.stake, PlutusNone):
            stake_part = None
        else:
            stake_part = VerificationKeyHash(self.stake.wrapped.wrapped.address[:28])
        return Address(payment_part=payment_part, staking_part=stake_part)


@dataclass
class PlutusScriptAddress(PlutusFullAddress):
    """A full address, including payment and staking keys."""

    payment: PlutusScriptPartAddress


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
        if len(asset) != 1:
            raise ValueError(error_msg)

        if asset.unit() == "lovelace":
            policy = b""
            asset_name = b""
        else:
            policy = bytes.fromhex(asset.unit()[:56])
            asset_name = bytes.fromhex(asset.unit()[56:])

        return AssetClass(policy=policy, asset_name=asset_name)

    @property
    def assets(self) -> Assets:
        """Convert back to assets."""
        if self.policy.hex() == "":
            asset = "lovelace"
        else:
            asset = self.policy.hex() + self.asset_name.hex()

        return Assets(root={asset: 0})


@dataclass
class CancelRedeemer(PlutusData):
    """Cancel datum."""

    CONSTR_ID = 1


class PoolDatum(PlutusData, ABC):
    """Abstract base class for all pool datum types."""

    CONSTR_ID = 0

    @abstractmethod
    def pool_pair(self) -> Union[Assets, None]:
        """Return the asset pair associated with the pool."""
        pass


class OrderDatum(PlutusData, ABC):
    """Abstract base class for all order datum types."""

    CONSTR_ID: int = 0

    @abstractmethod
    def address_source(self) -> Address:
        """This method should return the source address associated with the order."""
        pass

    @abstractmethod
    def requested_amount(self) -> Assets:
        """This method should return the amount requested in the order."""
        pass

    @abstractmethod
    def order_type(self) -> OrderType:
        """This method should return the type of the order."""
        pass
