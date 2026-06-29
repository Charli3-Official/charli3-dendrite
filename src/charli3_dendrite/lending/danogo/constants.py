"""Danogo deployment anchors + on-chain address resolution.

Anchors are None until supplied. With an anchor, `resolve_addresses` reads the Protocol
Config UTxO's ProtocolDatum and derives the four script addresses.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import TypedDict

from pycardano import Address
from pycardano import Network
from pycardano import ScriptHash

from charli3_dendrite.lending.danogo.datums import ProtocolDatum
from charli3_dendrite.lending.danogo.oracles.locator import LeafHandle

if TYPE_CHECKING:
    from charli3_dendrite.backend.backend_base import AbstractBackend

# Deployment anchors (Cardano mainnet). NFT anchors are policy(56 hex)+asset_name(hex);
# the path anchor is policy-only. The Oracle Global Config NFT and the Oracle Path FTs
# share a single minting policy.
PROTOCOL_CONFIG_NFT: str | None = (
    "bc2d3b7cf1009c788b1daf2208a38026ab20d7f8209d896ad1a09c14"
    "e567b64450c406a17f869eb4e430f2e2a6c74d103edb988cb1bc1347de285c69"
)
ORACLE_GLOBAL_CONFIG_NFT: str | None = (
    "bd23e2977937bb6054f625e5b566e28e294dcd8f7e8d0d33979ed90c"
    "1e5e4d7ac0ba99f5f4b3d3c7764054ccce87ef497407785a4ee04d2beadb0b78"
)
ORACLE_PATH_FT_POLICY: str | None = (
    "bd23e2977937bb6054f625e5b566e28e294dcd8f7e8d0d33979ed90c"
)

# Per-supply-token oracle routing anchor. Each supply token's price config is anchored
# to one Danogo reference pool: the `OraclePathDatum.anchor` (28 bytes) is the terminal
# bridge that converts every collateral price into the supply token. Many path-config
# UTxOs (across older config versions and other supply tokens) share the path FT policy;
# the create-loan oracle Withdraw validator only accepts the path datum anchored to the
# borrowed market's supply token. Keyed by supply-token unit ("lovelace" for ADA).
ORACLE_QUOTE_ANCHOR: dict[str, str] = {
    "lovelace": "836cc68931c2e4e3e838602eca1902591d216837bafddfe6f0c8cb07",
    # USDM-supply market routing anchor.
    "c48cbb3d5e57ed56e276bc45f99ab39abe94e6cd7ac39fb402da47ad0014df105553444d": (
        "64e648e02d76567652d55e33106123f72ceac04bf3dcaf4f70737d58"
    ),
    # DjedMicroUSD-supply market routing anchor.
    (
        "8db269c3ec630e06ae29f74bc39edd1f87c819f1056206e879a1cd61"
        "446a65644d6963726f555344"
    ): "504791c3a0348e8ebadac99d5df8753b1b12e3849b8bd69653e5db92",
    # USDC-supply market routing anchor.
    (
        "1f3aec8bfe7ea4fe14c5f121e2a92e301afe414147860d557cac7e34" "5553444378"
    ): "853fca625371e99f0e0b94663f3513dd99ec141bac5dad8128b41db6",
    # USDA-supply market routing anchor.
    (
        "fe7c786ab321f41c654ef6c1af7b3250a613c24e4213e0425a7ae456" "55534441"
    ): "a8fe3b72f0195c5af7a9df18402ba5ec77938f5795da7bb738653c9d",
}

# Minswap V2 LP minting policy. The oracle path config encodes a Minswap LP source as
# the LP token *name* (32 bytes) under this policy; matching a borrowed collateral's
# resolved Minswap LP leaf to a path config's sources confirms the config prices it.
MINSWAP_LP_POLICY: str = "f5808c2c990d86da54bfc97d89cee6efa20cd8461616359478d96b4c"

# Curated cross-check oracle feeds per cross-quote supply token (keyed by supply-token
# unit). A cross-quote market's ``ada -> quote`` conversion is deviation-checked by the
# routing path config against independent external price feeds that the mined pricing
# recipes never reference (no recipe step names them), so the recipe-driven leaf
# resolution does not fetch them. The oracle ``Withdraw`` still walks them, so a
# fully-live cross-quote build must additionally fetch these feed UTxOs; the compose
# filter then references only the candidate whose rate is within the path config's
# deviation tolerance. Each handle is resolved through the same single-UTxO
# discriminator the recipe leaves use. Only cross-quote supply tokens (those with a
# canonical ``ada -> quote`` recipe) appear here; ADA-quote / single-hop markets need
# none.
CROSS_CHECK_SUPPLEMENTAL_HANDLES: dict[str, tuple[LeafHandle, ...]] = {
    # USDCx-supply market: Indigo iUSD ``ada -> quote`` feed (a singleton UTxO pinned
    # by its feed NFT at a stable script address), parsed as ``OracleUtxoType.TINDIGO``.
    "1f3aec8bfe7ea4fe14c5f121e2a92e301afe414147860d557cac7e345553444378": (
        LeafHandle(
            otype="TINDIGO",
            address="addr1wygyy4mdrh5kxsmm6ja4phxez3vh2mngz2muqmw4gw3n9jqdu67a0",
            nft_policy="e3455f2715338b454fb853442f72dc03b98396854f97510027fe22ff",
            nft_name="695553443230323231313138313935393036",
        ),
    ),
}

# Script credentials that hold the config/oracle reference UTxOs. Backends match
# UTxOs by payment credential and don't support NFT-only lookups, so resolution
# starts from these addresses: PROTOCOL_CONFIG_SKH holds the Protocol Config UTxO,
# and ORACLE_DATA_SKH holds the Oracle Global Config UTxO and the Oracle Path FTs.
PROTOCOL_CONFIG_SKH: str | None = (
    "9a0054cf2ab678edcd7528d16a388e5b1cefa3af40d5b9634ab96de6"
)
ORACLE_DATA_SKH: str | None = "79a5116ee2ff13ffd53b2615568574fc804740a07bc2ff8930d2d6fb"


class DanogoScriptAddresses(TypedDict):
    """The four Danogo script addresses derived from the ProtocolDatum."""

    pool: str
    loan: str
    config_pool: str
    oracle: str


def _addr(skh_hex: str) -> str:
    # Returns a payment-credential (enterprise, no-stake) script *identity*. On-chain
    # pool/loan UTxOs may sit at base addresses with a stake part, so callers must match
    # by payment credential, not raw address-string equality (some backends match raw
    # address strings).
    return Address(
        payment_part=ScriptHash(bytes.fromhex(skh_hex)),
        network=Network.MAINNET,
    ).encode()


def resolve_addresses(
    backend: AbstractBackend,
    *,
    config_nft: str | None = None,
    config_address: str | None = None,
) -> DanogoScriptAddresses:
    """Read ProtocolDatum at the Protocol Config UTxO; return the 4 script addresses.

    Backends match UTxOs by payment credential and don't support NFT-only lookups, so
    resolution needs the address that holds the Protocol Config NFT. It defaults to
    `PROTOCOL_CONFIG_SKH`; pass `config_address` to point at another deployment.
    """
    nft = config_nft or PROTOCOL_CONFIG_NFT
    if nft is None:
        raise ValueError("PROTOCOL_CONFIG_NFT anchor not set")
    if config_address is None and PROTOCOL_CONFIG_SKH is not None:
        config_address = _addr(PROTOCOL_CONFIG_SKH)
    addresses = [config_address] if config_address else []
    rows = list(
        backend.get_pool_utxos(
            addresses=addresses,
            assets=[nft],
            limit=1,
            historical=False,
        ),
    )
    if not rows:
        raise ValueError("Protocol Config UTxO not found for anchor")
    d = ProtocolDatum.from_cbor(rows[0].datum_cbor)
    return DanogoScriptAddresses(
        pool=_addr(d.pool_skh.hex()),
        loan=_addr(d.loan_skh.hex()),
        config_pool=_addr(d.config_pool_skh.hex()),
        oracle=_addr(d.oracle_skh.hex()),
    )
