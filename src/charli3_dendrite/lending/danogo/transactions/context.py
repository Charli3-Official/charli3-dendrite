"""Danogo pool-action contexts and snapshots: the read side of building a tx.

Covers both ways the building blocks are obtained:

- Fixture replay (`CreateLoanContext`): ordered UTxOs + derived `CreateLoan` role
  indices reconstructed from a captured tx. The role indices are derived from the
  UTxOs themselves (their addresses, assets, and datums) rather than read from a
  redeemer, so the same classifier works for replayed fixtures and live snapshots.
- Live resolution (`CreateLoanSnapshot`, `TopupWithdrawSnapshot`): the current
  unspent on-chain UTxOs a forward build needs. Both share `resolve_pool_action_commons`
  for the prefix every pool action reads (Protocol Config -> derived script hashes ->
  market + pool UTxOs -> pool reference script); each adds only the action-specific
  oracle reference UTxOs / source leaves it needs.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING

from pycardano import PlutusData

from charli3_dendrite.lending.danogo.constants import CROSS_CHECK_SUPPLEMENTAL_HANDLES
from charli3_dendrite.lending.danogo.constants import ORACLE_DATA_SKH
from charli3_dendrite.lending.danogo.constants import PROTOCOL_CONFIG_NFT
from charli3_dendrite.lending.danogo.constants import PROTOCOL_CONFIG_SKH
from charli3_dendrite.lending.danogo.constants import _addr
from charli3_dendrite.lending.danogo.constants import resolve_addresses
from charli3_dendrite.lending.danogo.datums import LoanDatum
from charli3_dendrite.lending.danogo.datums import PoolDatum
from charli3_dendrite.lending.danogo.datums import ProtocolDatum
from charli3_dendrite.lending.transactions.snapshot import PoolActionSnapshot

if TYPE_CHECKING:
    from charli3_dendrite.backend.backend_base import AbstractBackend
    from charli3_dendrite.dataclasses.models import PoolStateInfo
    from charli3_dendrite.lending.danogo.market import DanogoMarket

ORACLE_SKH = "012a6bd4ae76261c1d3b5067caa4010f781f5c1c64ce2779bba2f90a"

# Reference-script script hashes (Cardano mainnet). Each is a PlutusV3 reference script
# published at the Danogo script-store address and resolved live by script hash (the
# backend's reference-script lookup returns the current holder, so rotation to a new
# out-ref is handled transparently).
POOL_SCRIPT_SKH = "94dca24a1f1fcc2ff51cd90f32f4fe9e786d861a2dbf7d27598d26e8"
LOAN_MINT_SCRIPT_SKH = "aca8e306eda3eb6c25a838bebac37d929c216aab13c8d463fca5a08d"
# The oracle withdrawal script reference is published under ORACLE_SKH.
#
# Known publication out-refs (informational only; resolution uses the current
# holder, so these are not asserted against):
#   pool       -> b4a4f8cdb511085cb102bcfc48171e8f48ee4aaf28081d34a8c78be581cffec6#0
#   loan mint  -> 9fc37868ac4b28b16507d77bf9450f37887e45f683fa16e5328bddfe40966030#0
#   oracle     -> d7d2792f34246a77b151440196db4b79bca8024e9369747fafaad8ad8baaa337#0

# All three Danogo reference scripts are PlutusV3; db-sync does not surface the script
# version, so it is tagged from the confirmed deployment.
_REF_SCRIPT_TYPE = "plutusV3"


@dataclass
class Utxo:
    """A resolved UTxO (transaction output or [reference] input)."""

    address: str
    lovelace: int
    assets: list[tuple[str, str, int]]  # (policy_hex, name_hex, qty)
    datum: str | None
    ref_script: str | None = None
    ref_script_type: str | None = None
    out_ref: tuple[str, int] | None = None

    def holds(self, policy: str, name: str, qty: int | None = None) -> bool:
        """True if this UTxO holds the given asset (optionally at an exact qty)."""
        return any(
            a[0] == policy and a[1] == name and (qty is None or a[2] == qty)
            for a in self.assets
        )

    def has_name(self, name: str) -> bool:
        """True if this UTxO holds any asset with the given name."""
        return any(a[1] == name for a in self.assets)


def _as_utxo(d: dict) -> Utxo:
    return Utxo(
        address=d["address"],
        lovelace=int(d["lovelace"]),
        assets=[(p, n, int(q)) for p, n, q in d["assets"]],
        datum=d.get("datum"),
        ref_script=d.get("ref_script"),
        ref_script_type=d.get("ref_script_type"),
        out_ref=tuple(d["out_ref"]) if d.get("out_ref") else None,
    )


def _parses_as(datum_hex: str | None, cls: type[PlutusData]) -> bool:
    if not datum_hex:
        return False
    try:
        cls.from_cbor(bytes.fromhex(datum_hex))
        return True
    except Exception:  # noqa: BLE001 - any decode failure means "not this datum"
        return False


@dataclass
class CreateLoanContext:
    """Everything `build_create_loan` needs, with roles derived from the UTxOs."""

    loan_skh: str
    ref_inputs: list[Utxo]
    inputs: list[Utxo]
    outputs: list[Utxo]
    invalid_before: int | None
    invalid_hereafter: int | None
    protocol_cfg_ref_idx: int
    market_ref_idx: int
    pool_out_idx: int
    loan_out_idx: int
    fee_out_idx: int | None
    pool_in_out_ref: tuple[str, int]
    market_name: str
    oracle_redeemer_cbor: str | None = None

    @classmethod
    def from_fixture(cls, fix: dict) -> CreateLoanContext:
        """Build a context from a captured create-loan tx fixture."""
        loan_skh = fix["loan_skh"]
        ref_inputs = [_as_utxo(u) for u in fix["ref_inputs"]]
        inputs = [_as_utxo(u) for u in fix["inputs"]]
        outputs = [_as_utxo(fix["outputs"][k]) for k in sorted(fix["outputs"], key=int)]

        cfg_nft = PROTOCOL_CONFIG_NFT or ""
        cfg_policy, cfg_name = cfg_nft[:56], cfg_nft[56:]
        protocol_cfg_ref_idx = next(
            i for i, u in enumerate(ref_inputs) if u.holds(cfg_policy, cfg_name)
        )

        loan_names = [m[1] for m in fix["mints"] if m[0] == loan_skh and m[2] == 1]
        loan_out_idx, market_name = cls._find_loan_output(outputs, loan_skh, loan_names)

        pool_out_idx = next(
            i
            for i, u in enumerate(outputs)
            if not u.holds(loan_skh, market_name) and cls._is_pool(u, market_name)
        )
        pool_addr = outputs[pool_out_idx].address
        market_ref_idx = next(
            i
            for i, u in enumerate(ref_inputs)
            if u.has_name(market_name) and u.address != pool_addr
        )
        pool_input = next(
            u for u in inputs if u.address == pool_addr and u.out_ref is not None
        )
        assert pool_input.out_ref is not None  # noqa: S101 - narrowed by the filter
        fee_out_idx = cls._find_fee_output(outputs, pool_out_idx, loan_out_idx)

        oracle_redeemer_cbor = next(
            (
                r["cbor"]
                for r in fix["redeemers"]
                if r["purpose"] == "reward" and r["script_hash"] == ORACLE_SKH
            ),
            None,
        )

        return cls(
            loan_skh=loan_skh,
            ref_inputs=ref_inputs,
            inputs=inputs,
            outputs=outputs,
            invalid_before=fix.get("invalid_before"),
            invalid_hereafter=fix.get("invalid_hereafter"),
            protocol_cfg_ref_idx=protocol_cfg_ref_idx,
            market_ref_idx=market_ref_idx,
            pool_out_idx=pool_out_idx,
            loan_out_idx=loan_out_idx,
            fee_out_idx=fee_out_idx,
            pool_in_out_ref=pool_input.out_ref,
            market_name=market_name,
            oracle_redeemer_cbor=oracle_redeemer_cbor,
        )

    @staticmethod
    def _find_loan_output(
        outputs: list[Utxo],
        loan_skh: str,
        loan_names: list[str],
    ) -> tuple[int, str]:
        for i, u in enumerate(outputs):
            for nm in loan_names:
                if u.holds(loan_skh, nm, 1) and _parses_as(u.datum, LoanDatum):
                    return i, nm
        raise AssertionError("loan output not found")

    @staticmethod
    def _is_pool(u: Utxo, market_name: str) -> bool:
        return u.has_name(market_name) and _parses_as(u.datum, PoolDatum)

    @staticmethod
    def _find_fee_output(
        outputs: list[Utxo],
        pool_out_idx: int,
        loan_out_idx: int,
    ) -> int | None:
        for i, u in enumerate(outputs):
            if i in (pool_out_idx, loan_out_idx):
                continue
            if u.datum is None and not u.assets:
                return i
        return None


def _split_value(value: dict[str, int]) -> tuple[int, list[tuple[str, str, int]]]:
    """Split a flat ``unit -> qty`` value map into lovelace + native-asset triples.

    The lovelace key maps to the ADA amount; every other key is a 56-hex policy id
    concatenated with the (hex) asset name, split into a ``(policy, name, qty)`` triple.
    """
    lovelace = 0
    assets: list[tuple[str, str, int]] = []
    for unit, qty in value.items():
        if unit == "lovelace":
            lovelace = int(qty)
        else:
            assets.append((unit[:56], unit[56:], int(qty)))
    return lovelace, assets


def _utxo_from_info(info: PoolStateInfo) -> Utxo:
    """Convert a backend `PoolStateInfo` row into a resolved `Utxo`.

    Splits the flat asset map into a lovelace amount and a list of native-asset
    triples, and carries the producing `(tx_hash, index)` as the out-ref so callers
    can use the UTxO as a reference or spend input.
    """
    lovelace, assets = _split_value(info.assets.root)
    return Utxo(
        address=info.address,
        lovelace=lovelace,
        assets=assets,
        datum=info.datum_cbor,
        out_ref=(info.tx_hash, info.tx_index),
    )


def _pool_nft_name(info: PoolStateInfo, policy: str) -> str | None:
    """Asset name (hex) of the pool NFT (policy == config_pool script hash).

    The pool NFT tags both the market-param UTxO and the pool-state UTxO, linking the
    two. Returns None if no qty-1 asset under `policy` is present.
    """
    for unit, qty in info.assets.root.items():
        if unit != "lovelace" and unit.startswith(policy) and qty == 1:
            name = unit[len(policy) :]
            if name:
                return name
    return None


def _resolve_script_ref(
    backend: AbstractBackend,
    script_skh: str,
) -> Utxo:
    """Resolve a published reference-script UTxO by its script hash.

    The backend's reference-script lookup keys on the script hash, so the returned
    UTxO is authoritative: it is guaranteed to carry that exact script regardless of
    which UTxO currently holds it. Reference scripts can be re-published at a new
    out-ref (rotation) without the hash changing, so the current out-ref is taken
    as-is; only a missing script / out-ref is a real failure and raises.
    """
    from pycardano import Address
    from pycardano import Network
    from pycardano import ScriptHash

    addr = Address(
        payment_part=ScriptHash(bytes.fromhex(script_skh)),
        network=Network.MAINNET,
    )
    ref = backend.get_script_from_address(addr)
    if ref.script is None or ref.tx_hash is None or ref.tx_index is None:
        raise ValueError(f"reference script not found for {script_skh}")
    lovelace, assets = _split_value(ref.assets.root if ref.assets else {})
    return Utxo(
        address=ref.address or "",
        lovelace=lovelace,
        assets=assets,
        datum=ref.datum_cbor,
        ref_script=ref.script,
        ref_script_type=_REF_SCRIPT_TYPE,
        out_ref=(ref.tx_hash, ref.tx_index),
    )


def _resolve_market(
    backend: AbstractBackend,
    *,
    address: str,
    policy: str,
    market_name: str,
) -> tuple[DanogoMarket, Utxo]:
    from charli3_dendrite.lending.danogo.market import DanogoMarket

    for info in backend.get_pool_utxos(addresses=[address], historical=False):
        if _pool_nft_name(info, policy) != market_name:
            continue
        try:
            market_info = DanogoMarket.from_market_datum(info.datum_cbor)
        except (ValueError, IndexError, TypeError, AttributeError):
            continue
        return market_info, _utxo_from_info(info)
    raise ValueError(f"market-param UTxO not found for {market_name!r}")


def _resolve_pool(
    backend: AbstractBackend,
    *,
    address: str,
    policy: str,
    market_name: str,
) -> Utxo:
    for info in backend.get_pool_utxos(addresses=[address], historical=False):
        if _pool_nft_name(info, policy) == market_name:
            return _utxo_from_info(info)
    raise ValueError(f"pool UTxO not found for {market_name!r}")


def _resolve_oracle_data_refs(backend: AbstractBackend) -> list[Utxo]:
    """Resolve the Danogo-owned oracle config / path reference UTxOs.

    These live at the oracle-data script address; they are added as reference inputs
    by any action whose pool revaluation reads the oracle. Raises if the oracle-data
    deployment anchor is unset.
    """
    oracle_data_skh = ORACLE_DATA_SKH
    if oracle_data_skh is None:
        raise ValueError("Danogo deployment anchors are not set")
    return [
        _utxo_from_info(info)
        for info in backend.get_pool_utxos(
            addresses=[_addr(oracle_data_skh)],
            historical=False,
        )
    ]


def _resolve_oracle_source_leaves(
    backend: AbstractBackend,
    *,
    market_info: DanogoMarket,
    priced_assets: list[str],
) -> list[Utxo]:
    """Resolve the unique price-path source leaves for ``priced_assets``.

    Reuses the live-pricing machinery: for each priced asset, look up its recipe in
    the packaged registry keyed ``f"{asset}|{supply_token}"`` and resolve every
    step's handle to its current UTxO (the same single-UTxO discrimination forward
    pricing uses). Leaves shared across assets are de-duplicated by out-ref so each
    source UTxO appears once.

    Callers pass exactly the assets that must be priced: create-loan prices the
    market's collaterals *and* its alternative supply tokens (the validator re-prices
    the alt-supply tokens to revalue the pool's holdings), whereas a deposit/withdraw
    only needs the alt-supply tokens since no collateral is involved.

    An asset with no recipe is simply not priceable via this builder and is skipped.
    An asset that *has* a recipe but whose leaf fails to resolve is a real
    on-chain/data error -- it would yield an invalid tx -- so it raises rather than
    producing a silently incomplete snapshot.
    """
    from charli3_dendrite.lending.danogo.oracles.forward import resolve_leaf_info
    from charli3_dendrite.lending.danogo.oracles.locator import load_registry

    registry = load_registry()
    quote = market_info.supply_token
    leaves: list[Utxo] = []
    seen: set[tuple[str, int]] = set()
    for asset in priced_assets:
        recipe = registry.get(f"{asset}|{quote}")
        if recipe is None:
            continue
        for step in recipe.steps:
            info = resolve_leaf_info(backend, step.handle)
            if info is None:
                h = step.handle
                raise ValueError(
                    f"oracle source leaf failed to resolve for asset "
                    f"{asset!r}: handle otype={h.otype} "
                    f"nft_policy={h.nft_policy} nft_name={h.nft_name} "
                    f"datum_key={h.datum_key!r}",
                )
            out_ref = (info.tx_hash, info.tx_index)
            if out_ref in seen:
                continue
            seen.add(out_ref)
            leaves.append(_utxo_from_info(info))

    for utxo in _resolve_cross_check_supplemental_leaves(backend, quote=quote):
        if utxo.out_ref is None or utxo.out_ref in seen:
            continue
        seen.add(utxo.out_ref)
        leaves.append(utxo)
    return leaves


def _resolve_cross_check_supplemental_leaves(
    backend: AbstractBackend,
    *,
    quote: str,
) -> list[Utxo]:
    """Resolve the curated cross-check oracle feed leaves for a cross-quote market.

    Recipe-driven leaf resolution (`_resolve_oracle_source_leaves`) only fetches the
    leaves the mined pricing recipes name. A cross-quote market's ``ada -> quote``
    conversion is additionally averaged across, and deviation-checked against,
    independent external price feeds (e.g. an Indigo ``ada -> quote`` feed) that no
    recipe references, so the oracle ``Withdraw`` walks those cross-check feeds even
    though forward pricing never reads them. This fetches the curated supplemental
    feed(s) for ``quote`` so a fully-live cross-quote build can reference them; the
    compose filter then keeps only the candidate whose rate lands within the path
    config's deviation tolerance.

    Gated to cross-quote markets: a supply token is cross-quote only when a canonical
    ``lovelace -> quote`` intermediate recipe exists, so ADA-quote / single-hop markets
    (no such recipe) and quotes with no curated entry get nothing and their leaf set is
    unchanged. A configured handle that fails to resolve raises -- the same strictness
    recipe leaves get -- because a missing cross-check feed would yield an oracle
    Withdraw the validator rejects.
    """
    from charli3_dendrite.lending.danogo.oracles.forward import INTERMEDIATE_QUOTE
    from charli3_dendrite.lending.danogo.oracles.forward import resolve_leaf_info
    from charli3_dendrite.lending.danogo.oracles.locator import load_registry

    handles = CROSS_CHECK_SUPPLEMENTAL_HANDLES.get(quote)
    if not handles:
        return []
    if load_registry().get(f"{INTERMEDIATE_QUOTE}|{quote}") is None:
        return []

    leaves: list[Utxo] = []
    for handle in handles:
        info = resolve_leaf_info(backend, handle)
        if info is None:
            raise ValueError(
                f"cross-check oracle source leaf failed to resolve for supply token "
                f"{quote!r}: handle otype={handle.otype} "
                f"nft_policy={handle.nft_policy} nft_name={handle.nft_name} "
                f"datum_key={handle.datum_key!r}",
            )
        leaves.append(_utxo_from_info(info))
    return leaves


@dataclass
class LoanOracleRefs:
    """The oracle reference UTxOs / leaves a loan-pricing action attaches.

    Both create-loan and repay drive the same oracle ``Withdraw`` to price a loan's
    collateral (plus the market's alternative supply tokens), so both resolve the
    identical set: the loan-mint + oracle reference scripts, the Danogo-owned oracle
    config / path reference UTxOs, and the external price-path source leaves. The only
    difference is which assets are priced -- create-loan prices the market's
    collaterals, repay prices the specific loan's locked collateral -- so the priced
    set is passed in by the caller.
    """

    oracle_data_refs: list[Utxo]
    loan_mint_script_ref: Utxo
    oracle_script_ref: Utxo
    oracle_source_leaves: list[Utxo]


def _resolve_loan_oracle(
    backend: AbstractBackend,
    *,
    oracle_skh: str,
    market_info: DanogoMarket,
    priced_assets: list[str],
) -> LoanOracleRefs:
    """Resolve the oracle reference UTxOs / leaves for a loan-pricing action.

    Shared by create-loan and repay: resolves the loan-mint + oracle reference
    scripts, the Danogo-owned oracle config / path reference UTxOs, and the price-path
    source leaves for ``priced_assets`` (the loan's collateral plus the market's
    alternative supply tokens). ``oracle_skh`` is the deployment's oracle withdrawal
    script hash (the ProtocolDatum's ``oracle_skh``), so the oracle reference script is
    resolved for the deployment the market actually points at rather than a fixed
    constant -- the script hash rotates across oracle deployments.
    """
    return LoanOracleRefs(
        oracle_data_refs=_resolve_oracle_data_refs(backend),
        loan_mint_script_ref=_resolve_script_ref(backend, LOAN_MINT_SCRIPT_SKH),
        oracle_script_ref=_resolve_script_ref(backend, oracle_skh),
        oracle_source_leaves=_resolve_oracle_source_leaves(
            backend,
            market_info=market_info,
            priced_assets=priced_assets,
        ),
    )


def _parse_out_ref(out_ref: str) -> tuple[str, int]:
    """Parse a ``tx_hash#index`` string into a ``(tx_hash, index)`` pair."""
    tx_hash, _, idx = out_ref.partition("#")
    return tx_hash, int(idx)


def _resolve_loan_utxo(
    backend: AbstractBackend,
    *,
    loan_skh: str,
    market_name: str,
    loan_out_ref: tuple[str, int],
) -> tuple[Utxo, LoanDatum]:
    """Locate the loan UTxO to spend by its explicit out-ref.

    Scans the currently-unspent UTxOs at the loan script address for the one matching
    ``loan_out_ref``, confirms it carries the market's loan token (policy ``loan_skh``,
    asset name ``market_name``, qty 1), and parses its ``LoanDatum``. A spent or
    unknown out-ref raises -- a forward build can only spend a currently-open loan.
    """
    for info in backend.get_pool_utxos(
        addresses=[_addr(loan_skh)],
        historical=False,
    ):
        if (info.tx_hash, info.tx_index) != loan_out_ref:
            continue
        utxo = _utxo_from_info(info)
        if not utxo.holds(loan_skh, market_name, 1):
            raise ValueError(
                f"UTxO {loan_out_ref[0]}#{loan_out_ref[1]} does not carry the market "
                f"loan token {loan_skh + market_name!r}",
            )
        if utxo.datum is None:
            raise ValueError(
                f"loan UTxO {loan_out_ref[0]}#{loan_out_ref[1]} has no inline datum",
            )
        return utxo, LoanDatum.from_cbor(bytes.fromhex(utxo.datum))
    raise ValueError(
        f"open loan UTxO {loan_out_ref[0]}#{loan_out_ref[1]} not found at the loan "
        f"script address (spent, or wrong out-ref)",
    )


def _loan_collateral_units(loan: Utxo, *, loan_skh: str, market_name: str) -> list[str]:
    """The loan's locked collateral units (its holdings minus the loan token).

    Every native asset the loan UTxO carries except the market loan token
    (``loan_skh`` + ``market_name``) is collateral the repay oracle path re-prices;
    the (min-ADA) lovelace balance is not a collateral asset and is excluded.
    """
    return [
        policy + name
        for policy, name, _qty in loan.assets
        if not (policy == loan_skh and name == market_name)
    ]


@dataclass
class PoolActionCommons:
    """Shared resolution prefix for any pool-spending action.

    Every Danogo pool action (create-loan, deposit, withdraw) starts from the same
    on-chain reads: the Protocol Config UTxO (whose datum yields the four derived
    script hashes), the market-param + pool UTxOs paired by pool-NFT name, and the
    pool reference script. Oracle-specific reference UTxOs / source leaves are
    resolved separately by the actions that need them.
    """

    market_name: str
    loan_skh: str
    pool_skh: str
    config_pool_skh: str
    oracle_skh: str
    protocol_config: Utxo
    market: Utxo
    market_info: DanogoMarket
    pool: Utxo
    pool_script_ref: Utxo


def resolve_pool_action_commons(
    backend: AbstractBackend,
    *,
    market_name: str,
) -> PoolActionCommons:
    """Resolve the building blocks shared by every Danogo pool action.

    Reads the Protocol Config UTxO (by its NFT) to derive the four script hashes,
    then fetches the market-param and pool UTxOs (paired by pool-NFT name) and the
    pool reference script. `market_name` is the pool-NFT asset name (hex) shared by
    the market-param and pool UTxOs.
    """
    config_nft = PROTOCOL_CONFIG_NFT
    config_skh = PROTOCOL_CONFIG_SKH
    if config_nft is None or config_skh is None:
        raise ValueError("Danogo deployment anchors are not set")

    addresses = resolve_addresses(backend)

    cfg_rows = list(
        backend.get_pool_utxos(
            addresses=[_addr(config_skh)],
            assets=[config_nft],
            limit=1,
            historical=False,
        ),
    )
    if not cfg_rows:
        raise ValueError("Protocol Config UTxO not found")
    protocol_config = _utxo_from_info(cfg_rows[0])
    pd = ProtocolDatum.from_cbor(cfg_rows[0].datum_cbor)

    config_pool_skh = pd.config_pool_skh.hex()
    market_info, market_utxo = _resolve_market(
        backend,
        address=addresses["config_pool"],
        policy=config_pool_skh,
        market_name=market_name,
    )
    pool_utxo = _resolve_pool(
        backend,
        address=addresses["pool"],
        policy=config_pool_skh,
        market_name=market_name,
    )

    return PoolActionCommons(
        market_name=market_name,
        loan_skh=pd.loan_skh.hex(),
        pool_skh=pd.pool_skh.hex(),
        config_pool_skh=config_pool_skh,
        oracle_skh=pd.oracle_skh.hex(),
        protocol_config=protocol_config,
        market=market_utxo,
        market_info=market_info,
        pool=pool_utxo,
        pool_script_ref=_resolve_script_ref(backend, POOL_SCRIPT_SKH),
    )


@dataclass
class CreateLoanSnapshot(PoolActionSnapshot):
    """Live building blocks for constructing a *new* create-loan tx (read side).

    This is the forward-build counterpart to `CreateLoanContext`: it resolves the
    current (unspent) on-chain UTxOs a builder needs—the Protocol Config and Market
    Param reference UTxOs, the pool UTxO to spend, and the Danogo-owned oracle config /
    path reference UTxOs—plus the four derived script hashes and the parsed market.

    Output indices (`pool_out_idx`, `loan_out_idx`, ...) deliberately live on
    `CreateLoanContext`, not here: those only exist once outputs are assembled, so the
    builder computes them at build time. `oracle_data_refs` holds the Danogo-owned
    oracle UTxOs; `oracle_source_leaves` holds the external price-path source leaves
    (Charli3 / Liqwid / Splash / Minswap) the redeemer synthesis reads, resolved from
    the live pricing recipes for this market's collaterals. `pool_script_ref`,
    `loan_mint_script_ref`, and `oracle_script_ref` are the three PlutusV3 reference
    scripts the build attaches as reference scripts.
    """

    market_name: str
    loan_skh: str
    pool_skh: str
    config_pool_skh: str
    oracle_skh: str
    protocol_config: Utxo
    market: Utxo
    market_info: DanogoMarket
    pool: Utxo
    oracle_data_refs: list[Utxo]
    pool_script_ref: Utxo
    loan_mint_script_ref: Utxo
    oracle_script_ref: Utxo
    oracle_source_leaves: list[Utxo]

    # The borrower (funding) input is the only entry Ogmios cannot resolve from its own
    # ledger snapshot, so it is the sole `additionalUtxo` the evaluation needs. It is
    # derived from the caller's action params and only known when the action is
    # contributed, so it is stashed via the base's `add_actor_additional_utxo` at build
    # time; `additional_utxo()` (inherited from `PoolActionSnapshot`) returns it.

    @classmethod
    def from_backend(
        cls,
        backend: AbstractBackend,
        *,
        market_name: str,
    ) -> CreateLoanSnapshot:
        """Resolve the live building blocks for `market_name` via the backend.

        Delegates the shared prefix (Protocol Config -> derived script hashes ->
        market + pool UTxOs -> pool reference script) to
        `resolve_pool_action_commons`, then adds the create-loan-specific oracle
        reference UTxOs, the loan-mint / oracle reference scripts, and the oracle
        source leaves.
        """
        commons = resolve_pool_action_commons(backend, market_name=market_name)
        oracle = _resolve_loan_oracle(
            backend,
            oracle_skh=commons.oracle_skh,
            market_info=commons.market_info,
            priced_assets=[
                *commons.market_info.collaterals,
                *commons.market_info.alt_supply_tokens,
            ],
        )

        return cls(
            market_name=commons.market_name,
            loan_skh=commons.loan_skh,
            pool_skh=commons.pool_skh,
            config_pool_skh=commons.config_pool_skh,
            oracle_skh=commons.oracle_skh,
            protocol_config=commons.protocol_config,
            market=commons.market,
            market_info=commons.market_info,
            pool=commons.pool,
            oracle_data_refs=oracle.oracle_data_refs,
            pool_script_ref=commons.pool_script_ref,
            loan_mint_script_ref=oracle.loan_mint_script_ref,
            oracle_script_ref=oracle.oracle_script_ref,
            oracle_source_leaves=oracle.oracle_source_leaves,
        )


@dataclass
class TopupWithdrawSnapshot(PoolActionSnapshot):
    """Live building blocks for a deposit (top-up) or withdraw against a pool.

    Shares the pool-action prefix with `CreateLoanSnapshot` (Protocol Config ->
    derived script hashes -> market + pool UTxOs -> pool reference script) via
    `resolve_pool_action_commons`. A deposit/withdraw revalues the pool's holdings,
    so the oracle machinery (`oracle_script_ref`, `oracle_data_refs`,
    `oracle_source_leaves`) is only needed when the pool carries alternative supply
    tokens to re-price; for a single-supply-token market those stay empty/None.

    Resolution only: output indices and the spend/redeemer wiring live on the
    builder's contribute step, not here.
    """

    market_name: str
    pool_skh: str
    config_pool_skh: str
    oracle_skh: str
    protocol_config: Utxo
    market: Utxo
    market_info: DanogoMarket
    pool: Utxo
    pool_script_ref: Utxo
    oracle_script_ref: Utxo | None = None
    oracle_data_refs: list[Utxo] = field(default_factory=list)
    oracle_source_leaves: list[Utxo] = field(default_factory=list)

    # The actor (funding) input is the only entry Ogmios cannot resolve from its own
    # ledger snapshot, so it is the sole `additionalUtxo` the evaluation needs. The
    # pool input, the protocol-config / market reference inputs, the pool script ref,
    # and the oracle config / leaves are all live on-chain and resolved by Ogmios from
    # its ledger. The funding input is only known once the action is contributed, so it
    # is stashed via the base's `add_actor_additional_utxo`; `additional_utxo()`
    # (inherited from `PoolActionSnapshot`) returns it.

    @classmethod
    def from_backend(
        cls,
        backend: AbstractBackend,
        *,
        market_name: str,
    ) -> TopupWithdrawSnapshot:
        """Resolve the live building blocks for a deposit/withdraw on `market_name`.

        Delegates the shared prefix to `resolve_pool_action_commons`. The oracle
        reference script, Danogo-owned oracle reference UTxOs, and oracle source
        leaves are resolved only when the market carries alternative supply tokens
        (the pool revaluation re-prices those); otherwise they are left empty.
        """
        commons = resolve_pool_action_commons(backend, market_name=market_name)

        oracle_script_ref: Utxo | None = None
        oracle_data_refs: list[Utxo] = []
        oracle_source_leaves: list[Utxo] = []
        if commons.market_info.alt_supply_tokens:
            oracle_script_ref = _resolve_script_ref(backend, commons.oracle_skh)
            oracle_data_refs = _resolve_oracle_data_refs(backend)
            oracle_source_leaves = _resolve_oracle_source_leaves(
                backend,
                market_info=commons.market_info,
                priced_assets=list(commons.market_info.alt_supply_tokens),
            )

        return cls(
            market_name=commons.market_name,
            pool_skh=commons.pool_skh,
            config_pool_skh=commons.config_pool_skh,
            oracle_skh=commons.oracle_skh,
            protocol_config=commons.protocol_config,
            market=commons.market,
            market_info=commons.market_info,
            pool=commons.pool,
            pool_script_ref=commons.pool_script_ref,
            oracle_script_ref=oracle_script_ref,
            oracle_data_refs=oracle_data_refs,
            oracle_source_leaves=oracle_source_leaves,
        )


@dataclass
class RepaySnapshot(PoolActionSnapshot):
    """Live building blocks for constructing a *repay* (decrease-loan) tx (read side).

    Repay spends a specific open loan UTxO (loan token + locked collateral +
    `LoanDatum`) alongside the pool, burns the loan + owner NFTs on a full repay, and
    re-prices the loan's collateral through the oracle exactly as create-loan does. So
    this resolves the same shared prefix as `CreateLoanSnapshot`
    (`resolve_pool_action_commons`: Protocol Config -> derived script hashes -> market
    + pool UTxOs -> pool reference script) plus the same loan-pricing oracle set
    (`_resolve_loan_oracle`), and adds the repay-specific reads:

    - `loan` / `loan_datum`: the open loan UTxO to spend, located by its explicit
      out-ref (`ActionParams.loan_utxo`) and confirmed to sit at the loan script
      address carrying the market loan token.
    - `owner_nft`: the borrower's owner-NFT unit, derived from the loan datum
      (`LoanDatum.owner_nft`). It is the token the full repay burns and that the
      borrower (funding) input must carry.

    `oracle_source_leaves` cover the loan's locked collateral plus the market's
    alternative supply tokens (the assets the repay oracle `Withdraw` prices).

    Output indices and the spend / mint-burn / redeemer wiring live on the builder's
    contribute step, not here.
    """

    market_name: str
    loan_skh: str
    pool_skh: str
    config_pool_skh: str
    oracle_skh: str
    protocol_config: Utxo
    market: Utxo
    market_info: DanogoMarket
    pool: Utxo
    loan: Utxo
    loan_datum: LoanDatum
    owner_nft: str
    oracle_data_refs: list[Utxo]
    pool_script_ref: Utxo
    loan_mint_script_ref: Utxo
    oracle_script_ref: Utxo
    oracle_source_leaves: list[Utxo]

    # The borrower (funding) input -- a plain, datum-less wallet UTxO holding the owner
    # NFT (qty 1) plus the supply token to repay -- is the only entry Ogmios cannot
    # resolve from its own ledger snapshot. The db-sync backend keys on datum-bearing
    # UTxOs, so a datum-less owner-NFT holder is not resolvable here; the builder
    # synthesizes the borrower input from `ActionParams.actor_utxo` + this `owner_nft`
    # at contribute time (mirroring create-loan's `add_borrower_funding`) and stashes
    # it via the base's `add_actor_additional_utxo`; `additional_utxo()` returns it.

    @classmethod
    def from_backend(
        cls,
        backend: AbstractBackend,
        *,
        market_name: str,
        loan_utxo: str,
    ) -> RepaySnapshot:
        """Resolve the live building blocks for repaying `loan_utxo` on `market_name`.

        Delegates the shared prefix to `resolve_pool_action_commons`, locates the loan
        UTxO to spend by its explicit ``tx_hash#index`` out-ref (parsing its
        `LoanDatum` and deriving the owner-NFT unit), then resolves the loan-pricing
        oracle set (`_resolve_loan_oracle`) over the loan's locked collateral plus the
        market's alternative supply tokens.
        """
        commons = resolve_pool_action_commons(backend, market_name=market_name)
        loan, loan_datum = _resolve_loan_utxo(
            backend,
            loan_skh=commons.loan_skh,
            market_name=market_name,
            loan_out_ref=_parse_out_ref(loan_utxo),
        )
        collateral_units = _loan_collateral_units(
            loan,
            loan_skh=commons.loan_skh,
            market_name=market_name,
        )
        oracle = _resolve_loan_oracle(
            backend,
            oracle_skh=commons.oracle_skh,
            market_info=commons.market_info,
            priced_assets=[
                *collateral_units,
                *commons.market_info.alt_supply_tokens,
            ],
        )

        return cls(
            market_name=commons.market_name,
            loan_skh=commons.loan_skh,
            pool_skh=commons.pool_skh,
            config_pool_skh=commons.config_pool_skh,
            oracle_skh=commons.oracle_skh,
            protocol_config=commons.protocol_config,
            market=commons.market,
            market_info=commons.market_info,
            pool=commons.pool,
            loan=loan,
            loan_datum=loan_datum,
            owner_nft=loan_datum.owner_nft.unit(),
            oracle_data_refs=oracle.oracle_data_refs,
            pool_script_ref=commons.pool_script_ref,
            loan_mint_script_ref=oracle.loan_mint_script_ref,
            oracle_script_ref=oracle.oracle_script_ref,
            oracle_source_leaves=oracle.oracle_source_leaves,
        )


@dataclass
class IncreaseLoanSnapshot(PoolActionSnapshot):
    """Live building blocks for an *increase-loan* (IncreaseLoanAmount) tx (read side).

    Borrowing MORE supply token against an EXISTING loan spends a specific open loan
    UTxO (loan token + locked collateral + `LoanDatum`) alongside the pool, pays the
    additional supply token out of the pool to the borrower, and re-prices the loan's
    collateral through the oracle exactly as create-loan does. It mints nothing (the
    loan token + owner NFT already exist).

    The read side is identical to `RepaySnapshot`: the same shared pool-action prefix
    (`resolve_pool_action_commons`: Protocol Config -> derived script hashes -> market
    + pool UTxOs -> pool reference script) plus the same loan-pricing oracle set
    (`_resolve_loan_oracle`), and the loan-specific reads:

    - `loan` / `loan_datum`: the open loan UTxO to spend, located by its explicit
      out-ref (`ActionParams.loan_utxo`) and confirmed to sit at the loan script
      address carrying the market loan token.
    - `owner_nft`: the borrower's owner-NFT unit, derived from the loan datum
      (`LoanDatum.owner_nft`). It is the token the borrower (funding) input must carry
      to prove loan ownership (nothing is burned).

    `oracle_source_leaves` cover the loan's locked collateral plus the market's
    alternative supply tokens (the assets the increase oracle `Withdraw` prices). The
    increase delegates its pool/loan spends through a ``Withdraw(pool_skh)`` reward hub
    (the pool reference script `pool_script_ref` carries that withdrawal script), not
    repay's ``Withdraw(loan_skh)``; `loan_mint_script_ref` is still resolved because the
    loan spend runs under the loan script.

    Output indices and the spend / hub / redeemer wiring live on the builder's
    contribute step, not here.
    """

    market_name: str
    loan_skh: str
    pool_skh: str
    config_pool_skh: str
    oracle_skh: str
    protocol_config: Utxo
    market: Utxo
    market_info: DanogoMarket
    pool: Utxo
    loan: Utxo
    loan_datum: LoanDatum
    owner_nft: str
    oracle_data_refs: list[Utxo]
    pool_script_ref: Utxo
    loan_mint_script_ref: Utxo
    oracle_script_ref: Utxo
    oracle_source_leaves: list[Utxo]

    # The borrower (funding) input -- a plain, datum-less wallet UTxO holding the owner
    # NFT (qty 1) plus lovelace for fees -- is the only entry Ogmios cannot resolve from
    # its own ledger snapshot (no supply token is supplied in; the pool pays it out).
    # The builder synthesizes the borrower input from `ActionParams.actor_utxo` + this
    # `owner_nft` at contribute time and stashes it via the base's
    # `add_actor_additional_utxo`; `additional_utxo()` returns it.

    @classmethod
    def from_backend(
        cls,
        backend: AbstractBackend,
        *,
        market_name: str,
        loan_utxo: str,
    ) -> IncreaseLoanSnapshot:
        """Resolve the live building blocks for increasing `loan_utxo` on `market_name`.

        Delegates the shared prefix to `resolve_pool_action_commons`, locates the loan
        UTxO to spend by its explicit ``tx_hash#index`` out-ref (parsing its
        `LoanDatum` and deriving the owner-NFT unit), then resolves the loan-pricing
        oracle set (`_resolve_loan_oracle`) over the loan's locked collateral plus the
        market's alternative supply tokens.
        """
        commons = resolve_pool_action_commons(backend, market_name=market_name)
        loan, loan_datum = _resolve_loan_utxo(
            backend,
            loan_skh=commons.loan_skh,
            market_name=market_name,
            loan_out_ref=_parse_out_ref(loan_utxo),
        )
        collateral_units = _loan_collateral_units(
            loan,
            loan_skh=commons.loan_skh,
            market_name=market_name,
        )
        oracle = _resolve_loan_oracle(
            backend,
            oracle_skh=commons.oracle_skh,
            market_info=commons.market_info,
            priced_assets=[
                *collateral_units,
                *commons.market_info.alt_supply_tokens,
            ],
        )

        return cls(
            market_name=commons.market_name,
            loan_skh=commons.loan_skh,
            pool_skh=commons.pool_skh,
            config_pool_skh=commons.config_pool_skh,
            oracle_skh=commons.oracle_skh,
            protocol_config=commons.protocol_config,
            market=commons.market,
            market_info=commons.market_info,
            pool=commons.pool,
            loan=loan,
            loan_datum=loan_datum,
            owner_nft=loan_datum.owner_nft.unit(),
            oracle_data_refs=oracle.oracle_data_refs,
            pool_script_ref=commons.pool_script_ref,
            loan_mint_script_ref=oracle.loan_mint_script_ref,
            oracle_script_ref=oracle.oracle_script_ref,
            oracle_source_leaves=oracle.oracle_source_leaves,
        )


@dataclass
class ModifyCollateralSnapshot(PoolActionSnapshot):
    """Live building blocks for a *modify-collateral* (ModifyCollaterals) tx.

    Adding and/or removing collateral on an EXISTING loan WITHOUT repaying spends
    only the open loan UTxO (loan token + locked collateral + `LoanDatum`); the pool
    is a REFERENCE input (read for the interest index), never spent, so there is no
    pool spend, no pool-datum synthesis, no fee output, and no mint. The loan datum is
    byte-unchanged in -> out (only the locked collateral value moves), and the loan's
    collateral is re-priced through the oracle exactly as repay does.

    This is a slimmer `RepaySnapshot`: it shares the same pool-action prefix
    (`resolve_pool_action_commons`: Protocol Config -> derived script hashes -> market
    + pool UTxOs) and the same loan-pricing oracle set (`_resolve_loan_oracle`), but
    drops the pool-spend material -- there is no `pool_script_ref` because the pool is
    never run as a script; its UTxO (`pool`) is carried only as a reference input.

    - `pool`: the pool UTxO, carried as reference material (its out-ref + datum are
      kept so it can be added as a reference input and read for the interest index);
      it is NOT marked spendable.
    - `loan` / `loan_datum`: the open loan UTxO to spend, located by its explicit
      out-ref (`ActionParams.loan_utxo`); the loan datum is reused verbatim on output.
    - `owner_nft`: the borrower's owner-NFT unit (`LoanDatum.owner_nft`), the token the
      borrower (funding) input must carry to prove loan ownership (nothing is burned).

    `oracle_source_leaves` cover the loan's locked collateral plus the market's
    alternative supply tokens (the assets the oracle `Withdraw` prices). Output indices
    and the spend / redeemer wiring live on the builder's contribute step, not here.
    """

    market_name: str
    loan_skh: str
    pool_skh: str
    config_pool_skh: str
    oracle_skh: str
    protocol_config: Utxo
    market: Utxo
    market_info: DanogoMarket
    pool: Utxo
    loan: Utxo
    loan_datum: LoanDatum
    owner_nft: str
    oracle_data_refs: list[Utxo]
    loan_mint_script_ref: Utxo
    oracle_script_ref: Utxo
    oracle_source_leaves: list[Utxo]

    # The borrower (funding) input -- a plain, datum-less wallet UTxO holding the owner
    # NFT (qty 1) plus any ADDED collateral and lovelace for fees -- is the only entry
    # Ogmios cannot resolve from its own ledger snapshot. The builder synthesizes it
    # from `ActionParams.actor_utxo` + this `owner_nft` at contribute time and stashes
    # it via the base's `add_actor_additional_utxo`; `additional_utxo()` returns it.

    @classmethod
    def from_backend(
        cls,
        backend: AbstractBackend,
        *,
        market_name: str,
        loan_utxo: str,
    ) -> ModifyCollateralSnapshot:
        """Resolve the live building blocks for modifying `loan_utxo` on `market_name`.

        Delegates the shared prefix to `resolve_pool_action_commons` (the pool UTxO it
        returns is carried only as reference material -- a modify never spends it),
        locates the loan UTxO to spend by its explicit ``tx_hash#index`` out-ref
        (parsing its `LoanDatum` and deriving the owner-NFT unit), then resolves the
        loan-pricing oracle set (`_resolve_loan_oracle`) over the loan's locked
        collateral plus the market's alternative supply tokens.
        """
        commons = resolve_pool_action_commons(backend, market_name=market_name)
        loan, loan_datum = _resolve_loan_utxo(
            backend,
            loan_skh=commons.loan_skh,
            market_name=market_name,
            loan_out_ref=_parse_out_ref(loan_utxo),
        )
        collateral_units = _loan_collateral_units(
            loan,
            loan_skh=commons.loan_skh,
            market_name=market_name,
        )
        oracle = _resolve_loan_oracle(
            backend,
            oracle_skh=commons.oracle_skh,
            market_info=commons.market_info,
            priced_assets=[
                *collateral_units,
                *commons.market_info.alt_supply_tokens,
            ],
        )

        return cls(
            market_name=commons.market_name,
            loan_skh=commons.loan_skh,
            pool_skh=commons.pool_skh,
            config_pool_skh=commons.config_pool_skh,
            oracle_skh=commons.oracle_skh,
            protocol_config=commons.protocol_config,
            market=commons.market,
            market_info=commons.market_info,
            pool=commons.pool,
            loan=loan,
            loan_datum=loan_datum,
            owner_nft=loan_datum.owner_nft.unit(),
            oracle_data_refs=oracle.oracle_data_refs,
            loan_mint_script_ref=oracle.loan_mint_script_ref,
            oracle_script_ref=oracle.oracle_script_ref,
            oracle_source_leaves=oracle.oracle_source_leaves,
        )
