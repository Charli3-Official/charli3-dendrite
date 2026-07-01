"""Derivation of Danogo loan identifiers shared by the build + replay paths."""

from __future__ import annotations

import hashlib

# The owner NFT name is a 28-byte (blake2b-224) digest, the loan-id width.
OWNER_NFT_DIGEST_SIZE = 28


def _index_seed_bytes(output_index: int) -> bytes:
    """The output index as the validator appends it to the loan-id seed.

    Minimal big-endian bytes with no leading zeros, so index 0 contributes no
    bytes at all (matching the on-chain encoding: ``#0`` hashes the bare tx id,
    ``#2`` hashes the tx id followed by a single ``0x02`` byte).
    """
    if output_index == 0:
        return b""
    length = (output_index.bit_length() + 7) // 8
    return output_index.to_bytes(length, "big")


def owner_nft_name(pool_in_out_ref: tuple[str, int]) -> str:
    """Owner-NFT asset name: blake2b-224 of the spent pool's output reference.

    The seed is the pool input's transaction id followed by its output index
    (minimal big-endian, omitted for index 0); hashing the full reference --
    not just the transaction id -- is what makes each loan id distinct when
    several outputs of one transaction can seed loans.
    """
    tx_id_hex, output_index = pool_in_out_ref
    digest = hashlib.blake2b(
        bytes.fromhex(tx_id_hex) + _index_seed_bytes(output_index),
        digest_size=OWNER_NFT_DIGEST_SIZE,
    )
    return digest.hexdigest()
