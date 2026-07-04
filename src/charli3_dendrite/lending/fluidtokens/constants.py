"""FluidTokens V3 mainnet coordinates.

The protocol is parameterized by a single config NFT; the config UTxO datum holds
every protocol script hash / policy id. The constants below are the live mainnet
deployment coordinates (a static fallback), and `resolve_addresses` re-reads the live
config datum through the backend so entity discovery self-heals across a redeploy,
falling back to the constants when the datum cannot be read (e.g. a non-dbsync
backend).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import TypedDict

import cbor2  # type: ignore[import-not-found]
from pycardano import Address
from pycardano import Network
from pycardano import ScriptHash

if TYPE_CHECKING:
    from charli3_dendrite.backend.backend_base import AbstractBackend

PROTOCOL_CONFIG_NFT_POLICY = "219832152b2c489358f4c02a1818d312a851b1f55774ae881e33a907"
PROTOCOL_CONFIG_NFT_NAME = "706172616d6574657273"  # "parameters"

# Mainnet deployment: config-datum field -> hash / policy id.
POOL_POLICY = "befbcb19919ff8ce5323d123c835da8e7653a098ad482271a72b72f2"
REQUEST_POLICY = "a37578f027ae878115cc70cd0909ddc855d67b6dd3bd038a757bd221"
BORROWER_BOND_POLICY = "eadc69a5d2d1357acc9b9d49ec5390fcdf6e080c7a40139917223dcb"
LENDER_BOND_POLICY = "bcd713bb7858d4b08738bed90ee7068d8f9b38d02e0cae0b45ac7a9b"
LOAN_POLICY = "30f1095a8a2acb68bb0ffa193e18e004b6dd3e12b5d9c2375a1d5c41"
REPAYMENT_POLICY = "1fb02a2a8f89d1484141e57bd370587773b3dbd69d45fec93a6b2a94"

POOL_SPEND_SKH = "ad353a777c817f4d9d6c4324930f5c6128400517ec9dae0461e034cd"
REQUEST_SPEND_SKH = "dc9003272dbd7fc5d19ce4f0eb3a92bec2c4ffcbd58c8ce4493888bc"
LOAN_SPEND_SKH = "5abbaa2eb177b574707fa3617e3436295d45d7795e0874623a9504da"
LOAN_CLAIM_ACTION_SKH = "79cc329af69d79ba85f56a04c0e8512306eb7ed98a5bd0c878485910"
LOAN_REPAY_ACTION_SKH = "5fcd23d021add45fdb25e8c7c674d6e2dbc445feafe246c5a0fab02f"
LOAN_CHANGE_COLLATERAL_ACTION_SKH = (
    "6ca3b95015bbef6237db91322132ebd3ac0c3850f79096eb6f42e1af"
)
LOAN_RECAST_ACTION_SKH = "696d17c79d1276945d0e96d2368a7c48b431afbe880c008da8fee0f2"
ASSET_MANAGER_SPEND_SKH = "e20678c018fe01a9b0d116a5a1bfa57e2efe8520645ca303d2c26a0e"

# Mainnet bech32 addresses where each entity's UTxOs live (payment cred = the
# general_spend instance for that entity; stake part carries the action withdraw cred).
POOL_ADDRESS = "addr1zxkn2wnh0jqh7nvad3pjfyc0t3sjssq9zlkfmtsyv8srfnfhmnz9plgputfxf9gqn86lwgyk0qu3n59haevkkdx0klmqspeu5h"  # noqa: E501
LOAN_ADDRESS = "addr1z9dth23wk9mm2ars073kzl35xc5463wh090qsarz822sfksvadkn2cntfnulwwvs7nq44jmrlzdfhtyvate92x7k67ysyhqtnf"  # noqa: E501
REQUEST_ADDRESS = "addr1z8wfqqe89k7hl3w3nnj0p6e6j2lv938le02cer8yfyug30xfmmkg85yfq8c7nrysz63g2sq8ttmt8gn7jmgfqy995d4s48shzc"  # noqa: E501


class FluidScriptAddresses(TypedDict):
    """Resolved entity coordinates used by the loader/builder."""

    pool: str
    loan: str
    request: str
    pool_policy: str
    loan_policy: str
    request_policy: str


# Config datum layout: Constr(0, [ ... ]). Fields 0-1 are governance credentials; 2-15
# are the policy ids / script hashes below; 16-21 are reserved (currently empty / zero).
_CONFIG_CONSTR_0 = 121
_CONFIG_MIN_FIELDS = 16
_CONFIG_FIELD_IX = {
    "pool_policy": 2,
    "request_policy": 3,
    "borrower_bond_policy": 4,
    "lender_bond_policy": 5,
    "loan_policy": 6,
    "repayment_policy": 7,
    "pool_spend_skh": 8,
    "request_spend_skh": 9,
    "loan_spend_skh": 10,
    "loan_claim_action_skh": 11,
    "loan_repay_action_skh": 12,
    "loan_change_collateral_action_skh": 13,
    "loan_recast_action_skh": 14,
    "asset_manager_spend_skh": 15,
}


@dataclass(frozen=True)
class FluidConfig:
    """The protocol policy ids / script hashes carried by the config NFT datum.

    Every field is a hex hash. :meth:`parse` decodes the on-chain config datum;
    :meth:`defaults` returns the static mainnet constants defined in this module, used
    as a fallback when the live datum cannot be read.
    """

    pool_policy: str
    request_policy: str
    borrower_bond_policy: str
    lender_bond_policy: str
    loan_policy: str
    repayment_policy: str
    pool_spend_skh: str
    request_spend_skh: str
    loan_spend_skh: str
    loan_claim_action_skh: str
    loan_repay_action_skh: str
    loan_change_collateral_action_skh: str
    loan_recast_action_skh: str
    asset_manager_spend_skh: str

    @classmethod
    def parse(cls, datum_cbor: str) -> FluidConfig:
        """Decode the config NFT datum cbor-hex into a :class:`FluidConfig`.

        Reads the known policy/script-hash fields (2-15) and tolerates the reserved
        tail (16+). Raises :class:`ValueError` if the datum is not the expected
        ``Constr(0, [...])`` with at least the known fields, all bytes.
        """
        try:
            top = cbor2.loads(bytes.fromhex(datum_cbor))
        except (ValueError, cbor2.CBORDecodeError) as exc:
            raise ValueError(f"config datum: undecodable cbor: {exc}") from exc
        if not isinstance(top, cbor2.CBORTag) or top.tag != _CONFIG_CONSTR_0:
            raise ValueError("config datum: expected Constr(0, ...)")
        fields = top.value
        if not isinstance(fields, list) or len(fields) < _CONFIG_MIN_FIELDS:
            raise ValueError(
                f"config datum: expected >= {_CONFIG_MIN_FIELDS} fields",
            )

        def _hash(name: str) -> str:
            value = fields[_CONFIG_FIELD_IX[name]]
            if not isinstance(value, bytes):
                raise ValueError(f"config datum: field {name} is not bytes")
            return value.hex()

        return cls(**{name: _hash(name) for name in _CONFIG_FIELD_IX})

    @classmethod
    def defaults(cls) -> FluidConfig:
        """The static mainnet constants defined in this module."""
        return cls(
            pool_policy=POOL_POLICY,
            request_policy=REQUEST_POLICY,
            borrower_bond_policy=BORROWER_BOND_POLICY,
            lender_bond_policy=LENDER_BOND_POLICY,
            loan_policy=LOAN_POLICY,
            repayment_policy=REPAYMENT_POLICY,
            pool_spend_skh=POOL_SPEND_SKH,
            request_spend_skh=REQUEST_SPEND_SKH,
            loan_spend_skh=LOAN_SPEND_SKH,
            loan_claim_action_skh=LOAN_CLAIM_ACTION_SKH,
            loan_repay_action_skh=LOAN_REPAY_ACTION_SKH,
            loan_change_collateral_action_skh=LOAN_CHANGE_COLLATERAL_ACTION_SKH,
            loan_recast_action_skh=LOAN_RECAST_ACTION_SKH,
            asset_manager_spend_skh=ASSET_MANAGER_SPEND_SKH,
        )


def _payment_address(script_hash_hex: str) -> str:
    """A mainnet payment-credential (enterprise) address for a spend script hash.

    Entity UTxOs sit at base addresses whose stake part is not carried by the config
    datum, but the backend discovers them by payment credential only, so a
    payment-credential address is the stable, redeploy-proof identity to match on.
    """
    return Address(
        payment_part=ScriptHash(bytes.fromhex(script_hash_hex)),
        network=Network.MAINNET,
    ).encode()


def resolve_config(backend: AbstractBackend) -> FluidConfig:
    """Read the live config NFT datum into a :class:`FluidConfig`.

    Falls back to :meth:`FluidConfig.defaults` when the datum cannot be read or parsed
    (e.g. a non-dbsync backend, a missing config UTxO, or an unexpected datum shape) so
    the loader keeps working offline / against a degraded backend.
    """
    try:
        # Imported lazily: `resolve` imports this module, so a top-level import cycles.
        from charli3_dendrite.lending.fluidtokens.transactions.resolve import (
            resolve_config_utxo,
        )

        utxo = resolve_config_utxo(backend)
        return FluidConfig.parse(utxo.datum)
    except (TypeError, ValueError, IndexError, KeyError, AttributeError):
        return FluidConfig.defaults()


def resolve_addresses(backend: AbstractBackend) -> FluidScriptAddresses:
    """Resolve the entity coordinate set, preferring the live config NFT datum.

    Re-reads the config datum through `backend` so pool / loan / request discovery
    self-heals across a protocol redeploy (new spend scripts / policies); falls back to
    the static mainnet constants when the datum cannot be read. Addresses are
    payment-credential identities (see :func:`_payment_address`).
    """
    config = resolve_config(backend)
    return FluidScriptAddresses(
        pool=_payment_address(config.pool_spend_skh),
        loan=_payment_address(config.loan_spend_skh),
        request=_payment_address(config.request_spend_skh),
        pool_policy=config.pool_policy,
        loan_policy=config.loan_policy,
        request_policy=config.request_policy,
    )
