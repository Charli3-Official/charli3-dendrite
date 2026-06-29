from charli3_dendrite.lending.fluidtokens.constants import (
    PROTOCOL_CONFIG_NFT_POLICY,
    PROTOCOL_CONFIG_NFT_NAME,
    POOL_ADDRESS,
    LOAN_ADDRESS,
    REQUEST_ADDRESS,
)


def test_config_nft_constants():
    assert PROTOCOL_CONFIG_NFT_POLICY == (
        "219832152b2c489358f4c02a1818d312a851b1f55774ae881e33a907"
    )
    assert PROTOCOL_CONFIG_NFT_NAME == "706172616d6574657273"  # "parameters"


def test_entity_addresses_are_mainnet_script_addresses():
    for addr in (POOL_ADDRESS, LOAN_ADDRESS, REQUEST_ADDRESS):
        assert addr.startswith("addr1")
