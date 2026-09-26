"""FluidTokens V4 read-only UTxO states.

One state class per V4 UTxO kind, each built from a raw UTxO record with
:meth:`from_record` and no backend call. Pools and loans implement the shared lending
bases through the V3 state classes, since V4 did not change the loan math; requests
reuse the V3 request view; the manager UTxOs get small views of their own.

Identities are identity-token asset names (hex):

- a pool and its pool manager share the pool NFT's asset name;
- a loan's datum ``origin_id`` is ``b"POOL"`` plus that name (or ``b"REQUEST"`` plus a
  request NFT name), so ``loan.pool_id == pool.pool_id`` without a lookup;
- a lender bond, the loan it was minted with, and the loan NFT share an asset name.

Identity policies are this package's constants: a policy change is a new deployment.
"""

from __future__ import annotations

from typing import Any
from typing import ClassVar
from typing import NamedTuple
from typing import TypeVar

from pycardano import PlutusData
from pydantic import PrivateAttr

from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dataclasses.models import DendriteBaseModel
from charli3_dendrite.dataclasses.models import PoolSelector
from charli3_dendrite.dataclasses.models import PoolStateInfo
from charli3_dendrite.lending.fluidtokens.datums import ORIGIN_POOL_TAG
from charli3_dendrite.lending.fluidtokens.datums import ORIGIN_REQUEST_TAG
from charli3_dendrite.lending.fluidtokens.market import FluidMarket
from charli3_dendrite.lending.fluidtokens.state import FluidLoanState
from charli3_dendrite.lending.fluidtokens.state import FluidPoolState
from charli3_dendrite.lending.fluidtokens.state import FluidRequestState
from charli3_dendrite.lending.fluidtokens_v4 import constants as c
from charli3_dendrite.lending.fluidtokens_v4.datums import AssetManagerDatumWithHash
from charli3_dendrite.lending.fluidtokens_v4.datums import AssetManagerDatumWithToken
from charli3_dendrite.lending.fluidtokens_v4.datums import CollateralAsset
from charli3_dendrite.lending.fluidtokens_v4.datums import LenderManagerDatum
from charli3_dendrite.lending.fluidtokens_v4.datums import LoanDatum
from charli3_dendrite.lending.fluidtokens_v4.datums import LockedBorrowerManagerDatum
from charli3_dendrite.lending.fluidtokens_v4.datums import PoolDatum
from charli3_dendrite.lending.fluidtokens_v4.datums import PoolManagerDatum
from charli3_dendrite.lending.fluidtokens_v4.datums import RequestDatum
from charli3_dendrite.lending.fluidtokens_v4.datums import TxOutRef
from charli3_dendrite.lending.fluidtokens_v4.datums import decode_asset_manager_datum
from charli3_dendrite.lending.oracles.models import OracleRef
from charli3_dendrite.lending.oracles.models import OracleSource
from charli3_dendrite.lending.units import asset_unit
from charli3_dendrite.lending.units import constr
from charli3_dendrite.lending.units import script_payment_address

PROTOCOL_NAME = "FluidTokensV4"

# A permissionless pool or request stores this marker as its condition script hash.
PERMISSIONLESS_MARKER = b"NONE"

# AuthorizationMethod constructor alternatives, in declaration order.
AUTH_KINDS = ("signature", "spend_script", "withdraw_script", "mint_script")

_POLICY_HEX_LEN = 56

# RepaymentMode constructor alternative -> number of integer fields it carries.
_REPAYMENT_ARITY = {0: 1, 1: 0, 2: 2}

_S = TypeVar("_S", bound="FluidV4Record")


class AuthMethod(NamedTuple):
    """A decoded ``AuthorizationMethod``: its kind (see ``AUTH_KINDS``) and hex hash."""

    kind: str
    hash_hex: str


def _constr(raw: object, what: str) -> tuple[int, list]:
    """``constr`` that reports any malformed value as ``ValueError``."""
    try:
        return constr(raw)
    except (ValueError, TypeError, IndexError) as exc:
        raise ValueError(f"not a {what}: {raw!r}") from exc


def auth_method(raw: object) -> AuthMethod:
    """Decode an ``AuthorizationMethod`` field (``ValueError`` if it is not one)."""
    alt, fields = _constr(raw, "AuthorizationMethod")
    if not 0 <= alt < len(AUTH_KINDS) or len(fields) != 1:
        raise ValueError(f"not an AuthorizationMethod: {raw!r}")
    if not isinstance(fields[0], bytes):
        raise ValueError(f"not an AuthorizationMethod: {raw!r}")
    return AuthMethod(kind=AUTH_KINDS[alt], hash_hex=fields[0].hex())


def decode_repayment_mode(raw: object) -> tuple[int, tuple[int, ...]]:
    """Decode a ``RepaymentMode`` field into (alternative, its integer fields).

    Raises ``ValueError`` when it is not a RepaymentMode.
    """
    alt, fields = _constr(raw, "RepaymentMode")
    arity = _REPAYMENT_ARITY.get(alt)
    if arity is None or len(fields) != arity:
        raise ValueError(f"not a RepaymentMode: {raw!r}")
    if not all(isinstance(f, int) and not isinstance(f, bool) for f in fields):
        raise ValueError(f"not a RepaymentMode: {raw!r}")
    return alt, tuple(fields)


def collateral_asset_unit(collateral: CollateralAsset) -> str:
    """Unit of a ``CollateralAsset``; its ``Option<AssetName>`` None is policy only.

    Raises ``ValueError`` when the optional asset name is malformed.
    """
    alt, fields = _constr(collateral.maybe_asset_name, "Option<AssetName>")
    if alt == 0 and len(fields) == 1 and isinstance(fields[0], bytes):
        return asset_unit(collateral.policy_id, fields[0])
    if alt == 1 and not fields:
        return asset_unit(collateral.policy_id, b"")
    raise ValueError(f"not an Option<AssetName>: {collateral.maybe_asset_name!r}")


def out_ref_str(ref: TxOutRef) -> str:
    """``tx_hash#index`` of a datum output reference."""
    return f"{ref.tx_id.hex()}#{ref.index}"


def names_under(assets: Assets, policy: str) -> list[str]:
    """Asset names (hex, sorted) held under ``policy``."""
    return sorted(
        unit[_POLICY_HEX_LEN:]
        for unit in assets.root
        if unit != "lovelace" and unit[:_POLICY_HEX_LEN] == policy
    )


def identity_name(assets: Assets, policy: str) -> str | None:
    """Asset name (hex) of the one identity token held under ``policy``.

    A genuine protocol UTxO holds exactly one token under its identity policy, with
    quantity 1 and a non-empty name. Anything else returns None.
    """
    names = names_under(assets, policy)
    if len(names) != 1 or not names[0]:
        return None
    if assets.root[policy + names[0]] != 1:
        return None
    return names[0]


class FluidV4Record:
    """Out-ref accessor and raw-record constructor shared by every V4 state.

    ``DERIVED_VIEWS`` names the properties :meth:`from_record` evaluates once. They
    read every untyped datum field the state's views and math use, so a datum that
    decodes but carries a malformed value there is rejected when the record is parsed
    rather than when a view is first read.
    """

    DERIVED_VIEWS: ClassVar[tuple[str, ...]] = ()

    @property
    def out_ref(self) -> str:
        """``tx_hash#tx_index`` of this UTxO."""
        return f"{self.tx_hash}#{self.tx_index}"  # type: ignore[attr-defined]

    @classmethod
    def from_record(cls: type[_S], info: PoolStateInfo) -> _S:  # noqa: PYI019
        """Build from a raw UTxO record and decode its datum.

        Raises whatever the datum decoder raises when the datum is not this kind's.
        """
        names = cls.model_fields  # type: ignore[attr-defined]
        values = {name: getattr(info, name) for name in names if hasattr(info, name)}
        state = cls(**values)
        state.decoded_datum()
        for view in cls.DERIVED_VIEWS:
            getattr(state, view)
        return state

    def decoded_datum(self) -> PlutusData:
        """This UTxO's datum, decoded as its kind's type."""
        raise NotImplementedError


class FluidV4PoolState(FluidV4Record, FluidPoolState):
    """A V4 pool UTxO: the principal liquidity it holds and the loan terms it offers."""

    DERIVED_VIEWS: ClassVar[tuple[str, ...]] = (
        "repayment_terms",
        "collateral_units",
        "market",
        "is_permissioned",
    )

    _market: FluidMarket | None = PrivateAttr(default=None)

    @classmethod
    def protocol(cls) -> str:
        """Protocol name."""
        return PROTOCOL_NAME

    @classmethod
    def pool_policy(cls) -> list[str] | None:
        """Pool NFT policy."""
        return [c.POOL_POLICY]

    @classmethod
    def pool_datum_class(cls) -> type[PlutusData]:
        """Pool datum type."""
        return PoolDatum

    @classmethod
    def pool_selector(cls) -> PoolSelector:
        """Pool UTxOs sit at the pool spend script's payment credential."""
        return PoolSelector(addresses=[script_payment_address(c.POOL_SPEND_SKH)])

    def decoded_datum(self) -> PlutusData:
        """The pool datum."""
        return self.pool_datum

    @property
    def market(self) -> FluidMarket:
        """Loan-terms view of this pool's datum (derived on first use)."""
        if self._market is None:
            datum: PoolDatum = self.pool_datum  # type: ignore[assignment]
            self._market = FluidMarket.from_pool_datum(datum)
        return self._market

    @property
    def pool_id(self) -> str:
        """Pool NFT asset name (hex); empty when the UTxO holds no single pool NFT."""
        return identity_name(self.assets, c.POOL_POLICY) or ""

    @property
    def is_permissioned(self) -> bool:
        """True when borrowing requires an extra condition script."""
        datum: PoolDatum = self.pool_datum  # type: ignore[assignment]
        return datum.permissioned_condition_script_hash != PERMISSIONLESS_MARKER

    @property
    def repayment_terms(self) -> tuple[int, tuple[int, ...]]:
        """(RepaymentMode alternative, its integer fields) of the pool's loan terms."""
        datum: PoolDatum = self.pool_datum  # type: ignore[assignment]
        return decode_repayment_mode(datum.common_data.repayment_mode)

    @property
    def collateral_units(self) -> list[str]:
        """Units of the collateral options, in datum order."""
        datum: PoolDatum = self.pool_datum  # type: ignore[assignment]
        return [collateral_asset_unit(option) for option in datum.collateral_options]


class FluidV4LoanState(FluidV4Record, FluidLoanState):
    """A V4 loan UTxO: one borrower position.

    Every loan term is copied into the loan datum, so debt and health evaluate from the
    loan alone; the origin pool may already be cancelled. Set the evaluation time with
    :meth:`set_time` (or ``attach_context``) before reading debt or health.
    """

    DERIVED_VIEWS: ClassVar[tuple[str, ...]] = (
        "origin",
        "borrowed_unit",
        "collateral_unit",
        "repayment_terms",
    )

    @classmethod
    def protocol(cls) -> str:
        """Protocol name."""
        return PROTOCOL_NAME

    @classmethod
    def protocol_nft_policy(cls) -> list[str] | None:
        """Loan NFT policy."""
        return [c.LOAN_POLICY]

    @classmethod
    def loan_datum_class(cls) -> type[PlutusData]:
        """Loan datum type."""
        return LoanDatum

    @classmethod
    def loan_selector(cls) -> PoolSelector:
        """Loan UTxOs sit at the loan spend script's payment credential."""
        return PoolSelector(addresses=[script_payment_address(c.LOAN_SPEND_SKH)])

    def decoded_datum(self) -> PlutusData:
        """The loan datum."""
        return self.loan_datum

    def set_time(self, now_ms: int) -> None:
        """Set the POSIX-ms time that debt and health factor are evaluated at."""
        self._now_ms = now_ms

    def current_debt(self) -> int:
        """Principal plus interest accrued to the evaluation time.

        Raises ``ValueError`` when no evaluation time has been set.
        """
        if self._now_ms <= 0:
            raise ValueError("set_time(now_ms) must be called before evaluating debt")
        return super().current_debt()

    @property
    def loan_id(self) -> str:
        """Loan NFT asset name (hex); also the asset name of its lender bond."""
        return identity_name(self.assets, c.LOAN_POLICY) or ""

    @property
    def origin(self) -> tuple[str, str]:
        """(``"pool"`` / ``"request"`` / ``"unknown"``, origin NFT asset name hex)."""
        ld: LoanDatum = self.loan_datum  # type: ignore[assignment]
        origin_id = ld.origin_id
        for kind, tag in (("pool", ORIGIN_POOL_TAG), ("request", ORIGIN_REQUEST_TAG)):
            if origin_id.startswith(tag):
                return kind, origin_id[len(tag) :].hex()
        return "unknown", origin_id.hex()

    @property
    def pool_id(self) -> str:
        """Pool NFT asset name this loan was borrowed from; empty otherwise."""
        kind, name = self.origin
        return name if kind == "pool" else ""

    @property
    def collateral_unit(self) -> str:
        """Unit of the loan's collateral asset."""
        ld: LoanDatum = self.loan_datum  # type: ignore[assignment]
        return collateral_asset_unit(ld.collateral)

    @property
    def repayment_terms(self) -> tuple[int, tuple[int, ...]]:
        """(RepaymentMode alternative, its integer fields) of the loan."""
        ld: LoanDatum = self.loan_datum  # type: ignore[assignment]
        return decode_repayment_mode(ld.repayment_mode)

    def _collateral_unit(self) -> str:
        """The validated collateral unit, for the inherited value and health math."""
        return self.collateral_unit

    def oracle_refs(self) -> list[OracleRef]:
        """The collateral price feed the liquidation check needs.

        Only a loan with an LTV liquidation mode can be liquidated for its value, and
        the contract requires every collateral option of such a loan to carry an oracle
        token, so exactly those loans need a collateral price.
        """
        if self._liquidation_ltv() is None:
            return []
        return [
            OracleRef(
                source=OracleSource.FLUID_AGGREGATED,
                token=self._collateral_unit(),
                quote=self.borrowed_unit,
            ),
        ]


class FluidV4RequestState(FluidV4Record, FluidRequestState):
    """A V4 borrow-request UTxO."""

    DERIVED_VIEWS: ClassVar[tuple[str, ...]] = (
        "principal_unit",
        "collateral_unit",
        "is_dynamic",
        "repayment_terms",
    )

    tx_hash: str
    tx_index: int
    block_time: int
    block_index: int
    datum_hash: str = ""

    @classmethod
    def request_datum_class(cls) -> type[RequestDatum]:
        """Request datum type."""
        return RequestDatum

    def decoded_datum(self) -> PlutusData:
        """The request datum."""
        return self.request_datum

    @property
    def collateral_unit(self) -> str:
        """Collateral asset unit offered (Option-None name -> policy only)."""
        return collateral_asset_unit(self.request_datum.collateral)

    @property
    def repayment_terms(self) -> tuple[int, tuple[int, ...]]:
        """(RepaymentMode alternative, its integer fields) of the requested terms."""
        return decode_repayment_mode(self.request_datum.common_data.repayment_mode)

    @property
    def request_id(self) -> str:
        """Request NFT asset name (hex); empty when the UTxO holds none."""
        return identity_name(self.assets, c.REQUEST_POLICY) or ""


class FluidV4UtxoState(FluidV4Record, DendriteBaseModel):
    """Common shape of the V4 UTxOs that are neither pools, loans nor requests."""

    address: str
    assets: Assets
    tx_hash: str
    tx_index: int
    block_time: int
    block_index: int
    datum_cbor: str
    datum_hash: str = ""

    _datum: Any = PrivateAttr(default=None)

    @classmethod
    def decode_datum(cls, datum_cbor: str) -> PlutusData:
        """Decode this kind's datum."""
        raise NotImplementedError

    def decoded_datum(self) -> PlutusData:
        """This UTxO's datum (decoded on first use)."""
        if self._datum is None:
            self._datum = self.decode_datum(self.datum_cbor)
        return self._datum


class FluidV4PoolManagerState(FluidV4UtxoState):
    """A pool-manager UTxO: who may edit / cancel its pool, and the compounding fee."""

    DERIVED_VIEWS: ClassVar[tuple[str, ...]] = (
        "owner_auth",
        "compounding_fee_per_mille",
    )

    @classmethod
    def decode_datum(cls, datum_cbor: str) -> PlutusData:
        """Decode a pool-manager datum."""
        return PoolManagerDatum.from_cbor(datum_cbor)

    @property
    def datum(self) -> PoolManagerDatum:
        """The pool-manager datum."""
        return self.decoded_datum()  # type: ignore[return-value]

    @property
    def pool_id(self) -> str:
        """Asset name (hex) shared by this manager's NFT and its pool's NFT."""
        return identity_name(self.assets, c.POOL_MANAGER_POLICY) or ""

    @property
    def owner_auth(self) -> AuthMethod:
        """Who may edit or cancel the pool."""
        return auth_method(self.datum.pool_owner_auth)

    @property
    def compounding_fee_per_mille(self) -> int:
        """Share of compounded principal paid to the compounding bot, per mille."""
        return self.datum.compounding_fee_per_mille


class FluidV4AssetManagerState(FluidV4UtxoState):
    """An asset-manager UTxO: a payment held for the owner of a bond or an auth."""

    DERIVED_VIEWS: ClassVar[tuple[str, ...]] = (
        "action",
        "input_out_ref",
        "owner_unit",
        "owner_auth",
    )

    @classmethod
    def decode_datum(cls, datum_cbor: str) -> PlutusData:
        """Decode either asset-manager datum variant."""
        return decode_asset_manager_datum(datum_cbor)

    @property
    def datum(self) -> AssetManagerDatumWithToken | AssetManagerDatumWithHash:
        """The asset-manager datum."""
        return self.decoded_datum()  # type: ignore[return-value]

    @property
    def action(self) -> bytes:
        """Action tag that created this payment (e.g. ``b"installment_repayment"``)."""
        return self.datum.action

    @property
    def input_out_ref(self) -> str:
        """Out-ref of the input this payment settles."""
        return out_ref_str(self.datum.input_output_reference)

    @property
    def owner_unit(self) -> str | None:
        """Unit of the owner token (token variant only)."""
        datum = self.datum
        if isinstance(datum, AssetManagerDatumWithToken):
            return datum.owner_asset.unit()
        return None

    @property
    def owner_auth(self) -> AuthMethod | None:
        """Owner authorization (hash variant only)."""
        datum = self.datum
        if isinstance(datum, AssetManagerDatumWithHash):
            return auth_method(datum.owner_auth)
        return None


class FluidV4LenderManagerState(FluidV4UtxoState):
    """A lender-manager UTxO: lender bonds held for automated claims."""

    DERIVED_VIEWS: ClassVar[tuple[str, ...]] = (
        "lender_auth",
        "pool_id",
        "principal_unit",
    )

    @classmethod
    def decode_datum(cls, datum_cbor: str) -> PlutusData:
        """Decode a lender-manager datum."""
        return LenderManagerDatum.from_cbor(datum_cbor)

    @property
    def datum(self) -> LenderManagerDatum:
        """The lender-manager datum."""
        return self.decoded_datum()  # type: ignore[return-value]

    @property
    def lender_bond_names(self) -> list[str]:
        """Asset names of the lender bonds held; each equals its loan's ``loan_id``."""
        return names_under(self.assets, c.LENDER_BOND_POLICY)

    @property
    def lender_auth(self) -> AuthMethod:
        """Who may withdraw the bonds."""
        return auth_method(self.datum.lender_auth)

    @property
    def pool_id(self) -> str:
        """Pool NFT asset name compounding targets; empty disables compounding."""
        return self.datum.pool_id.hex()

    @property
    def principal_unit(self) -> str:
        """Principal unit the managed loans must share."""
        return self.datum.principal_asset.unit()

    @property
    def liquidation_fee_per_mille(self) -> int:
        """Share of liquidated collateral paid to the liquidating bot, per mille."""
        return self.datum.liquidation_fee_per_mille


class FluidV4LockedBorrowerState(FluidV4UtxoState):
    """A borrower bond locked at a borrower-bond destination script."""

    DERIVED_VIEWS: ClassVar[tuple[str, ...]] = (
        "origin_out_ref",
        "borrower_auth",
    )

    @classmethod
    def decode_datum(cls, datum_cbor: str) -> PlutusData:
        """Decode a locked-borrower-manager datum."""
        return LockedBorrowerManagerDatum.from_cbor(datum_cbor)

    @property
    def datum(self) -> LockedBorrowerManagerDatum:
        """The locked-borrower-manager datum."""
        return self.decoded_datum()  # type: ignore[return-value]

    @property
    def borrower_bond_names(self) -> list[str]:
        """Asset names of the borrower bonds held."""
        return names_under(self.assets, c.BORROWER_BOND_POLICY)

    @property
    def origin_out_ref(self) -> str:
        """Out-ref the bond was locked from."""
        return out_ref_str(self.datum.origin_ref)

    @property
    def borrower_auth(self) -> AuthMethod:
        """Who may spend the bond."""
        return auth_method(self.datum.borrower_auth)
