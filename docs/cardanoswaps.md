# Cardano-Swaps

Cardano-Swaps is a fully on-chain, batcher-less peer-to-peer order book. Every
order is a UTxO at the owner's swap address, and any counterparty fills it
directly by spending that UTxO and returning a continuation output. There is no
batcher and no protocol fee.

## Swap Types

### One-Way Swaps

A one-way swap offers one asset and asks for another at a fixed `Rational`
price (ask given per offer taken). A taker draws some of the offer and pays in
the ask at the quoted price or better; the continuation output keeps the
remaining offer plus the ask paid in.

| Class | Contract | Supported operations |
|-------|----------|----------------------|
| `CardanoSwapsOrderState` | Official v2 one-way build (Plutus V3) | Parse, create, fill, close |
| `CardanoSwapsLegacyOrderState` | Pre-release v2 one-way build (Plutus V2) | Parse, close |

### Two-Way Swaps

A two-way swap is one UTxO holding both assets of a pair, stored in sorted
order (`asset1 < asset2` by policy id, then asset name; ADA sorts first). Each
side carries its own price: `asset1_price` is the asset2 given per asset1 taken
(redeemer `TakeAsset1`), and `asset2_price` the asset1 given per asset2 taken
(redeemer `TakeAsset2`). The continuation output keeps quoting in both
directions as the swap is filled.

`CardanoSwapsTwoWayOrderState` is one direction of a two-way swap UTxO,
selected by `take_asset2`. Each view is oriented for the taker exactly like a
one-way swap: `unit(0)` is the asset paid in and `unit(1)` the asset drawn.
`opposite()` returns the other direction's view of the same UTxO, and
`directions()` returns both views (`TakeAsset1` first). A side drawn down to
zero still parses: that view's `available` is 0 while the opposite view stays
fillable.

## Beacon Discovery

Swap UTxOs are tagged by beacon tokens minted under a beacon policy that is
parameterised by the swap validator hash:

- **One-way**: a pair beacon, an offer beacon and an ask beacon.
- **Two-way**: a pair beacon named after the sorted pair, plus one asset beacon
  per asset.

`pool_selector()` is beacon-based rather than address-based, since each owner
has their own swap address. `CardanoSwapsOrderBook.get_book` discovers one-way
and two-way swaps from the backend when no orders are passed, lists every
direction of every swap once per `(tx_hash, tx_index, in_unit)`, and leaves out
inactive swaps, malformed prices and directions with nothing claimable. Each
level's quantity is the direction's `claimable_offer`: an ADA side keeps its
min-UTxO, so it lists its balance less that carrier, sized at the
`coins_per_utxo_byte` keyword (default `MAINNET_COINS_PER_UTXO_BYTE`). The
helpers `pair_beacon_name`, `offer_beacon_name`, `ask_beacon_name`,
`two_way_pair_beacon_name` and `two_way_asset_beacon_name` compute the beacon
asset names.

## Transaction Builders

All builders mutate a `pycardano.TransactionBuilder`. Scripts are inlined by
default, or referenced through a reference input when `swap_ref_utxo` /
`beacon_ref_utxo` is supplied.

| Builder | Applies to | Action |
|---------|------------|--------|
| `build_create` | One-way, two-way | Mint the beacons and emit the swap UTxO with an inline datum |
| `swap_utxo` | One-way, two-way, `CardanoSwapsOrderBook` | Fill a swap at its price or better and emit the continuation |
| `build_close` | One-way, two-way | Spend the swap UTxO and burn its beacons |
| `build_update` | Two-way | Re-price or re-fund a swap in place, running the beacon policy as a staking script |

The owner is identified by the key-hash staking credential of `owner_address`,
which is carried onto the swap address. A created swap's lovelace is sized to
fund the largest continuation it reaches, so every deposit stays claimable
without a taker topping the output up.

## Basic Usage

```python
from charli3_dendrite import CardanoSwapsOrderBook
from charli3_dendrite.dataclasses.models import Assets

# Discover one-way and two-way swaps for a pair and build the book
pair = Assets({"lovelace": 0, "token_policy_id": 0})
book = CardanoSwapsOrderBook.get_book(assets=pair)

for order in book.sell_book_full:
    print(f"{order.quantity} @ {order.price}")
```

```python
from charli3_dendrite import CardanoSwapsTwoWayOrderState

def show_directions(two_way_swap: CardanoSwapsTwoWayOrderState) -> None:
    """Print both directions of one parsed two-way swap UTxO."""
    for view in two_way_swap.directions():
        print(view.in_unit, view.out_unit, view.available, view.price)
```

## See Also

- [Order Book Base Classes](order_book_base.md) - Understanding order book architecture
- [Genius Yield](geniusyield.md) - Another order book implementation

::: charli3_dendrite.dexs.ob.cardanoswaps

::: charli3_dendrite.dexs.ob.cardanoswaps_two_way
