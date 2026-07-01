"""Danogo oracle deployment registry, keyed by the protocol's ``oracle_skh``.

Two Float oracle deployments coexist on mainnet under one NFT mint policy
(``bd23e297…``). They are told apart by the oracle withdrawal script hash the
ProtocolDatum carries (``oracle_skh``); each pins its own global-config NFT and a
distinct config encoding:

* the original deployment stores the global source registry as a hand-packed byte
  blob (`aggregator_datums.OracleGlobalConfig`) and per-supply routing configs as the
  packed `OraclePathDatum` (anchor + packed price paths + packed sources), selected by
  the supply token's routing anchor;
* the newer deployment stores both as structured ``Constr0`` data
  (`structured_datums.StructuredOracleGlobalConfig` /
  `StructuredOraclePathConfig`); its routing config is selected by matching the path
  datum's supply-token tuple to the market's supply token.

`deployment_for_oracle_skh` resolves the deployment for a snapshot's ``oracle_skh``
and raises on an unknown script hash so a misconfigured snapshot fails loudly rather
than silently selecting the wrong config.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# Config encoding of a deployment's oracle source / path config datums.
PathConfigKind = Literal["packed", "structured"]

# How the routing path-config UTxO is disambiguated among the many UTxOs that share
# the path-config mint policy: "anchored" walks the packed `OraclePathDatum.anchor`
# (the original deployment); "supply_token" matches the structured datum's supply-token
# tuple to the market's supply token (the newer deployment).
PathSelection = Literal["anchored", "supply_token"]


@dataclass(frozen=True)
class OracleDeployment:
    """One Float oracle deployment's anchors and config encoding.

    * ``oracle_skh`` — the oracle withdrawal script hash (the ProtocolDatum's
      ``oracle_skh``); the deployment's identity.
    * ``global_config_nft`` — the full unit (policy + name) of the NFT that tags this
      deployment's single global-source-config UTxO.
    * ``path_config_policy`` — the mint policy shared by this deployment's path-config
      UTxOs (an FT carrying a quote anchor in the original deployment, per-supply NFTs
      in the newer one).
    * ``path_config_kind`` — whether the global/path config datums are ``packed`` bytes
      or structured ``Constr`` data.
    * ``path_selection`` — how the routing path config is selected.
    """

    oracle_skh: str
    global_config_nft: str
    path_config_policy: str
    path_config_kind: PathConfigKind
    path_selection: PathSelection


# Shared NFT mint policy for both deployments' global-config NFTs and path configs.
_ORACLE_NFT_POLICY = "bd23e2977937bb6054f625e5b566e28e294dcd8f7e8d0d33979ed90c"

# The original deployment: packed global config + anchored packed path configs. Its
# global-config NFT is the long-standing `ORACLE_GLOBAL_CONFIG_NFT` anchor.
_PACKED_DEPLOYMENT = OracleDeployment(
    oracle_skh="012a6bd4ae76261c1d3b5067caa4010f781f5c1c64ce2779bba2f90a",
    global_config_nft=(
        _ORACLE_NFT_POLICY
        + "1e5e4d7ac0ba99f5f4b3d3c7764054ccce87ef497407785a4ee04d2beadb0b78"
    ),
    path_config_policy=_ORACLE_NFT_POLICY,
    path_config_kind="packed",
    path_selection="anchored",
)

# The newer deployment: structured `Constr0` global config + per-supply structured path
# configs selected by supply-token match.
_STRUCTURED_DEPLOYMENT = OracleDeployment(
    oracle_skh="2eb7e9be6a1fff3e3e33d2b05007488f199c895a051b3ee371a95f6c",
    global_config_nft=(
        _ORACLE_NFT_POLICY
        + "b9205bb92e33323ee4477966237ec461a99ce528255cdd843dd84017f5224872"
    ),
    path_config_policy=_ORACLE_NFT_POLICY,
    path_config_kind="structured",
    path_selection="supply_token",
)

_DEPLOYMENTS: dict[str, OracleDeployment] = {
    d.oracle_skh: d for d in (_PACKED_DEPLOYMENT, _STRUCTURED_DEPLOYMENT)
}


def deployment_for_oracle_skh(oracle_skh: str) -> OracleDeployment:
    """The oracle deployment for ``oracle_skh``; raises on an unknown script hash.

    ``oracle_skh`` is the ProtocolDatum's oracle withdrawal script hash (hex). An
    unregistered hash means the snapshot points at a deployment this library does not
    know how to read, so it raises rather than guessing a config layout.
    """
    deployment = _DEPLOYMENTS.get(oracle_skh)
    if deployment is None:
        raise ValueError(f"no registered oracle deployment for oracle_skh {oracle_skh}")
    return deployment
