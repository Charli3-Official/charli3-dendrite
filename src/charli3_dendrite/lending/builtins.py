"""The protocol modules shipped with the lending package, in one table.

Each entry names the module that registers a protocol (never its bare package), and
that module provides:

- ``RATE_MODELS: tuple[type[RateModel], ...]``, the protocol's rate models, which
  :mod:`charli3_dendrite.lending.rates` registers on its first lookup; and
- ``market_source() -> MarketSource``, the protocol's market source, which
  :mod:`charli3_dendrite.lending.markets` registers on its first use.

Both registries keep a caller's own registration under the same key. No entry is
imported until a registry is first used.
"""

from __future__ import annotations

BUILTIN_PROTOCOL_PACKAGES: tuple[str, ...] = (
    "charli3_dendrite.lending.fluidtokens_v4.market",
    "charli3_dendrite.lending.liqwid.market",
)
