"""Unit tests for BlockFrostBackend network selection from project id."""

from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from blockfrost import ApiUrls

from charli3_dendrite.backend.blockfrost import BlockFrostBackend


@pytest.mark.parametrize(
    ("project_id", "expected_url"),
    [
        ("preprod" + "a" * 25, ApiUrls.preprod.value),
        ("preview" + "a" * 25, ApiUrls.preview.value),
        ("mainnet" + "a" * 25, ApiUrls.mainnet.value),
    ],
)
@patch("charli3_dendrite.backend.blockfrost.BlockFrostChainContext")
def test_blockfrost_backend_base_url_from_project_id(
    mock_ctx: MagicMock,
    project_id: str,
    expected_url: str,
) -> None:
    """BlockFrostBackend must pick ApiUrls from the project id prefix.

    Testnet project ids hitting mainnet return HTTP 403 (network token mismatch).
    """
    mock_ctx.return_value = MagicMock()
    BlockFrostBackend(project_id)
    mock_ctx.assert_called_once()
    _args, kwargs = mock_ctx.call_args
    assert kwargs["base_url"] == expected_url


@pytest.mark.parametrize("env_name", ["BLOCKFROST_PROJECT_ID", "PROJECT_ID"])
def test_set_default_backend_reads_project_id_env(
    monkeypatch: pytest.MonkeyPatch,
    env_name: str,
) -> None:
    """Default backend accepts BLOCKFROST_PROJECT_ID or PROJECT_ID."""
    import charli3_dendrite.backend as backend_mod

    for key in (
        "DBSYNC_USER",
        "DBSYNC_PASS",
        "DBSYNC_HOST",
        "DBSYNC_PORT",
        "DBSYNC_DB_NAME",
        "BLOCKFROST_PROJECT_ID",
        "PROJECT_ID",
        "OGMIOS_URL",
        "KUPO_URL",
        "CARDANO_NETWORK",
    ):
        monkeypatch.delenv(key, raising=False)

    project_id = "preview" + "b" * 25
    monkeypatch.setenv(env_name, project_id)
    monkeypatch.setattr(backend_mod, "BACKEND", None)

    with patch.object(backend_mod, "BlockFrostBackend") as mock_bf:
        mock_bf.return_value = MagicMock()
        backend_mod.set_default_backend()
        mock_bf.assert_called_once_with(project_id=project_id)
