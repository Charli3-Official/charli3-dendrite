"""Regression tests for ``VyFiCPPState._encode_asset`` (token-name double-encoding).

VyFi's ``/lp`` API returns each asset's ``token_name`` in one of two forms:

* a human-readable string (e.g. ``"PUDGY"``), or
* the on-chain asset name *already hex-encoded* behind a ``"0x"`` marker,
  optionally preceded by a NUL byte (e.g. ``"\x000x55534441"`` == ``"USDA"``).

The previous implementation UTF-8-encoded both forms, which double-encoded the
marked-hex form -- corrupting the unit and (for names with non-UTF-8 bytes)
expanding it past the 32-byte on-chain asset-name limit. These tests pin the
correct behaviour for both forms.
"""

from charli3_dendrite.dexs.amm.vyfi import VyFiCPPState

POLICY = "aa" * 28  # a 56-hex-char policy id


def test_marked_hex_name_used_verbatim():
    # "\x000x55534441" carries the on-chain name bytes for "USDA".
    assert VyFiCPPState._encode_asset(POLICY, "\x000x55534441") == POLICY + "55534441"


def test_marked_hex_without_nul_prefix():
    assert VyFiCPPState._encode_asset(POLICY, "0x55534441") == POLICY + "55534441"


def test_marked_hex_non_utf8_name_not_mangled():
    # A name with non-UTF-8 bytes (0xf6...) must survive verbatim; UTF-8
    # re-encoding would expand/mangle it well past the 32-byte limit.
    name_hex = "f6cee18b885e242e91e167e80a38543e58e6c6bd9a9af86e54d8ecef21c78948"
    assert VyFiCPPState._encode_asset(POLICY, "\x000x" + name_hex) == POLICY + name_hex


def test_uppercase_marked_hex_is_lowercased():
    # The "0x" marker is always lowercase from the API; an uppercase hex
    # payload must be normalised to lowercase in the on-chain unit.
    name_hex = "446A65644D6963726F555344"  # "DjedMicroUSD", uppercased
    assert (
        VyFiCPPState._encode_asset(POLICY, "0x" + name_hex) == POLICY + name_hex.lower()
    )


def test_plain_text_name_utf8_encoded():
    # Human-readable names retain the original UTF-8-hex behaviour.
    assert VyFiCPPState._encode_asset(POLICY, "PUDGY") == POLICY + b"PUDGY".hex()


def test_marked_but_invalid_hex_falls_back_to_utf8():
    # A "0x" prefix whose remainder is not valid hex is treated as plain text.
    assert VyFiCPPState._encode_asset(POLICY, "0xZZ") == POLICY + "0xZZ".encode().hex()
