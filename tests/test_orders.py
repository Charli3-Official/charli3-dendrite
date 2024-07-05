# import pytest

# from pycardano import Address

# from cardex.backend.dbsync import get_historical_order_utxos
# from cardex.backend.dbsync import get_order_utxos_by_block_or_tx
# from cardex.dataclasses.datums import OrderDatum
# from cardex.dataclasses.models import SwapTransactionInfo
# from cardex.dexs.amm.amm_base import AbstractPairState


# def test_get_orders(dex: AbstractPairState, benchmark):
#     order_selector = dex.order_selector()
#     result = benchmark(
#         get_historical_order_utxos,
#         stake_addresses=order_selector,
#         limit=1000,
#     )

#     # Test roundtrip parsing
#     for ind, r in enumerate(result):
#         reparsed = SwapTransactionInfo(r.model_dump())
#         assert reparsed == r

#     # Test datum parsing
#     found_datum = False
#     stake_addresses = []
#     for address in order_selector:
#         stake_addresses.append(
#             (Address.decode(address).payment_part.payload)
#         )

#     for ind, r in enumerate(result):
#         for swap in r:
#             if swap.swap_input.tx_hash in [
#                 "042e04611944c260b8897e29e40c8149b843634bce272bf0cad8140455e29edb",
#             ]:
#                 continue
#             if swap.swap_input.address_stake in stake_addresses:
#                 datum = dex.order_datum_class().from_cbor(swap.swap_input.datum_cbor)
#                 found_datum = True

#     assert found_datum


# def test_order_type(dex: AbstractPairState):
#     assert issubclass(dex.order_datum_class(), OrderDatum)


# @pytest.mark.parametrize("block", [9655329])
# def test_get_orders_in_block(block: int, dexs: list[AbstractPairState]):
#     order_selector = []
#     for dex in dexs:
#         order_selector.extend(dex.order_selector())
#     orders = get_order_utxos_by_block_or_tx(
#         stake_addresses=order_selector, block_no=block
#     )

#     # Assert requested assets are not empty
#     for order in orders:
#         for swap in order:
#             swap_input = swap.swap_input
#             for dex in dexs:
#                 if swap_input.address_stake in dex.order_selector():
#                     try:
#                         datum = dex.order_datum_class().from_cbor(swap_input.datum_cbor)
#                         break
#                     except (DeserializeException, TypeError, AssertionError):
#                         continue
#                 else:
#                     continue

#             assert "" not in datum.requested_amount()
