"""FluidTokens V3 mainnet coordinates.

The protocol is parameterized by a single config NFT; the config UTxO datum holds
every protocol script hash / policy id. The constants below are the live mainnet
deployment coordinates, and `resolve_addresses` returns them as the coordinate set
used by the loader/builder.
"""
from __future__ import annotations

from typing import TYPE_CHECKING
from typing import TypedDict

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


def resolve_addresses(backend: AbstractBackend) -> FluidScriptAddresses:  # noqa: ARG001
    """Return the mainnet coordinate set.

    Returns the constant coordinates defined in this module. The `backend` argument is
    a design seam: a future revision can re-read the config NFT datum through it to
    survive a redeploy, matching Danogo's `resolve_addresses` signature.
    """
    return FluidScriptAddresses(
        pool=POOL_ADDRESS,
        loan=LOAN_ADDRESS,
        request=REQUEST_ADDRESS,
        pool_policy=POOL_POLICY,
        loan_policy=LOAN_POLICY,
        request_policy=REQUEST_POLICY,
    )
