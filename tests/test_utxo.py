import os

import pytest
from cardex import MinswapCPPState
from cardex import MuesliSwapCLPState
from cardex import MuesliSwapCPPState
from cardex import SpectrumCPPState
from cardex import SundaeSwapCPPState
from cardex import VyFiCPPState
from cardex import WingRidersCPPState
from cardex import WingRidersSSPState
from cardex.backend.dbsync import get_pool_utxos
from cardex.dataclasses.models import Assets
from cardex.dexs.amm_base import AbstractPoolState
from cardex.dexs.errors import InvalidLPError
from cardex.dexs.errors import InvalidPoolError
from cardex.dexs.errors import NoAssetsError
from dotenv import load_dotenv
from pycardano import Address
from pycardano import BlockFrostChainContext
from pycardano import ExtendedSigningKey
from pycardano import HDWallet
from pycardano import blockfrost

load_dotenv()

context = BlockFrostChainContext(
    os.environ["PROJECT_ID"],
    base_url=getattr(blockfrost.ApiUrls, os.environ["NETWORK"]).value,
)

DEXS: list[AbstractPoolState] = [
    MinswapCPPState,
    MuesliSwapCLPState,
    MuesliSwapCPPState,
    SpectrumCPPState,
    SundaeSwapCPPState,
    VyFiCPPState,
    WingRidersCPPState,
    WingRidersSSPState,
]

IUSD = "f66d78b4a3cb3d37afa0ec36461e51ecbde00f26c8f0a68f94b6988069555344"
LQ = "da8c30857834c6ae7203935b89278c532b3995245295456f993e1d244c51"
IUSD_ASSETS = Assets(root={IUSD: 10000000})
LQ_ASSETS = Assets(root={LQ: 10000000})

wallet = HDWallet.from_mnemonic(os.environ["WALLET_MNEMONIC"])

SPEND_KEY = ExtendedSigningKey.from_hdwallet(
    wallet.derive_from_path("m/1852'/1815'/0'/0/0"),
)
STAKE_KEY = ExtendedSigningKey.from_hdwallet(
    wallet.derive_from_path("m/1852'/1815'/0'/2/0"),
)
ADDRESS = Address(
    SPEND_KEY.to_verification_key().hash(),
    STAKE_KEY.to_verification_key().hash(),
)


@pytest.mark.parametrize("dex", DEXS, ids=[d.dex for d in DEXS])
def test_build_utxo(dex: AbstractPoolState, subtests):
    selector = dex.pool_selector
    result = get_pool_utxos(limit=10000, historical=False, **selector.to_dict())

    for record in result:
        try:
            pool = dex.model_validate(record.model_dump())

            if pool.unit_a == "lovelace" and pool.unit_b in [
                IUSD_ASSETS.unit(),
                LQ_ASSETS.unit(),
            ]:
                out_assets = (
                    LQ_ASSETS if pool.unit_b == LQ_ASSETS.unit() else IUSD_ASSETS
                )
                pool.swap_utxo(
                    address=ADDRESS,
                    in_assets=Assets(root={"lovelace": 1000000}),
                    out_assets=out_assets,
                )

        except InvalidLPError:
            pass
        except NoAssetsError:
            pass
        except InvalidPoolError:
            pass


@pytest.mark.parametrize("dex", DEXS, ids=[d.dex for d in DEXS])
def test_address_from_datum(dex: AbstractPoolState):
    # Create the datum
    if dex.dex == "Spectrum":
        datum = dex.order_datum_class.create_datum(
            address=ADDRESS,
            in_assets=Assets(root={"lovelace": 1000000}),
            out_assets=Assets(root={"lovelace": 1000000}),
            batcher_fee=1000000,
            volume_fee=30,
            pool_token=Assets({"lovelace": 1}),
        )
    elif dex.dex == "SundaeSwap":
        datum = dex.order_datum_class.create_datum(
            ident=b"01",
            address=ADDRESS,
            in_assets=Assets(root={"lovelace": 1000000}),
            out_assets=Assets(root={"lovelace": 1000000}),
            fee=30,
        )
    else:
        datum = dex.order_datum_class.create_datum(
            address=ADDRESS,
            in_assets=Assets(root={"lovelace": 1000000}),
            out_assets=Assets(root={"lovelace": 1000000}),
            batcher_fee=Assets(root={"lovelace": 1000000}),
            deposit=Assets(root={"lovelace": 1000000}),
        )

    assert ADDRESS.encode() == datum.source_address().encode()


# @pytest.mark.parametrize("dex", DEXS, ids=[d.dex for d in DEXS])
# def test_submit_transaction(dex: AbstractPoolState, subtests):
#     if dex in [WingRidersSSPState, MuesliSwapCLPState]:
#         pytest.skip("Currently not supported.")
#         return

#     selector = dex.pool_selector
#     result = get_pool_utxos(limit=10000, historical=False, **selector.to_dict())

#     tx_hash = None
#     for record in result:
#         try:
#             pool = dex.model_validate(record.model_dump())

#             if pool.inactive:
#                 continue

#             if pool.unit_a == "lovelace" and pool.unit_b in [
#                 IUSD_ASSETS.unit(),
#                 LQ_ASSETS.unit(),
#             ]:
#                 tx_builder = TransactionBuilder(context)
#                 tx_builder.add_input_address(ADDRESS)

#                 out_assets = (
#                     LQ_ASSETS if pool.unit_b == LQ_ASSETS.unit() else IUSD_ASSETS
#                 )

#                 utxo, datum = pool.swap_utxo(
#                     address=ADDRESS,
#                     in_assets=Assets(root={"lovelace": 1000000}),
#                     out_assets=out_assets,
#                 )

#                 datum = None if utxo.datum is not None else datum
#                 tx_builder.add_output(utxo, datum=datum, add_datum_to_witness=True)

#                 tx = tx_builder.build_and_sign(
#                     signing_keys=[SPEND_KEY],
#                     change_address=ADDRESS,
#                     auto_ttl_offset=600,
#                 )

#                 tx_hash = context.submit_tx(tx)

#                 break

#         except InvalidLPError:
#             pass
#         except NoAssetsError:
#             pass
#         except InvalidPoolError:
#             pass

#     if tx_hash is None:
#         raise ValueError("No transaction submitted")


# Test cancel transaction: 447fafeba8d431bae4b7c7a59bae85fffbf898b4877072ed9784644381f5f458
