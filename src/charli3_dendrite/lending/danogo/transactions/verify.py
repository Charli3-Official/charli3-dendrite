"""Back-compat re-export of the Ogmios evaluate-only client.

The implementation now lives in the protocol-agnostic
`charli3_dendrite.lending.transactions.infra`; this module preserves the existing
``from ...danogo.transactions.verify import evaluate_tx_cbor`` import path.
"""

from charli3_dendrite.lending.transactions.infra import evaluate_tx_cbor
from charli3_dendrite.lending.transactions.infra import ogmios_http_url

__all__ = ["evaluate_tx_cbor", "ogmios_http_url"]
